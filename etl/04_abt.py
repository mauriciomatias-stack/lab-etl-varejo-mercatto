#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETAPA 4 — TABELA-BASE DE ANÁLISE (ABT) E O EXPERIMENTO DE VAZAMENTO

Pergunta analítica:
    dado o comportamento do cliente ATÉ 2025-06-30, ele comprará de novo
    entre 2025-07-01 e 2025-09-28?

Esta etapa produz DUAS versões da mesma tabela:

    --correta     todos os atributos calculados apenas com dados até a data
                  de corte. É a versão que vale.

    --vazamento   a mesma coisa, só que com dois ou três atributos calculados
                  sobre a base inteira. É a versão errada, que vocês vão
                  construir de propósito, rodar no Orange e comparar.

O objetivo do experimento não é ver um número maior. É medir a distância
entre um resultado que parece excelente e um resultado que é válido —
e descobrir que a diferença entre os dois cabe em uma linha de código.

    docker compose exec etl python 04_abt.py --correta
    docker compose exec etl python 04_abt.py --vazamento

Decisões desta etapa (ver RDT-14 e RDT-15):
  · as duas versões usam a MESMA função (`sql_atributos`); a única diferença é
    o filtro de data em dois atributos (`VAZAMENTO_FILTRO`) e a normalização;
  · tempo como cliente sai do PRIMEIRO PEDIDO, e não de dim_cliente, porque
    dt_primeiro_cadastro é posterior ao corte em 3.371 dos 22.106 clientes
    da base (15,25%) e posterior ao primeiro pedido em 16.410 (74%);
  · prazo de entrega só conta entregas ocorridas até o corte (456 pedidos
    anteriores ao corte foram entregues depois dele);
  · origem_unificacao e regra_pareamento (dim_cliente) NÃO entram: dependem
    do cruzamento de CPF entre os dois sistemas, que usa a base inteira.
"""

import argparse

import pandas as pd
from sqlalchemy import text

from config import DATA_CORTE, DATA_FIM_ALVO, eng_dw
from qualidade.assercoes import Suite


# ---------------------------------------------------------------------------
# ALVO — este está pronto, e é o único ponto do pipeline em que é legítimo
# olhar para o futuro, porque é exatamente isso que se quer prever.
# ---------------------------------------------------------------------------
SQL_ALVO = f"""
WITH base AS (
    SELECT DISTINCT c.cliente_sk
    FROM dw.dim_cliente c
    JOIN dw.fato_pedido p ON p.cliente_sk = c.cliente_sk
    WHERE p.data_pedido_sk <= DATE '{DATA_CORTE}'
),
compras_na_janela AS (
    SELECT DISTINCT p.cliente_sk
    FROM dw.fato_pedido p
    JOIN dw.dim_status s ON s.status_sk = p.status_sk
    WHERE p.data_pedido_sk >  DATE '{DATA_CORTE}'
      AND p.data_pedido_sk <= DATE '{DATA_FIM_ALVO}'
      AND s.status_conformado <> 'CANCELADO'   -- ajuste ao SEU mapeamento
)
SELECT b.cliente_sk,
       DATE '{DATA_CORTE}' AS data_corte,
       CASE WHEN j.cliente_sk IS NOT NULL THEN 1 ELSE 0 END AS alvo_recompra_90d
FROM base b
LEFT JOIN compras_na_janela j ON j.cliente_sk = b.cliente_sk
"""

# colunas numéricas que o Postgres devolve como Decimal
COLUNAS_NUMERICAS = ["recencia_dias", "frequencia_pedidos", "valor_monetario",
                     "ticket_medio", "prazo_medio_entrega", "taxa_cancelamento",
                     "categorias_distintas", "sistemas_distintos",
                     "qtd_atendimentos_sac", "nota_media_sac", "qtd_avaliacoes",
                     "nota_media_avaliacoes", "tempo_como_cliente"]


def sql_atributos(corte, vazamento=False):
    """SQL único dos atributos preditivos, um cliente por linha.

    Versão CORRETA: todo agregado tem  data <= corte.
    Versão com VAZAMENTO: recencia_dias e frequencia_pedidos perdem o filtro de
    data (enxergam a janela do alvo e tudo que vem depois). O resto é idêntico.
    """
    def filtro(vaza_aqui):
        return "TRUE" if (vazamento and vaza_aqui) else f"p.data_pedido_sk <= DATE '{corte}'"

    return f"""
