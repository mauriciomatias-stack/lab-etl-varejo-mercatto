#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETAPA 2 — TRANSFORMAÇÃO E CONFORMAÇÃO  (esqueleto: vocês completam)

Esta é a etapa epistemicamente densa. Cada bloco abaixo corresponde a uma
decisão que NÃO tem resposta única. Para cada uma, o RDT deve trazer:
    (a) as alternativas consideradas;
    (b) a escolhida e por quê;
    (c) o que se perde com ela;
    (d) o número de registros afetados.

Uma decisão sem (c) e (d) não conta como decisão registrada.

    docker compose exec etl python 02_transformacao.py
"""

import re
import unicodedata

import pandas as pd
from sqlalchemy import text

from config import eng_dw
from qualidade.assercoes import Suite


# ---------------------------------------------------------------------------
# Utilitários que vocês vão precisar — estes estão prontos.
# ---------------------------------------------------------------------------
def so_digitos(txt):
    return re.sub(r"\D", "", txt or "")


def cpf_valido(cpf):
    """Valida os dois dígitos verificadores. Retorna None se não tiver 11 dígitos."""
    c = so_digitos(cpf)
    if len(c) != 11 or len(set(c)) == 1:
        return None if len(c) != 11 else False
    b = [int(x) for x in c]
    s = sum((10 - i) * b[i] for i in range(9))
    d1 = 0 if (11 - s % 11) >= 10 else (11 - s % 11)
    s = sum((11 - i) * (b[:9] + [d1])[i] for i in range(10))
    d2 = 0 if (11 - s % 11) >= 10 else (11 - s % 11)
    return b[9] == d1 and b[10] == d2


def normaliza_nome(nome):
    """MAIÚSCULAS sem acento, espaços colapsados. Base para pareamento aproximado."""
    n = unicodedata.normalize("NFD", (nome or "").strip().upper())
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", n)


FUSO_CADASTRO = "America/Sao_Paulo"   # RDT-09
ESTADO = {}                             # números passados entre etapas


def _nulo(serie):
    """NaN/NaT/pd.NA -> None, para o driver gravar NULL."""
    return serie.astype(object).where(serie.notna(), None)


def monta_clientes(leg, eco):
    """Lógica pura (sem banco) do pareamento. Devolve (dim, ponte, info)."""
    def cpf11(v):
        d = so_digitos(v if isinstance(v, str) else None)
        return d if len(d) == 11 else None

    a = pd.DataFrame({
        "sistema": "LEGADO", "id": leg["ID_CLIENTE"].astype("int64"),
        "cpf": leg["CPF"].map(cpf11), "nome": leg["NOME"],
        "cidade": leg["CIDADE"], "uf": leg["UF"],
        "dt": pd.to_datetime(leg["DT_CADASTRO"], errors="coerce")})
    b = pd.DataFrame({
        "sistema": "ECOM", "id": eco["customer_id"].astype("int64"),
        "cpf": eco["tax_id"].map(cpf11), "nome": eco["full_name"],
        "cidade": eco["city"], "uf": eco["state"],
        "dt": (pd.to_datetime(eco["created_at"], utc=True, errors="coerce")
                 .dt.tz_convert(FUSO_CADASTRO).dt.tz_localize(None).dt.normalize())})
    u = pd.concat([a, b], ignore_index=True)

    # texto: nome e cidade em MAIÚSCULAS sem acento; UF em 2 letras; vazio -> NULL
    for c in ("nome", "cidade"):
        u[c] = u[c].map(lambda v: normaliza_nome(v) if isinstance(v, str) else None)
        u[c] = u[c].replace("", None)
    u["uf"] = u["uf"].map(lambda v: v.strip().upper()[:2] if isinstance(v, str) else None)
    u["uf"] = u["uf"].replace("", None)

    # grupo = CPF; quem não tem CPF forma um grupo sozinho
    u["grupo"] = u["cpf"].where(u["cpf"].notna(),
                                "SEM_CPF:" + u["sistema"] + ":" + u["id"].astype(str))

    # mais recente primeiro: groupby.first() pega o primeiro valor NÃO nulo
    u = u.sort_values(["dt", "id"], ascending=[False, False], na_position="last")
    g = u.groupby("grupo", sort=False)
    dim = g.agg(cpf_normalizado=("cpf", "first"), nome_padronizado=("nome", "first"),
                cidade=("cidade", "first"), uf=("uf", "first"),
                dt_primeiro_cadastro=("dt", "min")).reset_index()
    dim["id_legado"] = dim["grupo"].map(
        u[u.sistema == "LEGADO"].groupby("grupo")["id"].first())
    dim["id_ecommerce"] = dim["grupo"].map(
        u[u.sistema == "ECOM"].groupby("grupo")["id"].first())
    nl = dim["grupo"].map(u[u.sistema == "LEGADO"].groupby("grupo").size()).fillna(0)
    ne = dim["grupo"].map(u[u.sistema == "ECOM"].groupby("grupo").size()).fillna(0)

    dim["origem_unificacao"] = ["AMBOS" if (x > 0 and y > 0) else
                                "LEGADO" if x > 0 else "ECOM" for x, y in zip(nl, ne)]
    def regra(grupo, x, y):
        if grupo.startswith("SEM_CPF:"):
            return "SEM_CPF"
        if x > 0 and y > 0:
            return "CPF_EXATO_ENTRE_SISTEMAS"
        if x + y > 1:
            return "CPF_EXATO_DUPLICATA_LEGADO"
        return "CPF_SEM_PAR"
    dim["regra_pareamento"] = [regra(gr, x, y) for gr, x, y in zip(dim["grupo"], nl, ne)]
    dim["cpf_valido"] = [None if pd.isna(c) else cpf_valido(c)
                         for c in dim["cpf_normalizado"]]
    dim["nome_padronizado"] = dim["nome_padronizado"].fillna("NAO INFORMADO")

    dim = dim.reset_index(drop=True)
    dim["cliente_sk"] = dim.index + 1
    sk = dict(zip(dim["grupo"], dim["cliente_sk"]))
    ponte = pd.DataFrame({"sistema": u["sistema"], "id_origem": u["id"],
                          "cliente_sk": u["grupo"].map(sk)})

    info = {"linhas_legado": len(a), "linhas_ecom": len(b),
            "dim_cliente": len(dim),
            "sem_cpf_legado": int(a["cpf"].isna().sum()),
            "sem_cpf_ecom": int(b["cpf"].isna().sum()),
            "linhas_legado_fundidas": int((nl - 1).clip(lower=0).sum()),
            "cpfs_validos": int((dim["cpf_valido"] == True).sum()),   # noqa: E712
            "cpfs_invalidos": int((dim["cpf_valido"] == False).sum())}  # noqa: E712
    info.update(dim["origem_unificacao"].value_counts().to_dict())
    info.update(dim["regra_pareamento"].value_counts().to_dict())

    cols = ["cliente_sk", "cpf_normalizado", "cpf_valido", "nome_padronizado",
            "cidade", "uf", "dt_primeiro_cadastro", "id_legado", "id_ecommerce",
            "origem_unificacao", "regra_pareamento"]
    dim = dim[cols].copy()
    dim["cpf_normalizado"] = _nulo(dim["cpf_normalizado"])
    for c in ("cidade", "uf"):
        dim[c] = _nulo(dim[c])
    dim["dt_primeiro_cadastro"] = _nulo(dim["dt_primeiro_cadastro"].dt.date)
    for c in ("id_legado", "id_ecommerce"):
        dim[c] = _nulo(dim[c].astype("Int64"))
    dim["cpf_valido"] = _nulo(dim["cpf_valido"].astype("boolean"))
    return dim, ponte[["sistema", "id_origem", "cliente_sk"]], info


# ===========================================================================
# DECISÃO 1 — IDENTIDADE DO CLIENTE ENTRE OS DOIS SISTEMAS
# ===========================================================================
def unifica_clientes(qa):
    """
    O CPF é a única ponte direta entre legado e e-commerce, mas:
      · parte dos CPFs está ausente nas duas origens;
      · parte tem dígito verificador inválido;
      · no legado ele aparece ora com máscara, ora sem;
      · o legado ainda contém duplicatas exatas e aproximadas do mesmo cliente.

    Perguntas que sua regra precisa responder — e o RDT, justificar:
      a) CPF com dígito verificador inválido serve para parear? Ele identifica
         de forma tão estável quanto um válido, mesmo estando errado?
      b) Sem CPF, você pareia por nome normalizado + cidade + UF? Qual a taxa
         de falsos positivos que isso introduz? Estime-a.
      c) Qual erro é mais grave aqui: fundir dois clientes distintos, ou manter
         duplicado um mesmo cliente? A resposta muda conforme o uso do dado.
      d) Como você trata as duplicatas INTERNAS do legado antes de cruzar?

    Preencha dim_cliente com o rastro: id_legado, id_ecommerce,
    origem_unificacao e regra_pareamento (que regra uniu cada par).
    """
    # RDT-02 e RDT-03 — pareamento por CPF exato (11 dígitos, válido ou não);
    # sem CPF não há pareamento; duplicatas internas do legado são fundidas.
    eng = eng_dw()
    with eng.connect() as cx:
        leg = pd.read_sql(text('SELECT "ID_CLIENTE","NOME","CPF","DT_CADASTRO",'
                               '"CIDADE","UF" FROM stg.legado_clientes'), cx)
        eco = pd.read_sql(text("SELECT customer_id, full_name, tax_id, created_at,"
                               " city, state FROM stg.ecom_customer"), cx)

    dim, ponte, info = monta_clientes(leg, eco)

    with eng.begin() as cx:
        cx.execute(text("CREATE TABLE IF NOT EXISTS dw.ponte_cliente ("
                        " sistema TEXT NOT NULL, id_origem BIGINT NOT NULL,"
                        " cliente_sk BIGINT NOT NULL REFERENCES dw.dim_cliente(cliente_sk)"
                        " ON DELETE CASCADE, PRIMARY KEY (sistema, id_origem))"))
        cx.execute(text("TRUNCATE dw.dim_cliente RESTART IDENTITY CASCADE"))
        dim.to_sql("dim_cliente", cx, schema="dw", if_exists="append",
                   index=False, method="multi", chunksize=2000)
        cx.execute(text("SELECT setval(pg_get_serial_sequence('dw.dim_cliente',"
                        "'cliente_sk'), (SELECT MAX(cliente_sk) FROM dw.dim_cliente))"))
        ponte.to_sql("ponte_cliente", cx, schema="dw", if_exists="append",
                     index=False, method="multi", chunksize=5000)

    # --- asserções (princípio 6) ---
    with eng.connect() as cx:
        # contagem independente, feita em SQL direto sobre a stg
        esperado = cx.execute(text("""
            WITH u AS (
              SELECT regexp_replace(COALESCE("CPF",''),'\\D','','g') c FROM stg.legado_clientes
              UNION ALL
              SELECT regexp_replace(COALESCE(tax_id,''),'\\D','','g') FROM stg.ecom_customer)
            SELECT COUNT(DISTINCT c) FILTER (WHERE length(c)=11)
                 + COUNT(*)          FILTER (WHERE length(c)<>11) FROM u""")).scalar()
        n_dim = cx.execute(text("SELECT COUNT(*) FROM dw.dim_cliente")).scalar()
        dup_validos = cx.execute(text(
            "SELECT COUNT(*) FROM (SELECT cpf_normalizado FROM dw.dim_cliente "
            "WHERE cpf_valido GROUP BY 1 HAVING COUNT(*) > 1) t")).scalar()
        dup_todos = cx.execute(text(
            "SELECT COUNT(*) FROM (SELECT cpf_normalizado FROM dw.dim_cliente "
            "WHERE cpf_normalizado IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1) t")).scalar()
        origens = pd.read_sql(text("SELECT origem_unificacao FROM dw.dim_cliente"), cx)
        n_ponte = cx.execute(text("SELECT COUNT(*) FROM dw.ponte_cliente")).scalar()

    qa.igual("dim_cliente: linhas x contagem independente em SQL", n_dim, esperado,
             "um cliente por CPF de 11 dígitos + um por cadastro sem CPF")
    qa.igual("dim_cliente: CPF duplicado entre CPFs válidos", dup_validos, 0,
             "mesma pessoa não pode ocupar duas linhas")
    qa.igual("dim_cliente: CPF duplicado (válidos e inválidos)", dup_todos, 0,
             "a regra pareia também CPF inválido (RDT-02)")
    qa.dominio("dim_cliente: origem_unificacao", origens["origem_unificacao"],
               {"LEGADO", "ECOM", "AMBOS"})
    qa.igual("ponte_cliente: cadastros de origem mapeados", n_ponte,
             len(leg) + len(eco), "todo cadastro de origem aponta para um cliente_sk")
    qa.sem_nulos("dim_cliente: nome_padronizado", dim["nome_padronizado"])
    qa.cardinalidade_preservada("dim_cliente x cadastros de origem", len(leg) + len(eco), n_dim)

    print("clientes:", {k: info[k] for k in sorted(info)})


# ===========================================================================
# DIMENSÕES DE APOIO — tempo e canal (necessárias antes de qualquer fato)
# ===========================================================================
NOMES_MES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
             "agosto", "setembro", "outubro", "novembro", "dezembro"]


def monta_dim_tempo(inicio="2020-01-01", fim="2026-12-31"):
    """Calendário completo. dia_semana segue a norma ISO: 1 = segunda ... 7 = domingo."""
    d = pd.DataFrame({"data_sk": pd.date_range(inicio, fim, freq="D")})
    t = d["data_sk"].dt
    d["ano"], d["mes"], d["dia"] = t.year, t.month, t.day
    d["trimestre"] = t.quarter
    d["dia_semana"] = t.dayofweek + 1
    d["nome_mes"] = d["mes"].map(lambda m: NOMES_MES[m - 1])
    d["eh_fim_semana"] = d["dia_semana"] >= 6
    d["data_sk"] = d["data_sk"].dt.date
    return d[["data_sk", "ano", "trimestre", "mes", "dia", "dia_semana",
              "nome_mes", "eh_fim_semana"]]


# (sistema, canal na origem) -> canal conformado
MAPA_CANAL = {
    ("LEGADO", "LOJA"): "FISICO",
    ("LEGADO", "TELEVENDAS"): "TELEVENDAS",
    ("LEGADO", "SITE"): "ONLINE_PROPRIO",
    ("ECOM", "web"): "ONLINE_PROPRIO",
    ("ECOM", "app"): "ONLINE_PROPRIO",
    ("ECOM", "marketplace"): "MARKETPLACE",
}


def carrega_dim_tempo_e_canal(qa):
    eng = eng_dw()
    tempo = monta_dim_tempo()
    canal = pd.DataFrame([{"canal_origem": o, "canal_conformado": c, "sistema": sis}
                          for (sis, o), c in MAPA_CANAL.items()])
    with eng.begin() as cx:
        cx.execute(text("TRUNCATE dw.dim_tempo, dw.dim_canal RESTART IDENTITY CASCADE"))
        tempo.to_sql("dim_tempo", cx, schema="dw", if_exists="append",
                     index=False, method="multi", chunksize=1000)
        canal.to_sql("dim_canal", cx, schema="dw", if_exists="append", index=False)
    with eng.connect() as cx:
        leg = pd.read_sql(text('SELECT DISTINCT "CANAL" c FROM stg.legado_pedidos'), cx)["c"]
        eco = pd.read_sql(text("SELECT DISTINCT channel c FROM stg.ecom_orders"), cx)["c"]
        n_tempo = cx.execute(text("SELECT COUNT(*) FROM dw.dim_tempo")).scalar()
    qa.dominio("canal legado: valores sem mapeamento", leg,
               {o for (sis, o) in MAPA_CANAL if sis == "LEGADO"})
    qa.dominio("canal e-commerce: valores sem mapeamento", eco,
               {o for (sis, o) in MAPA_CANAL if sis == "ECOM"})
    qa.igual("dim_tempo: dias no calendário", n_tempo, len(tempo))
    print(f"dim_tempo: {n_tempo} dias | dim_canal: {len(canal)} linhas")


# (sistema, status na origem) -> (status conformado, motivo do cancelamento)
MAPA_STATUS = {
    ("LEGADO", "ENTREGUE"):    ("ENTREGUE", None),
    ("LEGADO", "EM TRANSITO"): ("EM_TRANSITO", None),
    ("LEGADO", "SEPARACAO"):   ("EM_SEPARACAO", None),
    ("LEGADO", "CANCELADO"):   ("CANCELADO", None),   # a origem não distingue o motivo
    ("ECOM", "delivered"):        ("ENTREGUE", None),
    ("ECOM", "shipped"):          ("EM_TRANSITO", None),
    ("ECOM", "processing"):       ("EM_SEPARACAO", None),
    ("ECOM", "canceled"):         ("CANCELADO", "CLIENTE"),
    ("ECOM", "payment_declined"): ("CANCELADO", "PAGAMENTO_RECUSADO"),
}


# ===========================================================================
# DECISÃO 2 — CONFORMAÇÃO DE STATUS
# ===========================================================================
def conforma_status(qa):
    """
    Legado:      ENTREGUE · CANCELADO · EM TRANSITO · SEPARACAO
    E-commerce:  delivered · canceled · payment_declined · shipped · processing

    O legado agrega em CANCELADO duas situações que o e-commerce separa:
    cancelamento pelo cliente e recusa de pagamento.

    Alternativas, todas defensáveis:
      (i)   mapear payment_declined -> CANCELADO. Simples; perde a distinção
            justamente onde ela existe.
      (ii)  criar CANCELADO_CLIENTE e CANCELADO_PAGAMENTO, deixando o legado
            com motivo NULL. Preserva a informação; cria assimetria que precisa
            ser tratada por qualquer análise posterior.
      (iii) criar um nível hierárquico: status_conformado = CANCELADO e
            motivo_cancelamento preenchido apenas quando conhecido.

    Escolha e preencha dw.dim_status. Registre quantos pedidos caem em cada
    célula do mapeamento — esse número é parte da justificativa.
    """
    # RDT-04 — alternativa (iii): status_conformado = CANCELADO, com o motivo
    # preenchido só quando a origem o distingue. Hipótese a registrar na ficha:
    # 'processing' (e-commerce) equivale a 'SEPARACAO' (legado), e 'canceled'
    # é cancelamento pelo cliente.
    eng = eng_dw()
    linhas = [{"status_origem": o, "sistema": sis, "status_conformado": c,
               "motivo_cancelamento": m} for (sis, o), (c, m) in MAPA_STATUS.items()]
    with eng.begin() as cx:
        cx.execute(text("TRUNCATE dw.dim_status RESTART IDENTITY CASCADE"))
        pd.DataFrame(linhas).to_sql("dim_status", cx, schema="dw",
                                    if_exists="append", index=False)
    with eng.connect() as cx:
        leg = pd.read_sql(text('SELECT "STATUS" s, COUNT(*) n FROM stg.legado_pedidos '
                               "GROUP BY 1"), cx)
        eco = pd.read_sql(text("SELECT status s, COUNT(*) n FROM stg.ecom_orders "
                               "GROUP BY 1"), cx)
    leg["sistema"], eco["sistema"] = "LEGADO", "ECOM"
    cel = pd.concat([leg, eco], ignore_index=True)
    cel["mapeado"] = [(sis, st) in MAPA_STATUS for sis, st in zip(cel["sistema"], cel["s"])]
    total = int(cel["n"].sum())
    cel["pct"] = (100 * cel["n"] / cel.groupby("sistema")["n"].transform("sum")).round(2)
    cel["destino"] = [MAPA_STATUS.get((a, b), ("SEM MAPEAMENTO", None))[0]
                      + (f" ({MAPA_STATUS[(a, b)][1]})" if MAPA_STATUS.get((a, b), (0, None))[1] else "")
                      for a, b in zip(cel["sistema"], cel["s"])]
    print(cel[["sistema", "s", "n", "pct", "destino"]].to_string(index=False))
    qa.igual("status: pedidos sem mapeamento", int(cel.loc[~cel["mapeado"], "n"].sum()), 0,
             "todo status de origem precisa cair em uma célula do mapeamento")
    qa.igual("status: pedidos mapeados x total na stg",
             int(cel.loc[cel["mapeado"], "n"].sum()), total)
    qa.igual("dim_status: linhas", len(linhas), len(MAPA_STATUS))


def monta_pedidos(lp, li, ep, ei):
    """Lógica pura (sem banco) de valores e datas. Devolve (pedidos, itens, info).

    RDT-05  valor_mercadoria = Σ(qtd × preço), sem frete e sem desconto;
            valor_desconto = Σ desconto; valor_total = mercadoria + frete − desconto.
            No legado VALOR_TOTAL já inclui o frete; no e-commerce total_amount não.
    RDT-06  desconto −1 (legado) é sentinela: vira 0. Frete negativo é mantido.
    RDT-08  entrega 1900-01-01 (sentinela) e entrega antes do pedido (impossível)
            viram NULL; entrega nula em pedido não entregue é nulo estrutural.
    RDT-09  datas padronizadas em FUSO_CADASTRO; DATETIME do legado é tomado como
            horário local.
    """
    def itens(df, sis, ped, prod, qtd, unit, desc):
        return pd.DataFrame({
            "sistema": sis, "pedido": df[ped].astype("int64"),
            "produto": df[prod].astype("int64"), "quantidade": df[qtd].astype("int64"),
            "valor_unitario": df[unit].astype(float).round(2),
            "desc_origem": df[desc].astype(float)})
    it = pd.concat([
        itens(li, "LEGADO", "ID_PEDIDO", "ID_PRODUTO", "QTD", "VL_UNITARIO", "VL_DESCONTO"),
        itens(ei, "ECOM", "order_id", "product_id", "quantity", "unit_price", "discount")],
        ignore_index=True)
    it["valor_desconto"] = it["desc_origem"].clip(lower=0).round(2)
    it["bruto"] = (it["quantidade"] * it["valor_unitario"]).round(2)
    it["valor_linha"] = (it["bruto"] - it["valor_desconto"]).round(2)

    def local(serie_utc):
        return (pd.to_datetime(serie_utc, utc=True, errors="coerce")
                  .dt.tz_convert(FUSO_CADASTRO).dt.tz_localize(None))
    a = pd.DataFrame({
        "sistema": "LEGADO", "id": lp["ID_PEDIDO"].astype("int64"),
        "cliente": lp["ID_CLIENTE"].astype("int64"), "canal": lp["CANAL"],
        "status": lp["STATUS"], "frete": lp["VALOR_FRETE"].astype(float),
        "total_origem": lp["VALOR_TOTAL"].astype(float),
        "ped": pd.to_datetime(lp["DT_PEDIDO"], errors="coerce"),
        "ent": pd.to_datetime(lp["DT_ENTREGA"], errors="coerce")})
    b = pd.DataFrame({
        "sistema": "ECOM", "id": ep["order_id"].astype("int64"),
        "cliente": ep["customer_id"].astype("int64"), "canal": ep["channel"],
        "status": ep["status"], "frete": ep["freight_amount"].astype(float),
        "total_origem": ep["total_amount"].astype(float),
        "ped": local(ep["placed_at"]), "ent": local(ep["delivered_at"])})
    p = pd.concat([a, b], ignore_index=True)

    # itens órfãos (pedido inexistente na origem) não entram no DW
    chave_ped = pd.MultiIndex.from_frame(p[["sistema", "id"]])
    ok = pd.MultiIndex.from_frame(it[["sistema", "pedido"]]).isin(chave_ped)
    orfaos = it[~ok]
    it = it[ok].copy()

    agg = (it.groupby(["sistema", "pedido"])
             .agg(mercadoria=("bruto", "sum"), desconto=("valor_desconto", "sum"),
                  qtd_itens=("quantidade", "sum"))
             .reset_index().rename(columns={"pedido": "id"}))
    antes = len(p)
    p = p.merge(agg, on=["sistema", "id"], how="left")
    depois = len(p)
    sem_itens = int(p["mercadoria"].isna().sum())
    for c in ("mercadoria", "desconto", "qtd_itens"):
        p[c] = p[c].fillna(0)
    p["mercadoria"] = p["mercadoria"].round(2)
    p["desconto"] = p["desconto"].round(2)
    p["total"] = (p["mercadoria"] + p["frete"] - p["desconto"]).round(2)

    # reconciliação com o total da origem
    dif = p["total"] - p["total_origem"]
    dif = dif.where(p["sistema"] == "LEGADO", p["total"] - p["frete"] - p["total_origem"])
    p["divergente"] = dif.abs() > 0.02

    # datas
    p["data_pedido"] = p["ped"].dt.normalize()
    sentinela = p["ent"].notna() & (p["ent"].dt.year <= 1900)
    ent = p["ent"].where(~sentinela).dt.normalize()
    antes_ped = ent.notna() & (ent < p["data_pedido"])
    p["data_entrega"] = ent.where(~antes_ped)
    p["dias_entrega"] = (p["data_entrega"] - p["data_pedido"]).dt.days
    p["tipo_ausencia_entrega"] = "ENTREGA_VALIDA"
    p.loc[p["ent"].isna(), "tipo_ausencia_entrega"] = "NULO_ESTRUTURAL"
    p.loc[sentinela, "tipo_ausencia_entrega"] = "SENTINELA_1900"
    p.loc[antes_ped, "tipo_ausencia_entrega"] = "ENTREGA_ANTES_DO_PEDIDO"

    # preço 10x acima da mediana do próprio produto: só medido, nunca corrigido
    med = it.groupby(["sistema", "produto"])["valor_unitario"].transform("median")
    it["preco_10x"] = it["valor_unitario"] >= 10 * med

    info = {
        "pedidos": len(p), "linhas_antes_merge": antes, "linhas_depois_merge": depois,
        "pedidos_sem_itens": sem_itens,
        "itens_orfaos_legado": int((orfaos["sistema"] == "LEGADO").sum()),
        "itens_orfaos_ecom": int((orfaos["sistema"] == "ECOM").sum()),
        "itens_desconto_menos1": int((it["desc_origem"] < 0).sum()),
        "pedidos_desconto_menos1": int(it.loc[it["desc_origem"] < 0, ["sistema", "pedido"]]
                                       .drop_duplicates().shape[0]),
        "frete_negativo_legado": int(((p["sistema"] == "LEGADO") & (p["frete"] < 0)).sum()),
        "frete_negativo_ecom": int(((p["sistema"] == "ECOM") & (p["frete"] < 0)).sum()),
        "itens_preco_10x": int(it["preco_10x"].sum()),
        "divergentes_legado": int((p["divergente"] & (p["sistema"] == "LEGADO")).sum()),
        "divergentes_ecom": int((p["divergente"] & (p["sistema"] == "ECOM")).sum()),
        "dia_mudou_no_fuso_ecom": int((
            (pd.to_datetime(ep["placed_at"], utc=True).dt.tz_localize(None).dt.normalize()
             != local(ep["placed_at"]).dt.normalize())).sum()),
    }
    for sis in ("LEGADO", "ECOM"):
        for tipo, n in p[p["sistema"] == sis]["tipo_ausencia_entrega"].value_counts().items():
            info[f"entrega_{sis}_{tipo}"] = int(n)
    return p, it, info


# ===========================================================================
# DECISÃO 3 — SEMÂNTICA DOS VALORES MONETÁRIOS
# ===========================================================================
def conforma_valores(qa):
    """
    VALOR_TOTAL (legado) e total_amount (e-commerce) NÃO medem a mesma coisa.
    Descubra a diferença comparando cada um com a soma dos itens do pedido —
    o dicionário de dados não diz, porque na vida real ele também não diria.

    Depois, preencha dw.fato_pedido com medidas explicitamente definidas:
        valor_mercadoria  (sem frete, sem desconto)
        valor_frete
        valor_desconto
        valor_total = mercadoria + frete - desconto

    Trate também:
      · frete negativo (o que é? estorno? erro? decida e registre);
      · desconto = -1 no legado (sentinela, não valor);
      · preços com magnitude 10x acima da mediana da categoria (digitação).
        Atenção: nem todo valor alto é erro. Qual é o seu critério?
    """
    # RDT-05 / RDT-06 (e as datas do RDT-08 / RDT-09, que alimentam o mesmo fato).
    eng = eng_dw()
    with eng.connect() as cx:
        lp = pd.read_sql(text('SELECT "ID_PEDIDO","ID_CLIENTE","DT_PEDIDO","DT_ENTREGA",'
                              '"STATUS","VALOR_TOTAL","VALOR_FRETE","CANAL" '
                              "FROM stg.legado_pedidos"), cx)
        li = pd.read_sql(text('SELECT "ID_PEDIDO","ID_PRODUTO","QTD","VL_UNITARIO",'
                              '"VL_DESCONTO" FROM stg.legado_itens_pedido'), cx)
        ep = pd.read_sql(text("SELECT order_id, customer_id, placed_at, delivered_at,"
                              " status, total_amount, freight_amount, channel "
                              "FROM stg.ecom_orders"), cx)
        ei = pd.read_sql(text("SELECT order_id, product_id, quantity, unit_price, discount "
                              "FROM stg.ecom_order_item"), cx)
        ponte = pd.read_sql(text("SELECT sistema, id_origem, cliente_sk FROM dw.ponte_cliente"), cx)
        canais = pd.read_sql(text("SELECT sistema, canal_origem, canal_sk FROM dw.dim_canal"), cx)
        stat = pd.read_sql(text("SELECT sistema, status_origem, status_sk FROM dw.dim_status"), cx)
        prod = pd.read_sql(text("SELECT produto_sk, origem, id_legado, id_ecommerce "
                                "FROM dw.dim_produto"), cx)

    p, it, info = monta_pedidos(lp, li, ep, ei)
    ESTADO["pedidos"] = info
    # os dicionários são montados UMA vez (montá-los dentro do laço custava O(pedidos x ponte))
    d_cli = dict(zip(zip(ponte["sistema"], ponte["id_origem"]), ponte["cliente_sk"]))
    d_can = dict(zip(zip(canais["sistema"], canais["canal_origem"]), canais["canal_sk"]))
    d_sta = dict(zip(zip(stat["sistema"], stat["status_origem"]), stat["status_sk"]))
    p["cliente_sk"] = [d_cli.get(k) for k in zip(p["sistema"], p["cliente"])]
    p["canal_sk"] = [d_can.get(k) for k in zip(p["sistema"], p["canal"])]
    p["status_sk"] = [d_sta.get(k) for k in zip(p["sistema"], p["status"])]
    n_sem_dim = int(p[["cliente_sk", "canal_sk", "status_sk"]].isna().any(axis=1).sum())

    fato = pd.DataFrame({
        "id_origem": [("LEG-" if sis == "LEGADO" else "ECOM-") + str(i)
                      for sis, i in zip(p["sistema"], p["id"])],
        "sistema_origem": p["sistema"], "cliente_sk": p["cliente_sk"],
        "data_pedido_sk": p["data_pedido"].dt.date,
        "data_entrega_sk": _nulo(p["data_entrega"].dt.date),
        "canal_sk": p["canal_sk"], "status_sk": p["status_sk"],
        "qtd_itens": p["qtd_itens"].astype(int), "valor_mercadoria": p["mercadoria"],
        "valor_frete": p["frete"].round(2), "valor_desconto": p["desconto"],
        "valor_total": p["total"], "dias_entrega": _nulo(p["dias_entrega"].astype("Int64"))})
    for c in ("cliente_sk", "canal_sk", "status_sk"):
        fato[c] = fato[c].astype("int64")

    with eng.begin() as cx:
        cx.execute(text("TRUNCATE dw.fato_pedido RESTART IDENTITY CASCADE"))
        fato.to_sql("fato_pedido", cx, schema="dw", if_exists="append",
                    index=False, method="multi", chunksize=2000)
        sk = pd.read_sql(text("SELECT pedido_sk, id_origem FROM dw.fato_pedido"), cx)
        idx_ped = dict(zip(sk["id_origem"], sk["pedido_sk"]))
        idx_prod = {("LEGADO", int(a)): b for a, b in zip(prod["id_legado"], prod["produto_sk"])
                    if pd.notna(a)}
        idx_prod.update({("ECOM", int(a)): b for a, b in zip(prod["id_ecommerce"],
                                                             prod["produto_sk"]) if pd.notna(a)})
        itf = pd.DataFrame({
            "pedido_sk": [idx_ped[("LEG-" if sis == "LEGADO" else "ECOM-") + str(i)]
                          for sis, i in zip(it["sistema"], it["pedido"])],
            "produto_sk": [idx_prod.get((sis, int(i))) for sis, i in
                           zip(it["sistema"], it["produto"])],
            "quantidade": it["quantidade"], "valor_unitario": it["valor_unitario"],
            "valor_desconto": it["valor_desconto"], "valor_linha": it["valor_linha"]})
        n_item_sem_prod = int(itf["produto_sk"].isna().sum())
        itf = itf.dropna(subset=["produto_sk"])
        itf["produto_sk"] = itf["produto_sk"].astype("int64")
        itf.to_sql("fato_item_pedido", cx, schema="dw", if_exists="append",
                   index=False, method="multi", chunksize=5000)

    with eng.connect() as cx:
        n_ped = cx.execute(text("SELECT COUNT(*) FROM dw.fato_pedido")).scalar()
        n_ped_stg = (cx.execute(text("SELECT COUNT(*) FROM stg.legado_pedidos")).scalar()
                     + cx.execute(text("SELECT COUNT(*) FROM stg.ecom_orders")).scalar())
        n_it = cx.execute(text("SELECT COUNT(*) FROM dw.fato_item_pedido")).scalar()
        n_it_ok = (cx.execute(text('SELECT COUNT(*) FROM stg.legado_itens_pedido i JOIN '
                                   'stg.legado_pedidos p ON p."ID_PEDIDO" = i."ID_PEDIDO"')).scalar()
                   + cx.execute(text("SELECT COUNT(*) FROM stg.ecom_order_item i JOIN "
                                     "stg.ecom_orders p ON p.order_id = i.order_id")).scalar())
        dif_pedido = cx.execute(text("""
            SELECT COUNT(*) FROM dw.fato_pedido f JOIN (
              SELECT pedido_sk, SUM(valor_linha) s FROM dw.fato_item_pedido GROUP BY 1) i
            USING (pedido_sk)
            WHERE ABS((f.valor_mercadoria - f.valor_desconto) - i.s) > 0.01""")).scalar()
        dif_total = cx.execute(text("""
            SELECT COUNT(*) FROM dw.fato_pedido
            WHERE ABS(valor_total - (valor_mercadoria + valor_frete - valor_desconto)) > 0.001"""
                                    )).scalar()

    qa.igual("fato_pedido: linhas x pedidos na stg", n_ped, n_ped_stg)
    qa.igual("fato_pedido: pedidos sem cliente, canal ou status", n_sem_dim, 0,
             "chave estrangeira obrigatória")
    qa.cardinalidade_preservada("fato_pedido: junção pedido x itens agregados",
                                info["linhas_antes_merge"], info["linhas_depois_merge"])
    qa.igual("fato_item_pedido: linhas x itens com pedido na origem", n_it, n_it_ok,
             "itens órfãos ficam de fora (RDT-06)")
    qa.igual("fato_item_pedido: itens sem produto", n_item_sem_prod, 0)
    qa.igual("fato_pedido: mercadoria − desconto x soma dos itens (±0,01)", dif_pedido, 0)
    qa.igual("fato_pedido: valor_total = mercadoria + frete − desconto", dif_total, 0)
    qa.igual("legado: VALOR_TOTAL x total conformado (±0,02)", info["divergentes_legado"], 0,
             "VALOR_TOTAL inclui o frete")
    qa.igual("e-commerce: total_amount x total conformado sem frete (±0,02)",
             info["divergentes_ecom"], 0, "total_amount não inclui o frete")
    print("pedidos:", {k: v for k, v in sorted(info.items())})


# ===========================================================================
# DECISÃO 4 — TAXONOMIA DE PRODUTO
# ===========================================================================
# valor bruto do legado (normalizado) -> (nivel1, nivel2) da taxonomia do e-commerce
NAO_CLASSIFICADO = ("NAO_CLASSIFICADO", None)
MAPA_CATEGORIA = {
    "INFORMATICA": ("Informática", None), "INFO": ("Informática", None),
    "ELETRODOMESTICOS": ("Eletrodomésticos", None), "ELETRO": ("Eletrodomésticos", None),
    "ELETRO-DOMESTICOS": ("Eletrodomésticos", None),
    "CASA E DECORACAO": ("Casa e Decoração", None), "CASA": ("Casa e Decoração", None),
    "MOVEIS": ("Casa e Decoração", "Móveis"),
    "FERRAMENTAS E CONSTRUCAO": ("Ferramentas", None), "FERRAMENTAS": ("Ferramentas", None),
    "GAMES": ("Games", None), "GAMES E CONSOLES": ("Games", None),
    "TELEVISORES": ("Áudio e TV", "Televisores"),
    "TV E SOM": ("Áudio e TV", None), "AUDIO/VIDEO": ("Áudio e TV", None),
    "ESPORTES E LAZER": ("Esporte e Lazer", None), "ESPORTE": ("Esporte e Lazer", None),
    "CELULAR/SMARTPHONE": ("Telefonia", "Smartphones"), "CELULARES": ("Telefonia", "Smartphones"),
    "TELEFONIA": ("Telefonia", None),
    # sem informação ou sem solução limpa (RDT-07)
    "DIVERSOS": NAO_CLASSIFICADO, "OUTROS": NAO_CLASSIFICADO, "": NAO_CLASSIFICADO,
    "INFORMATICA E GAMES": ("AMBIGUO", None),   # cobre dois ramos: Informática e Games
}


def conforma_categorias(qa):
    """
    O legado guarda categoria como texto livre ('ELETRO', 'Eletro-domesticos',
    'informatica e games', 'DIVERSOS', ''); o e-commerce tem taxonomia de dois
    níveis. Construa o mapeamento.

    Casos que não têm solução limpa — e é esse o ponto:
      · 'informatica e games' cobre dois ramos distintos da taxonomia;
      · 'DIVERSOS', 'OUTROS' e '' não informam nada;
      · 'TV E SOM' cobre dois filhos de 'Áudio e TV'.

    Mantenha categoria_origem preenchida com o valor bruto. Conformar não
    autoriza apagar a evidência do que havia antes.
    """
    # RDT-07 — mapeamento do texto livre do legado para a taxonomia do e-commerce.
    # Produtos NÃO são pareados entre sistemas: o legado não tem SKU e as
    # descrições não coincidem. Cada produto de origem vira uma linha.
    eng = eng_dw()
    with eng.connect() as cx:
        cat = pd.read_sql(text("SELECT category_id, name, parent_id FROM stg.ecom_category"), cx)
        pl = pd.read_sql(text('SELECT "ID_PRODUTO","DESCRICAO","CATEGORIA","PRECO_TABELA",'
                              '"ATIVO" FROM stg.legado_produtos'), cx)
        pe = pd.read_sql(text("SELECT product_id, sku, title, category_id, list_price,"
                              " active FROM stg.ecom_product"), cx)
    nome = dict(zip(cat["category_id"], cat["name"]))
    tax = {int(r.category_id): ((r.name, None) if pd.isna(r.parent_id)
                                else (nome[int(r.parent_id)], r.name))
           for r in cat.itertuples()}

    chave = pl["CATEGORIA"].map(lambda v: normaliza_nome(v) if isinstance(v, str) else "")
    n_sem_mapa = int((~chave.isin(MAPA_CATEGORIA)).sum())
    destino = chave.map(lambda k: MAPA_CATEGORIA.get(k, NAO_CLASSIFICADO))
    a = pd.DataFrame({
        "sku": None, "descricao": pl["DESCRICAO"],
        "categoria_nivel1": [d[0] for d in destino], "categoria_nivel2": [d[1] for d in destino],
        "categoria_origem": pl["CATEGORIA"], "preco_referencia": pl["PRECO_TABELA"].round(2),
        "ativo": pl["ATIVO"].map({"S": True, "N": False}),
        "id_legado": pl["ID_PRODUTO"], "id_ecommerce": None, "origem": "LEGADO"})
    ce = pe["category_id"].map(lambda c: tax[int(c)])
    b = pd.DataFrame({
        "sku": pe["sku"], "descricao": pe["title"],
        "categoria_nivel1": [c[0] for c in ce], "categoria_nivel2": [c[1] for c in ce],
        "categoria_origem": pe["category_id"].map(lambda c: str(int(c))),
        "preco_referencia": pe["list_price"].round(2), "ativo": pe["active"].astype(bool),
        "id_legado": None, "id_ecommerce": pe["product_id"], "origem": "ECOM"})
    dim = pd.concat([a, b], ignore_index=True)
    for c in ("id_legado", "id_ecommerce"):
        dim[c] = _nulo(dim[c].astype("Int64"))
    for c in ("sku", "categoria_nivel2", "ativo"):
        dim[c] = _nulo(dim[c])
    with eng.begin() as cx:
        cx.execute(text("TRUNCATE dw.dim_produto RESTART IDENTITY CASCADE"))
        dim.to_sql("dim_produto", cx, schema="dw", if_exists="append",
                   index=False, method="multi", chunksize=1000)
        n_dim = cx.execute(text("SELECT COUNT(*) FROM dw.dim_produto")).scalar()

    resumo = (pd.DataFrame({"categoria_origem": pl["CATEGORIA"], "nivel1": [d[0] for d in destino],
                            "nivel2": [d[1] for d in destino]})
              .groupby(["categoria_origem", "nivel1", "nivel2"], dropna=False)
              .size().reset_index(name="produtos"))
    print(resumo.to_string(index=False))
    n_amb = int((dim["categoria_nivel1"] == "AMBIGUO").sum())
    n_nc = int((dim["categoria_nivel1"] == "NAO_CLASSIFICADO").sum())
    print(f"produtos legado: {len(pl)} | NAO_CLASSIFICADO: {n_nc} | AMBIGUO: {n_amb}")
    qa.igual("categorias: valores do legado sem regra de mapeamento", n_sem_mapa, 0,
             "valor novo no legado cairia em NAO_CLASSIFICADO sem aviso")
    qa.igual("dim_produto: linhas x produtos de origem", n_dim, len(pl) + len(pe))
    qa.igual("dim_produto: categoria_origem preservada (legado)",
             int((dim.loc[dim.origem == "LEGADO", "categoria_origem"].fillna("") ==
                  pl["CATEGORIA"].fillna("")).sum()), len(pl),
             "conformar não pode apagar o valor bruto")


# ===========================================================================
# DECISÃO 5 — DATAS, AUSÊNCIAS E SENTINELAS
# ===========================================================================
def trata_datas_e_ausencias(qa):
    """
    Antes de imputar qualquer coisa, classifique cada ausência:
      · não coletada          (o sistema nunca teve o dado)
      · não aplicável         (pedido cancelado não tem data de entrega)
      · sentinela             ('1900-01-01', 'N/A', -1)
    As três exigem tratamentos diferentes e NENHUMA delas é "preencher com a
    média". Documente a classificação de cada campo.

    Trate ainda:
      · DT_ENTREGA anterior a DT_PEDIDO;
      · TIMESTAMPTZ do e-commerce x DATETIME sem fuso do legado — padronize
        e diga em qual fuso;
      · data_fechamento vazia no SAC (atendimento em aberto x dado perdido).
    """
    # RDT-08 / RDT-09 — o tratamento já foi aplicado em monta_pedidos; aqui ele é
    # conferido no DW e classificado para o relatório.
    eng = eng_dw()
    with eng.connect() as cx:
        antes = cx.execute(text("SELECT COUNT(*) FROM dw.fato_pedido "
                                "WHERE data_entrega_sk < data_pedido_sk")).scalar()
        sent = cx.execute(text("SELECT COUNT(*) FROM dw.fato_pedido "
                               "WHERE data_entrega_sk < DATE '1901-01-01'")).scalar()
        neg = cx.execute(text("SELECT COUNT(*) FROM dw.fato_pedido "
                              "WHERE dias_entrega < 0")).scalar()
        entregue_sem_data = cx.execute(text("""
            SELECT COUNT(*) FROM dw.fato_pedido f JOIN dw.dim_status s USING (status_sk)
            WHERE s.status_conformado = 'ENTREGUE' AND f.data_entrega_sk IS NULL""")).scalar()
        nao_entregue_com_data = cx.execute(text("""
            SELECT COUNT(*) FROM dw.fato_pedido f JOIN dw.dim_status s USING (status_sk)
            WHERE s.status_conformado <> 'ENTREGUE' AND f.data_entrega_sk IS NOT NULL""")).scalar()
        sentinela_stg = cx.execute(text(
            'SELECT COUNT(*) FROM stg.legado_pedidos WHERE "DT_ENTREGA" < TIMESTAMP \'1901-01-01\'')).scalar()
        sac_aberto = cx.execute(text(
            "SELECT COUNT(*) FROM stg.sac_atendimentos WHERE TRIM(data_fechamento) = ''")).scalar()
    inf = ESTADO.get("pedidos", {})
    ant = inf.get("entrega_LEGADO_ENTREGA_ANTES_DO_PEDIDO", 0) + inf.get("entrega_ECOM_ENTREGA_ANTES_DO_PEDIDO", 0)
    sen = inf.get("entrega_LEGADO_SENTINELA_1900", 0) + inf.get("entrega_ECOM_SENTINELA_1900", 0)
    qa.igual("datas: entrega antes do pedido no DW", antes, 0, "viram NULL (RDT-08)")
    qa.igual("datas: sentinela 1900 no DW", sent, 0)
    qa.igual("datas: dias_entrega negativo", neg, 0)
    qa.igual("datas: sentinelas tratadas x contagem independente na stg", sen, sentinela_stg)
    qa.igual("datas: pedidos ENTREGUE sem data de entrega = sentinelas + impossíveis",
             entregue_sem_data, sen + ant)
    qa.igual("datas: pedido não entregue com data de entrega", nao_entregue_com_data, 0,
             "entrega nula em pedido não entregue é nulo estrutural")
    print("ausências de entrega:", {k: v for k, v in sorted(inf.items())
                                    if k.startswith("entrega_") or k == "dia_mudou_no_fuso_ecom"})
    print(f"SAC: data_fechamento vazia = {sac_aberto} (em aberto ou perdida; entra na carga do SAC)")


# ===========================================================================
# DECISÃO 6 — CHAVES DAS FONTES DE ARQUIVO
# ===========================================================================
def liga_sac_e_avaliacoes(qa):
    """
    SAC:        chave composta (origem_sistema, id_pedido_origem) + cpf_cliente
    Avaliações: order.ref no formato 'LEG-<id>' ou 'ECOM-<id>'

    Quantos atendimentos e avaliações conseguem ser ligados a um pedido do DW?
    O que fazer com os que não conseguem — descartar, ou manter com pedido_sk
    nulo? A resposta depende da pergunta analítica; a sua é a recompra.
    """
    # TODO 2.6
    raise NotImplementedError


if __name__ == "__main__":
    qa = Suite(etapa="02_transformacao")
    for etapa in (unifica_clientes, carrega_dim_tempo_e_canal, conforma_status,
                  conforma_categorias, conforma_valores, trata_datas_e_ausencias,
                  liga_sac_e_avaliacoes):
        try:
            etapa(qa)
        except NotImplementedError:
            print(f"  (pendente) {etapa.__name__}")

    # TODO 2.7 — asserções obrigatórias desta etapa:
    #   · dim_cliente sem cpf_normalizado duplicado entre linhas com CPF válido;
    #   · toda linha de dim_cliente tem origem_unificacao em {LEGADO,ECOM,AMBOS};
    #   · nenhuma junção multiplicou linhas (use qa.cardinalidade_preservada);
    #   · soma de valor_mercadoria por pedido bate com a soma dos itens (±0,01).

    qa.registrar(eng_dw())
    if qa.imprimir():
        print("Há asserções em falha. Isso não impede seguir, mas precisa "
              "aparecer no RDT com explicação.")
