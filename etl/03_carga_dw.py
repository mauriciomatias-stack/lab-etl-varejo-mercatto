#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETAPA 3 — CARGA DO DW, QUARENTENA E RECONCILIAÇÃO

Completa o que a etapa 2 deixou pendente e verifica o conjunto:
  3.1  qa.item_orfao          itens sem pedido na origem (RDT-10)
  3.2  dw.fato_atendimento    SAC ligado a pedido e cliente (RDT-13)
  3.3  dw.fato_avaliacao      avaliações do marketplace ligadas a pedido/produto (RDT-13)
  3.4  integridade referencial do DW inteiro
  3.5  reconciliação financeira por sistema de origem (tolerância 0,1%) (RDT-11)

Pré-requisito: 02_transformacao.py já executado (dimensões e fato_pedido carregados).

    podman compose run --rm etl python 03_carga_dw.py
"""

import re

import pandas as pd
from sqlalchemy import text

from config import eng_dw
from qualidade.assercoes import Suite

FUSO = "America/Sao_Paulo"          # RDT-09: mesmo fuso da etapa 2
TOLERANCIA_FINANCEIRA = 0.001       # 0,1%
ANO_MIN, ANO_MAX = 2020, 2026       # intervalo de dw.dim_tempo

# RDT-13: motivos do SAC que são o mesmo motivo escrito de outro jeito
MAPA_MOTIVO = {
    "ATRASO ENTREGA": "ATRASO NA ENTREGA",
    "CANCELAMENTO A PEDIDO DO CLIENTE": "CANCELAMENTO CLIENTE",
    "TROCA DE PRODUTO": "TROCA",
}


# ---------------------------------------------------------------------------
# Utilitários puros (sem banco): testáveis isoladamente
# ---------------------------------------------------------------------------
def _sem_acento(s):
    import unicodedata
    n = unicodedata.normalize("NFD", str(s))
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def texto_padrao(serie):
    """strip + MAIÚSCULAS + sem acento + espaços colapsados; vazio vira NaN."""
    s = serie.astype("string").str.strip()
    s = s.map(lambda v: pd.NA if v is pd.NA or v is None else
              re.sub(r"\s+", " ", _sem_acento(v).upper()), na_action="ignore")
    return s.where(s.fillna("") != "", pd.NA)


def parse_data(serie):
    """Texto -> datetime64 (dia). Tenta dd/mm/aaaa e ISO; o que não parseia vira NaT."""
    s = serie.astype("string").str.strip()
    s = s.where(s.fillna("") != "", pd.NA)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
                "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        falta = out.isna() & s.notna()
        if not falta.any():
            break
        out[falta] = pd.to_datetime(s[falta], format=fmt, errors="coerce")
    return out.dt.normalize()


def prefixo_origem(serie):
    """'LEGADO' -> 'LEG-', 'ECOM' -> 'ECOM-'; qualquer outro valor -> NaN (não liga)."""
    u = serie.astype("string").str.strip().str.upper()
    return pd.Series(
        [("LEG-" if v.startswith("LEG") else "ECOM-" if v.startswith("ECOM") else pd.NA)
         if isinstance(v, str) else pd.NA for v in u], index=serie.index, dtype="string")


def nota_1_a_5(serie):
    """Texto/num -> Int64 em 1..5; fora do domínio ou não inteiro vira <NA>."""
    n = pd.to_numeric(serie.astype("string").str.strip().str.replace(",", ".", regex=False),
                      errors="coerce")
    n = n.where((n >= 1) & (n <= 5) & (n == n.round()))
    return n.round().astype("Int64")


def monta_atendimentos(sac, idx_pedido, idx_cpf, cliente_do_pedido):
    """Limpa o SAC. Devolve (DataFrame pronto para dw.fato_atendimento, info).

    RDT-13  · duplicata exata de linha é removida; protocolo continua único;
            · pedido ligado por (origem_sistema, id_pedido_origem) -> id_origem do DW;
            · cliente ligado só pelo CPF (sem CPF ou sem par: NULL; o cliente do pedido não é usado);
            · nota fora de 1..5 vira NULL; vírgula decimal convertida;
            · data_fechamento vazia fica NULL (em aberto ou perdida);
            · fechamento anterior à abertura vira NULL.
    """
    info = {"linhas_stg": len(sac)}
    cols = ["protocolo", "origem_sistema", "id_pedido_origem", "cpf_cliente",
            "data_abertura", "data_fechamento", "canal", "motivo",
            "nota_satisfacao", "tempo_resolucao_horas"]
    s = sac.sort_values("_linha_origem").drop_duplicates(subset=cols).copy()
    info["duplicatas_exatas_removidas"] = info["linhas_stg"] - len(s)
    antes = len(s)
    s = s.drop_duplicates(subset=["protocolo"], keep="first")
    info["protocolos_repetidos_divergentes_removidos"] = antes - len(s)

    pref = prefixo_origem(s["origem_sistema"])
    num = s["id_pedido_origem"].astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
    chave = (pref + num).where(pref.notna() & num.fillna("").str.fullmatch(r"\d+"))
    s["pedido_sk"] = chave.map(idx_pedido).astype("Int64")
    info["ligados_a_pedido"] = int(s["pedido_sk"].notna().sum())

    cpf = s["cpf_cliente"].astype("string").str.replace(r"\D", "", regex=True)
    cli_cpf = cpf.map(idx_cpf)
    cli_ped = s["pedido_sk"].map(cliente_do_pedido)
    # o cliente do pedido NÃO é usado como reserva: em 17.770 de 17.771 casos o CPF e o
    # pedido apontam para clientes diferentes (RDT-13), então o CPF é a única fonte do cliente
    s["cliente_sk"] = cli_cpf.astype("Int64")
    info["cliente_por_cpf"] = int(cli_cpf.notna().sum())
    info["cliente_so_pelo_pedido"] = 0
    ambos = cli_cpf.notna() & cli_ped.notna()
    info["cpf_e_pedido_concordam"] = int((cli_cpf[ambos] == cli_ped[ambos]).sum())
    info["cpf_e_pedido_discordam"] = int((cli_cpf[ambos] != cli_ped[ambos]).sum())
    info["sem_cliente"] = int(s["cliente_sk"].isna().sum())

    ab = parse_data(s["data_abertura"])
    fe = parse_data(s["data_fechamento"])
    info["abertura_nao_parseada"] = int(ab.isna().sum())
    info["fechamento_vazio"] = int((s["data_fechamento"].astype("string").str.strip()
                                    .fillna("") == "").sum())
    info["fechamento_nao_parseado"] = int(fe.isna().sum()) - info["fechamento_vazio"]
    antes_ab = fe.notna() & ab.notna() & (fe < ab)
    info["fechamento_antes_da_abertura"] = int(antes_ab.sum())
    fe = fe.where(~antes_ab)

    def no_intervalo(d):
        return d.where(d.dt.year.between(ANO_MIN, ANO_MAX))
    ab2, fe2 = no_intervalo(ab), no_intervalo(fe)
    info["abertura_fora_do_dim_tempo"] = int((ab.notna() & ab2.isna()).sum())
    s["data_abertura_sk"], s["data_fechamento_sk"] = ab2, fe2

    s["canal"] = texto_padrao(s["canal"])
    motivo = texto_padrao(s["motivo"])
    info["motivo_com_obs_removido"] = int(motivo.str.contains(r"OBS:", na=False).sum())
    motivo = motivo.str.replace(r"\s*OBS:.*$", "", regex=True).str.strip()
    s["motivo_conformado"] = motivo.map(lambda v: MAPA_MOTIVO.get(v, v), na_action="ignore")
    info["motivos_distintos"] = int(s["motivo_conformado"].nunique())
    nota_bruta = pd.to_numeric(s["nota_satisfacao"].astype("string").str.strip()
                               .str.replace(",", ".", regex=False), errors="coerce")
    s["nota_satisfacao"] = nota_1_a_5(s["nota_satisfacao"])
    info["nota_nula_na_origem"] = int(nota_bruta.isna().sum())
    info["nota_fora_do_dominio"] = int((nota_bruta.notna() & s["nota_satisfacao"].isna()).sum())

    h = pd.to_numeric(s["tempo_resolucao_horas"].astype("string").str.strip()
                      .str.replace(",", ".", regex=False), errors="coerce")
    info["horas_negativas"] = int((h < 0).sum())
    s["horas_resolucao"] = h.where(h >= 0).round(2)

    sem_abertura = s["data_abertura_sk"].isna()
    info["excluidos_sem_data_abertura_valida"] = int(sem_abertura.sum())
    s = s[~sem_abertura]
    info["linhas_a_carregar"] = len(s)
    saida = s[["protocolo", "cliente_sk", "pedido_sk", "data_abertura_sk",
               "data_fechamento_sk", "canal", "motivo_conformado",
               "nota_satisfacao", "horas_resolucao"]].reset_index(drop=True)
    return saida, info


def monta_avaliacoes(av, idx_pedido, idx_sku):
    """Limpa as avaliações. Devolve (DataFrame para dw.fato_avaliacao, info).

    RDT-13  · order_ref 'LEG-n'/'ECOM-n' liga direto a id_origem;
            · produto_sk só existe quando o SKU está no cadastro do e-commerce;
            · rating (texto/int/ausente) vira inteiro 1..5 ou NULL;
            · submitted_at com fuso é convertido para FUSO; sem fuso é tomado
              como horário local (decisão do RDT-09), e a data é o dia local.
    """
    info = {"linhas_stg": len(av)}
    cols = ["review_id", "order_ref", "product_sku", "submitted_at", "rating",
            "verified_purchase", "helpful_votes"]
    s = av.sort_values("_linha_origem").drop_duplicates(subset=cols).copy()
    info["duplicatas_exatas_removidas"] = info["linhas_stg"] - len(s)
    antes = len(s)
    s = s.drop_duplicates(subset=["review_id"], keep="first")
    info["review_id_repetido_divergente_removido"] = antes - len(s)

    ref = s["order_ref"].astype("string").str.strip().str.upper()
    s["pedido_sk"] = ref.map(idx_pedido).astype("Int64")
    info["ligadas_a_pedido"] = int(s["pedido_sk"].notna().sum())
    sku = s["product_sku"].astype("string").str.strip()
    s["produto_sk"] = sku.map(idx_sku).astype("Int64")
    info["ligadas_a_produto"] = int(s["produto_sk"].notna().sum())

    ts = s["submitted_at"].astype("string").str.strip()
    tem_fuso = ts.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False)
    local = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if tem_fuso.any():
        aware = pd.to_datetime(ts[tem_fuso], utc=True, format="mixed", errors="coerce")
        local[tem_fuso] = aware.dt.tz_convert(FUSO).dt.tz_localize(None)
    sem_fuso = ~tem_fuso & ts.notna()
    if sem_fuso.any():
        naive = pd.to_datetime(ts[sem_fuso], format="mixed", errors="coerce")
        local[sem_fuso] = naive
    info["data_com_fuso"] = int(tem_fuso.sum())
    info["data_sem_fuso_assumida_local"] = int(sem_fuso.sum())
    dia = local.dt.normalize()
    info["data_nao_parseada"] = int(dia.isna().sum())
    dia2 = dia.where(dia.dt.year.between(ANO_MIN, ANO_MAX))
    info["data_fora_do_dim_tempo"] = int((dia.notna() & dia2.isna()).sum())
    s["data_sk"] = dia2

    bruto = pd.to_numeric(s["rating"].astype("string").str.strip()
                          .str.replace(",", ".", regex=False), errors="coerce")
    s["nota"] = nota_1_a_5(s["rating"])
    info["nota_nula_ou_ilegivel"] = int(bruto.isna().sum())
    info["nota_fora_do_dominio"] = int((bruto.notna() & s["nota"].isna()).sum())

    v = s["verified_purchase"].astype("string").str.strip().str.lower()
    verdade = {"true", "1", "yes", "sim", "s", "y", "t", "verified"}
    falso = {"false", "0", "no", "nao", "não", "n", "f"}
    s["compra_verificada"] = pd.Series(
        [True if x in verdade else False if x in falso else pd.NA for x in v],
        index=s.index, dtype="boolean")
    info["compra_verificada_indefinida"] = int(s["compra_verificada"].isna().sum())

    vt = pd.to_numeric(s["helpful_votes"].astype("string").str.strip(), errors="coerce")
    s["votos_uteis"] = vt.where(vt >= 0).round().astype("Int64")

    sem_data = s["data_sk"].isna()
    info["excluidas_sem_data_valida"] = int(sem_data.sum())
    s = s[~sem_data]
    info["linhas_a_carregar"] = len(s)
    saida = s[["review_id", "pedido_sk", "produto_sk", "data_sk", "nota",
               "compra_verificada", "votos_uteis"]].reset_index(drop=True)
    return saida, info


def _para_banco(df, colunas_data=()):
    """Datas viram date puro; <NA>/NaT/NaN viram None (NULL no driver)."""
    df = df.copy()
    for c in colunas_data:
        df[c] = df[c].dt.date
    df = df.astype(object).where(df.notna(), None)
    return df


# ---------------------------------------------------------------------------
# 3.1 — Quarentena dos itens órfãos (RDT-10)
# ---------------------------------------------------------------------------
def quarentena_itens_orfaos(qa, eng):
    with eng.begin() as cx:
        cx.execute(text("""
            CREATE TABLE IF NOT EXISTS qa.item_orfao (
                sistema            text   NOT NULL,
                id_item            bigint,
                id_pedido          bigint NOT NULL,
                id_produto         bigint,
                quantidade         bigint,
                valor_unitario     numeric(14,2),
                desconto_origem    numeric(14,2),
                valor_bruto        numeric(14,2),
                motivo             text   NOT NULL DEFAULT 'PEDIDO_INEXISTENTE_NA_ORIGEM',
                quarentenado_em    timestamptz NOT NULL DEFAULT now())"""))
        cx.execute(text("TRUNCATE qa.item_orfao"))
        cx.execute(text("""
            INSERT INTO qa.item_orfao
                (sistema,id_item,id_pedido,id_produto,quantidade,valor_unitario,
                 desconto_origem,valor_bruto)
            SELECT 'LEGADO', i."ID_ITEM", i."ID_PEDIDO", i."ID_PRODUTO", i."QTD",
                   ROUND(i."VL_UNITARIO"::numeric,2), ROUND(i."VL_DESCONTO"::numeric,2),
                   ROUND((i."QTD"*i."VL_UNITARIO")::numeric,2)
            FROM stg.legado_itens_pedido i
            WHERE NOT EXISTS (SELECT 1 FROM stg.legado_pedidos p WHERE p."ID_PEDIDO"=i."ID_PEDIDO")
            UNION ALL
            SELECT 'ECOM', i.order_item_id, i.order_id, i.product_id, i.quantity,
                   ROUND(i.unit_price::numeric,2), ROUND(i.discount::numeric,2),
                   ROUND((i.quantity*i.unit_price)::numeric,2)
            FROM stg.ecom_order_item i
            WHERE NOT EXISTS (SELECT 1 FROM stg.ecom_orders p WHERE p.order_id=i.order_id)"""))
    with eng.connect() as cx:
        q = pd.read_sql(text("""SELECT sistema, COUNT(*) itens,
                                COUNT(DISTINCT id_pedido) pedidos, SUM(valor_bruto) valor
                                FROM qa.item_orfao GROUP BY 1 ORDER BY 1"""), cx)
        n_q = int(q["itens"].sum()) if len(q) else 0
        n_stg = (cx.execute(text("SELECT COUNT(*) FROM stg.legado_itens_pedido")).scalar()
                 + cx.execute(text("SELECT COUNT(*) FROM stg.ecom_order_item")).scalar())
        n_dw = cx.execute(text("SELECT COUNT(*) FROM dw.fato_item_pedido")).scalar()
        n_sem_prod = cx.execute(text("""SELECT COUNT(*) FROM stg.legado_itens_pedido i
            JOIN stg.legado_pedidos p ON p."ID_PEDIDO"=i."ID_PEDIDO"
            LEFT JOIN dw.dim_produto d ON d.id_legado=i."ID_PRODUTO" AND d.origem='LEGADO'
            WHERE d.produto_sk IS NULL""")).scalar()
    valor_total = float(q["valor"].sum()) if len(q) else 0.0
    pct = 100 * n_q / n_stg if n_stg else 0.0
    print("quarentena qa.item_orfao:\n", q.to_string(index=False))
    print(f"  total: {n_q} itens ({pct:.2f}% de {n_stg}); valor bruto R$ {valor_total:,.2f}")
    qa.igual("quarentena: itens em quarentena + itens no DW = itens na stg",
             n_q + n_dw, n_stg, "nenhum item some sem registro (RDT-10)")
    qa.igual("quarentena: itens com pedido sem produto no DW", n_sem_prod, 0)
    return {"itens": n_q, "valor": valor_total, "pct": pct}


# ---------------------------------------------------------------------------
# 3.2 / 3.3 — SAC e avaliações (RDT-13)
# ---------------------------------------------------------------------------
def _indices(cx):
    ped = pd.read_sql(text("SELECT pedido_sk, id_origem, cliente_sk FROM dw.fato_pedido"), cx)
    idx_pedido = dict(zip(ped["id_origem"], ped["pedido_sk"]))
    cli_ped = dict(zip(ped["pedido_sk"], ped["cliente_sk"]))
    cli = pd.read_sql(text("SELECT cliente_sk, cpf_normalizado FROM dw.dim_cliente "
                           "WHERE cpf_normalizado IS NOT NULL"), cx)
    idx_cpf = dict(zip(cli["cpf_normalizado"].str.strip(), cli["cliente_sk"]))
    prod = pd.read_sql(text("SELECT produto_sk, sku FROM dw.dim_produto "
                            "WHERE sku IS NOT NULL"), cx)
    idx_sku = dict(zip(prod["sku"].str.strip(), prod["produto_sk"]))
    return idx_pedido, cli_ped, idx_cpf, idx_sku


def carrega_atendimentos(qa, eng):
    with eng.connect() as cx:
        sac = pd.read_sql(text("SELECT * FROM stg.sac_atendimentos"), cx)
        idx_pedido, cli_ped, idx_cpf, _ = _indices(cx)
    fato, info = monta_atendimentos(sac, idx_pedido, idx_cpf, cli_ped)
    with eng.begin() as cx:
        cx.execute(text("TRUNCATE dw.fato_atendimento RESTART IDENTITY CASCADE"))
        _para_banco(fato, ("data_abertura_sk", "data_fechamento_sk")).to_sql(
            "fato_atendimento", cx, schema="dw", if_exists="append", index=False,
            method="multi", chunksize=2000)
    print("SAC:", {k: v for k, v in sorted(info.items())})
    print("SAC canal:", fato["canal"].value_counts(dropna=False).to_dict())
    print("SAC motivo:", fato["motivo_conformado"].value_counts(dropna=False).to_dict())
    with eng.connect() as cx:
        n = cx.execute(text("SELECT COUNT(*) FROM dw.fato_atendimento")).scalar()
    qa.igual("fato_atendimento: linhas carregadas x linhas tratadas", n, len(fato))
    qa.igual("fato_atendimento: stg = carregadas + duplicatas removidas + excluídas",
             n + info["duplicatas_exatas_removidas"]
             + info["protocolos_repetidos_divergentes_removidos"]
             + info["excluidos_sem_data_abertura_valida"], info["linhas_stg"])
    qa.igual("fato_atendimento: motivos distintos após conformação",
             info["motivos_distintos"], 12, "36 textos livres -> 12 motivos (RDT-13)")
    qa.unico("fato_atendimento: protocolo único", fato["protocolo"])
    qa.igual("fato_atendimento: nota fora de 1..5 após tratamento",
             int(((fato["nota_satisfacao"] < 1) | (fato["nota_satisfacao"] > 5)).sum()), 0)
    qa.igual("fato_atendimento: atendimentos sem pedido (manter com NULL, RDT-13)",
             int(fato["pedido_sk"].isna().sum()), 0,
             "o SAC liga 100% pelos pares (origem_sistema, id_pedido_origem)")
    return info


def carrega_avaliacoes(qa, eng):
    with eng.connect() as cx:
        av = pd.read_sql(text("SELECT * FROM stg.avaliacoes_marketplace"), cx)
        idx_pedido, _, _, idx_sku = _indices(cx)
    fato, info = monta_avaliacoes(av, idx_pedido, idx_sku)
    with eng.begin() as cx:
        cx.execute(text("TRUNCATE dw.fato_avaliacao RESTART IDENTITY CASCADE"))
        _para_banco(fato, ("data_sk",)).to_sql(
            "fato_avaliacao", cx, schema="dw", if_exists="append", index=False,
            method="multi", chunksize=2000)
    print("avaliações:", {k: v for k, v in sorted(info.items())})
    with eng.connect() as cx:
        n = cx.execute(text("SELECT COUNT(*) FROM dw.fato_avaliacao")).scalar()
    qa.igual("fato_avaliacao: linhas carregadas x linhas tratadas", n, len(fato))
    qa.igual("fato_avaliacao: stg = carregadas + duplicatas + excluídas",
             n + info["duplicatas_exatas_removidas"]
             + info["review_id_repetido_divergente_removido"]
             + info["excluidas_sem_data_valida"], info["linhas_stg"])
    qa.unico("fato_avaliacao: review_id único", fato["review_id"])
    qa.igual("fato_avaliacao: avaliações sem pedido", int(fato["pedido_sk"].isna().sum()), 0,
             "order.ref liga 100% pelo prefixo LEG-/ECOM-")
    qa.igual("fato_avaliacao: nota fora de 1..5 após tratamento",
             int(((fato["nota"] < 1) | (fato["nota"] > 5)).sum()), 0)
    return info


# ---------------------------------------------------------------------------
# 3.4 — Integridade referencial do DW inteiro
# ---------------------------------------------------------------------------
def integridade(qa, eng):
    checks = {
        "fato_pedido -> dim_cliente": "SELECT COUNT(*) FROM dw.fato_pedido f LEFT JOIN dw.dim_cliente d USING (cliente_sk) WHERE d.cliente_sk IS NULL",
        "fato_pedido -> dim_tempo (pedido)": "SELECT COUNT(*) FROM dw.fato_pedido f LEFT JOIN dw.dim_tempo d ON d.data_sk=f.data_pedido_sk WHERE d.data_sk IS NULL",
        "fato_pedido -> dim_canal": "SELECT COUNT(*) FROM dw.fato_pedido f LEFT JOIN dw.dim_canal d USING (canal_sk) WHERE d.canal_sk IS NULL",
        "fato_pedido -> dim_status": "SELECT COUNT(*) FROM dw.fato_pedido f LEFT JOIN dw.dim_status d USING (status_sk) WHERE d.status_sk IS NULL",
        "fato_item_pedido -> fato_pedido": "SELECT COUNT(*) FROM dw.fato_item_pedido f LEFT JOIN dw.fato_pedido d USING (pedido_sk) WHERE d.pedido_sk IS NULL",
        "fato_item_pedido -> dim_produto": "SELECT COUNT(*) FROM dw.fato_item_pedido f LEFT JOIN dw.dim_produto d USING (produto_sk) WHERE d.produto_sk IS NULL",
        "fato_atendimento -> dim_cliente (quando preenchido)": "SELECT COUNT(*) FROM dw.fato_atendimento f LEFT JOIN dw.dim_cliente d USING (cliente_sk) WHERE f.cliente_sk IS NOT NULL AND d.cliente_sk IS NULL",
        "fato_atendimento -> fato_pedido (quando preenchido)": "SELECT COUNT(*) FROM dw.fato_atendimento f LEFT JOIN dw.fato_pedido d USING (pedido_sk) WHERE f.pedido_sk IS NOT NULL AND d.pedido_sk IS NULL",
        "fato_avaliacao -> fato_pedido (quando preenchido)": "SELECT COUNT(*) FROM dw.fato_avaliacao f LEFT JOIN dw.fato_pedido d USING (pedido_sk) WHERE f.pedido_sk IS NOT NULL AND d.pedido_sk IS NULL",
        "fato_avaliacao -> dim_produto (quando preenchido)": "SELECT COUNT(*) FROM dw.fato_avaliacao f LEFT JOIN dw.dim_produto d USING (produto_sk) WHERE f.produto_sk IS NOT NULL AND d.produto_sk IS NULL",
        "ponte_cliente -> dim_cliente": "SELECT COUNT(*) FROM dw.ponte_cliente f LEFT JOIN dw.dim_cliente d USING (cliente_sk) WHERE d.cliente_sk IS NULL",
        "pedidos sem cliente (RDT-12)": "SELECT COUNT(*) FROM dw.fato_pedido WHERE cliente_sk IS NULL",
    }
    with eng.connect() as cx:
        for nome, sql in checks.items():
            qa.igual(f"integridade: {nome}", cx.execute(text(sql)).scalar(), 0,
                     "chave órfã no DW")
        extra = pd.read_sql(text("""
            SELECT (SELECT COUNT(*) FROM dw.fato_avaliacao WHERE produto_sk IS NULL) av_sem_produto,
                   (SELECT COUNT(*) FROM dw.fato_atendimento WHERE cliente_sk IS NULL) at_sem_cliente,
                   (SELECT COUNT(*) FROM dw.fato_atendimento WHERE pedido_sk IS NULL) at_sem_pedido"""), cx)
    print("lacunas esperadas (não são órfãs):", extra.iloc[0].to_dict())


# ---------------------------------------------------------------------------
# 3.5 — Reconciliação financeira por sistema de origem (RDT-11)
# ---------------------------------------------------------------------------
def reconciliacao(qa, eng):
    """Soma de valor_total do DW por sistema x total calculado direto na origem.

    Mesma definição nos dois lados (RDT-11):
      legado  : SUM(VALOR_TOTAL)                        (já inclui o frete)
      e-comm. : SUM(total_amount) + SUM(freight_amount) (total_amount não inclui o frete)
    """
    with eng.connect() as cx:
        origem = {
            "LEGADO": float(cx.execute(text('SELECT COALESCE(SUM("VALOR_TOTAL"),0) '
                                            'FROM stg.legado_pedidos')).scalar()),
            "ECOM": float(cx.execute(text('SELECT COALESCE(SUM(total_amount),0)'
                                          ' + COALESCE(SUM(freight_amount),0) '
                                          'FROM stg.ecom_orders')).scalar())}
        dw = {r[0]: float(r[1]) for r in cx.execute(text(
            "SELECT sistema_origem, SUM(valor_total) FROM dw.fato_pedido GROUP BY 1"))}
        n = {r[0]: int(r[1]) for r in cx.execute(text(
            "SELECT sistema_origem, COUNT(*) FROM dw.fato_pedido GROUP BY 1"))}
    linhas = []
    for sis in ("LEGADO", "ECOM"):
        dif = dw.get(sis, 0.0) - origem[sis]
        pct = abs(dif) / origem[sis] if origem[sis] else 0.0
        linhas.append({"sistema": sis, "pedidos": n.get(sis, 0),
                       "origem_R$": round(origem[sis], 2), "dw_R$": round(dw.get(sis, 0.0), 2),
                       "diferenca_R$": round(dif, 2), "diferenca_%": round(100 * pct, 6)})
        qa.entre(f"reconciliação {sis}: |DW − origem| / origem", pct, 0, TOLERANCIA_FINANCEIRA,
                 "tolerância de 0,1% (Entrega 3)")
    o, d = sum(origem.values()), sum(dw.values())
    pct = abs(d - o) / o if o else 0.0
    linhas.append({"sistema": "TOTAL", "pedidos": sum(n.values()), "origem_R$": round(o, 2),
                   "dw_R$": round(d, 2), "diferenca_R$": round(d - o, 2),
                   "diferenca_%": round(100 * pct, 6)})
    qa.entre("reconciliação TOTAL: |DW − origem| / origem", pct, 0, TOLERANCIA_FINANCEIRA)
    print("\nreconciliação financeira (valor_total):")
    print(pd.DataFrame(linhas).to_string(index=False))
    return linhas


if __name__ == "__main__":
    eng = eng_dw()
    with eng.connect() as cx:
        n_ped = cx.execute(text("SELECT COUNT(*) FROM dw.fato_pedido")).scalar()
    if n_ped == 0:
        raise SystemExit("dw.fato_pedido está vazio: rode antes "
                         "'python 02_transformacao.py' (e NÃO rode o esqueleto antigo do 03).")
    qa = Suite(etapa="03_carga_dw")
    resumo = {}
    resumo["quarentena"] = quarentena_itens_orfaos(qa, eng)
    resumo["sac"] = carrega_atendimentos(qa, eng)
    resumo["avaliacoes"] = carrega_avaliacoes(qa, eng)
    integridade(qa, eng)
    resumo["reconciliacao"] = reconciliacao(qa, eng)
    qa.registrar(eng)
    if qa.imprimir():
        print("Há asserções em falha. Isso não impede seguir, mas precisa "
              "aparecer no RDT com explicação.")