WITH ped AS (
    SELECT p.pedido_sk, p.cliente_sk, p.sistema_origem, p.data_pedido_sk,
           p.data_entrega_sk, p.dias_entrega, p.valor_total,
           s.status_conformado, k.canal_conformado
    FROM dw.fato_pedido p
    JOIN dw.dim_status s ON s.status_sk = p.status_sk
    JOIN dw.dim_canal  k ON k.canal_sk  = p.canal_sk
),
base AS (                                   -- mesma base do alvo
    SELECT DISTINCT cliente_sk FROM ped p WHERE p.data_pedido_sk <= DATE '{corte}'
),
freq AS (                                   -- VAZAMENTO 1 e 2: frequência e recência
    SELECT p.cliente_sk,
           COUNT(*)               AS frequencia_pedidos,
           MAX(p.data_pedido_sk)  AS ultima_compra
    FROM ped p
    WHERE {filtro(True)} AND p.status_conformado <> 'CANCELADO'
    GROUP BY p.cliente_sk
),
val AS (
    SELECT p.cliente_sk,
           SUM(p.valor_total) AS valor_monetario,
           COUNT(*)           AS n_validos
    FROM ped p
    WHERE {filtro(False)} AND p.status_conformado <> 'CANCELADO'
    GROUP BY p.cliente_sk
),
canc AS (
    SELECT p.cliente_sk,
           COUNT(*) FILTER (WHERE p.status_conformado = 'CANCELADO')::numeric
               / COUNT(*) AS taxa_cancelamento,
           MIN(p.data_pedido_sk) AS primeiro_pedido,
           COUNT(DISTINCT p.sistema_origem) AS sistemas_distintos
    FROM ped p
    WHERE {filtro(False)}
    GROUP BY p.cliente_sk
),
prazo AS (                                  -- só entregas ocorridas até o corte
    SELECT p.cliente_sk, AVG(p.dias_entrega) AS prazo_medio_entrega
    FROM ped p
    WHERE {filtro(False)} AND p.data_entrega_sk <= DATE '{corte}'
          AND p.dias_entrega IS NOT NULL
    GROUP BY p.cliente_sk
),
canal AS (
    SELECT DISTINCT ON (cliente_sk) cliente_sk, canal_conformado AS canal_predominante
    FROM (SELECT p.cliente_sk, p.canal_conformado, COUNT(*) AS n,
                 MAX(p.data_pedido_sk) AS ult
          FROM ped p WHERE {filtro(False)}
          GROUP BY p.cliente_sk, p.canal_conformado) t
    ORDER BY cliente_sk, n DESC, ult DESC, canal_conformado
),
cats AS (                                   -- NAO_CLASSIFICADO e AMBIGUO não contam
    SELECT p.cliente_sk,
           COUNT(DISTINCT d.categoria_nivel1) AS categorias_distintas
    FROM ped p
    JOIN dw.fato_item_pedido i ON i.pedido_sk = p.pedido_sk
    JOIN dw.dim_produto d ON d.produto_sk = i.produto_sk
    WHERE {filtro(False)}
      AND d.categoria_nivel1 NOT IN ('NAO_CLASSIFICADO', 'AMBIGUO')
    GROUP BY p.cliente_sk
),
sac AS (                                    -- cliente do SAC vem só do CPF (RDT-13)
    SELECT a.cliente_sk,
           COUNT(*) FILTER (WHERE a.data_abertura_sk <= DATE '{corte}') AS qtd_atendimentos_sac,
           AVG(a.nota_satisfacao) FILTER (WHERE a.data_fechamento_sk <= DATE '{corte}')
               AS nota_media_sac
    FROM dw.fato_atendimento a
    WHERE a.cliente_sk IS NOT NULL
    GROUP BY a.cliente_sk
),
aval AS (                                   -- avaliação -> pedido -> cliente do pedido
    SELECT p.cliente_sk,
           COUNT(*) AS qtd_avaliacoes,
           AVG(v.nota) AS nota_media_avaliacoes
    FROM dw.fato_avaliacao v
    JOIN ped p ON p.pedido_sk = v.pedido_sk
    WHERE v.data_sk <= DATE '{corte}' AND {filtro(False)}
    GROUP BY p.cliente_sk
)
SELECT c.cliente_sk,
       NULLIF(TRIM(c.uf), '')                              AS uf,
       DATE '{corte}' - f.ultima_compra                    AS recencia_dias,
       COALESCE(f.frequencia_pedidos, 0)                   AS frequencia_pedidos,
       COALESCE(v.valor_monetario, 0)                      AS valor_monetario,
       v.valor_monetario / NULLIF(v.n_validos, 0)          AS ticket_medio,
       pz.prazo_medio_entrega,
       k.taxa_cancelamento,
       COALESCE(ct.categorias_distintas, 0)                AS categorias_distintas,
       cn.canal_predominante,
       k.sistemas_distintos,
       COALESCE(s.qtd_atendimentos_sac, 0)                 AS qtd_atendimentos_sac,
       s.nota_media_sac,
       COALESCE(a.qtd_avaliacoes, 0)                       AS qtd_avaliacoes,
       a.nota_media_avaliacoes,
       DATE '{corte}' - k.primeiro_pedido                  AS tempo_como_cliente
FROM dw.dim_cliente c
JOIN base b          ON b.cliente_sk = c.cliente_sk
LEFT JOIN freq  f    ON f.cliente_sk = c.cliente_sk
LEFT JOIN val   v    ON v.cliente_sk = c.cliente_sk
LEFT JOIN canc  k    ON k.cliente_sk = c.cliente_sk
LEFT JOIN prazo pz   ON pz.cliente_sk = c.cliente_sk
LEFT JOIN canal cn   ON cn.cliente_sk = c.cliente_sk
LEFT JOIN cats  ct   ON ct.cliente_sk = c.cliente_sk
LEFT JOIN sac   s    ON s.cliente_sk = c.cliente_sk
LEFT JOIN aval  a    ON a.cliente_sk = c.cliente_sk
"""


def _le_atributos(dw, vazamento):
    df = pd.read_sql(text(sql_atributos(DATA_CORTE, vazamento)), dw)
    for c in COLUNAS_NUMERICAS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def monta_atributos_corretos(dw):
    """
    Atributos preditivos, todos com  data <= corte.

    Tratamento de ausências (NaN não é zero):
      · contagens (frequência, atendimentos, avaliações, categorias) viram 0
        quando não há registro, porque "nenhum evento" é uma contagem de zero;
      · médias e razões (ticket, prazo, nota SAC, nota de avaliação, taxa de
        cancelamento) ficam NaN quando não há base para calcular, e o Orange
        as trata como valor ausente;
      · recência fica NaN para o cliente que só teve pedidos cancelados até o
        corte (não há "última compra" válida).
    Ressalva que vai para o RDT: dw.fato_pedido guarda o status observado na
    extração, e a origem não traz a data do cancelamento. Um pedido anterior
    ao corte pode ter sido cancelado depois dele, e isso não é detectável.
    """
    return _le_atributos(dw, vazamento=False)


def monta_atributos_com_vazamento(dw):
    """
    Mesmos atributos, com três vazamentos introduzidos DE PROPÓSITO:

      1. recencia_dias       perde o filtro de data: a última compra pode estar
                             na janela do alvo (ou depois); recência negativa
                             é o sintoma.
      2. frequencia_pedidos  perde o filtro de data: conta os pedidos da janela
                             do alvo, então quem recomprou tem frequência maior.
      3. ticket_medio        normalizado (z-score) com média e desvio de TODAS
                             as linhas, antes de qualquer partição treino/teste.
                             É o vazamento sutil: o dado de teste influencia a
                             escala que o treino enxerga.

    Todas as outras colunas são idênticas às da versão correta.
    """
    df = _le_atributos(dw, vazamento=True)
    media, desvio = df["ticket_medio"].mean(), df["ticket_medio"].std(ddof=0)
    df["ticket_medio"] = (df["ticket_medio"] - media) / desvio
    return df


def verifica_sem_vazamento(qa, dw, abt):
    """TODO 4.3 — recalcula, por fora do SQL de atributos, três atributos com o
    filtro de data explícito (em pandas) e compara com a ABT linha a linha."""
    corte = pd.Timestamp(str(DATA_CORTE))
    ped = pd.read_sql(text("""
        SELECT p.cliente_sk, p.data_pedido_sk, p.valor_total, s.status_conformado
        FROM dw.fato_pedido p JOIN dw.dim_status s ON s.status_sk = p.status_sk"""), dw)
    ped["data_pedido_sk"] = pd.to_datetime(ped["data_pedido_sk"])
    ped["valor_total"] = pd.to_numeric(ped["valor_total"])
    ok = ped[(ped["data_pedido_sk"] <= corte) & (ped["status_conformado"] != "CANCELADO")]
    ref = ok.groupby("cliente_sk").agg(freq=("valor_total", "size"),
                                       valor=("valor_total", "sum"),
                                       ultima=("data_pedido_sk", "max"))
    m = abt.set_index("cliente_sk").join(ref, how="left")
    freq_ref = m["freq"].fillna(0)
    valor_ref = m["valor"].fillna(0)
    rec_ref = (corte - m["ultima"]).dt.days
    qa.igual("sem vazamento: frequencia_pedidos x recálculo com filtro de data",
             int((m["frequencia_pedidos"] != freq_ref).sum()), 0,
             "toda linha deve bater com o recálculo restrito ao corte")
    qa.igual("sem vazamento: valor_monetario x recálculo com filtro de data (±0,01)",
             int(((m["valor_monetario"] - valor_ref).abs() > 0.01).sum()), 0)
    dif_rec = (m["recencia_dias"].fillna(-1) != rec_ref.fillna(-1)).sum()
    qa.igual("sem vazamento: recencia_dias x recálculo com filtro de data",
             int(dif_rec), 0)
    qa.igual("sem vazamento: recência negativa (última compra depois do corte)",
             int((m["recencia_dias"] < 0).sum()), 0)


def grava(df, dw, sufixo):
    tabela = "recompra_90d" if sufixo == "correta" else "recompra_90d_vazamento"
    with dw.begin() as cx:
        cx.execute(text("CREATE SCHEMA IF NOT EXISTS abt"))
    df.to_sql(tabela, dw, schema="abt", if_exists="replace",
              index=False, method="multi", chunksize=5_000)
    print(f"abt.{tabela}: {len(df)} linhas, {df.shape[1]} colunas")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--correta", action="store_true")
    ap.add_argument("--vazamento", action="store_true")
    args = ap.parse_args()
    if not (args.correta or args.vazamento):
        ap.error("escolha --correta ou --vazamento")

    dw = eng_dw()
    alvo = pd.read_sql(text(SQL_ALVO), dw)
    print(f"alvo: {len(alvo)} clientes | taxa de recompra = "
          f"{alvo['alvo_recompra_90d'].mean():.3f}")

    qa = Suite(etapa="04_abt_" + ("correta" if args.correta else "vazamento"))
    qa.entre("taxa do alvo", float(alvo["alvo_recompra_90d"].mean()), 0.02, 0.60,
             "classe extremamente desbalanceada indica erro na janela")
    qa.unico("cliente_sk no alvo", alvo["cliente_sk"])
    with dw.connect() as cx:
        n_base = cx.execute(text(
            f"SELECT COUNT(DISTINCT cliente_sk) FROM dw.fato_pedido "
            f"WHERE data_pedido_sk <= DATE '{DATA_CORTE}'")).scalar()
    qa.igual("alvo: clientes x clientes com pedido até o corte (contagem em SQL)",
             len(alvo), n_base)

    atributos = (monta_atributos_corretos(dw) if args.correta
                 else monta_atributos_com_vazamento(dw))
    qa.unico("cliente_sk nos atributos", atributos["cliente_sk"])

    antes = len(alvo)
    abt = alvo.merge(atributos, on="cliente_sk", how="left")
    qa.cardinalidade_preservada("junção alvo x atributos", antes, len(abt))
    qa.igual("junção alvo x atributos: clientes sem atributos", int(
        (~alvo["cliente_sk"].isin(atributos["cliente_sk"])).sum()), 0)
    qa.igual("data_corte única e igual ao parâmetro",
             int(abt["data_corte"].astype(str).str[:10].ne(str(DATA_CORTE)[:10]).sum()), 0)

    # TODO 4.3 — roda nas duas versões: na correta deve passar; na versão com
    # vazamento deve FALHAR, e essa falha é a prova de que a asserção enxerga o erro.
    verifica_sem_vazamento(qa, dw, abt)

    grava(abt, dw, "correta" if args.correta else "vazamento")
    qa.registrar(dw)
    qa.imprimir()
