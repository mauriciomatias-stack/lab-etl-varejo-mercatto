#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETAPA 1 — EXTRAÇÃO

Traz as quatro fontes para o esquema `stg` do DW SEM transformar significado.

Regras da camada stg:
  · nomes de coluna espelham a origem (por isso, no Postgres, as colunas do
    legado ficam em MAIÚSCULAS e precisam de aspas duplas em SQL: "NOME");
  · nenhum valor é descartado, corrigido ou reinterpretado;
  · metadados de procedência acrescentados:
        _fonte           de onde veio (legado, ecom, sac, avaliacoes)
        _extraido_em     momento da extração (UTC)
        _linha_origem    registro lógico no arquivo (só SAC e avaliações; nas
                         tabelas de banco a chave primária já identifica a linha)

RDT-01 — carga completa, não incremental. Cada execução recria as tabelas stg
(`if_exists="replace"`), o que torna o pipeline reexecutável do zero. Motivo:
nenhuma tabela de origem tem coluna de data de alteração (só data de pedido,
de entrega e de cadastro), e o status de um pedido muda sem deixar rastro, então
um corte incremental por data perderia essas mudanças. O SAC é exportação manual
mensal concatenada, com linhas repetidas: também não há critério incremental
seguro.

    docker compose exec etl python 01_extracao.py
"""

import csv
import json
import re
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from config import (ARQ_AVALIACOES, ARQ_SAC, eng_dw, eng_mysql, eng_origem)
from qualidade.assercoes import Suite

AGORA = datetime.now(timezone.utc)
LOTE = 50_000          # leia em blocos: a base não cabe confortavelmente na RAM

# (tabela na origem, tabela na stg, chave para ordenar, coluna monetária a somar)
TABELAS_MYSQL = [
    ("CLIENTES",     "legado_clientes",     "ID_CLIENTE", None),
    ("PRODUTOS",     "legado_produtos",     "ID_PRODUTO", None),
    ("PEDIDOS",      "legado_pedidos",      "ID_PEDIDO",  "VALOR_TOTAL"),
    ("ITENS_PEDIDO", "legado_itens_pedido", "ID_ITEM",    None),
]
TABELAS_PG = [
    ("category",   "ecom_category",   "category_id",   None),
    ("customer",   "ecom_customer",   "customer_id",   None),
    ("product",    "ecom_product",    "product_id",    None),
    ("orders",     "ecom_orders",     "order_id",      "total_amount"),
    ("order_item", "ecom_order_item", "order_item_id", None),
]

# Texto UTF-8 lido como latin-1 vira "Ã§", "Ã¡", "Ã©"... (Ã/Â seguido de um
# caractere de 0x80 a 0xBF). "JOÃO" e "SÃO PAULO" legítimos não casam.
MOJIBAKE = re.compile("[ÃÂ][\u0080-¿]")


def grava_stg(df, tabela, engine, primeiro_bloco):
    df["_fonte"] = tabela.split("_")[0]
    df["_extraido_em"] = AGORA
    df.to_sql(tabela, engine, schema="stg",
              if_exists="replace" if primeiro_bloco else "append",
              index=False, method="multi",
              chunksize=max(200, 30_000 // len(df.columns)))


def conta_mojibake(df):
    n = 0
    for col in df.columns:
        s = df[col]
        if (pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s)
                or pd.api.types.is_datetime64_any_dtype(s)):
            continue
        n += int(s.map(
            lambda v: isinstance(v, str) and bool(MOJIBAKE.search(v))).sum())
    return n


def _extrai_sql(eng, dw, qa, tabelas, schema=None, confere_acentos=False):
    """Copia cada tabela para a stg em blocos e confere contagem (e soma)."""
    prefixo = f"{schema}." if schema else ""
    for tab_origem, tab_stg, pk, col_soma in tabelas:
        rotulo = f"{prefixo}{tab_origem}"
        lidas = acentos_ruins = 0
        with eng.connect() as cx:
            esperado = cx.execute(text(f"SELECT COUNT(*) FROM {rotulo}")).scalar()
            soma_origem = None
            if col_soma:
                soma_origem = float(cx.execute(
                    text(f"SELECT SUM({col_soma}) FROM {rotulo}")).scalar())
            consulta = text(f"SELECT * FROM {rotulo} ORDER BY {pk}")
            primeiro = True
            for bloco in pd.read_sql(consulta, cx, chunksize=LOTE):
                lidas += len(bloco)
                if confere_acentos:
                    acentos_ruins += conta_mojibake(bloco)
                grava_stg(bloco, tab_stg, dw, primeiro)
                primeiro = False

        with dw.connect() as cx:
            n_stg = cx.execute(text(f"SELECT COUNT(*) FROM stg.{tab_stg}")).scalar()
            soma_stg = None
            if col_soma:
                soma_stg = float(cx.execute(
                    text(f'SELECT SUM("{col_soma}") FROM stg.{tab_stg}')).scalar())

        qa.igual(f"{rotulo}: linhas lidas x origem", lidas, esperado,
                 "a leitura não pode perder linhas")
        qa.igual(f"{rotulo}: linhas na stg x origem", n_stg, esperado,
                 "a gravação não pode perder nem duplicar linhas")
        if col_soma:
            qa.entre(f"{rotulo}.{col_soma}: soma na stg x origem", soma_stg,
                     soma_origem - 0.01, soma_origem + 0.01,
                     "o valor monetário não pode mudar na extração")
        if confere_acentos:
            qa.igual(f"{rotulo}: textos com acento corrompido", acentos_ruins, 0,
                     "indica leitura latin1 como utf8 (ver config.py)")
        print(f"  stg.{tab_stg}: {n_stg} linhas")


def extrai_mysql(qa):
    print("legado (MySQL, latin1)")
    _extrai_sql(eng_mysql(), eng_dw(), qa, TABELAS_MYSQL, confere_acentos=True)


def extrai_postgres(qa):
    # placed_at / delivered_at são TIMESTAMPTZ (UTC) e ficam assim na stg.
    # A padronização de fuso é decisão da etapa 2 (RDT-09), não desta.
    print("e-commerce (PostgreSQL, schema shop)")
    _extrai_sql(eng_origem(), eng_dw(), qa, TABELAS_PG, schema="shop")


# ---------------------------------------------------------------------------
# SAC (CSV)
# ---------------------------------------------------------------------------
def le_sac():
    # dtype=str: tudo texto, conversão é transformação.
    # keep_default_na=False: sem isso o pandas transforma o texto 'N/A' (e
    # outros) em nulo, destruindo uma sentinela que a etapa 2 precisa ver.
    df = pd.read_csv(ARQ_SAC, sep=";", encoding="latin-1", dtype=str,
                     keep_default_na=False)
    df["_linha_origem"] = range(1, len(df) + 1)   # registro lógico, sem o cabeçalho
    return df


def conta_registros_csv(caminho, encoding, sep):
    """Contagem independente do pandas: o módulo csv também respeita as quebras
    de linha dentro de campos entre aspas."""
    with open(caminho, encoding=encoding, newline="") as f:
        return sum(1 for linha in csv.reader(f, delimiter=sep) if linha) - 1


def extrai_sac(qa):
    print("SAC (CSV latin-1)")
    df = le_sac()
    dw = eng_dw()
    grava_stg(df, "sac_atendimentos", dw, True)
    esperado = conta_registros_csv(ARQ_SAC, "latin-1", ";")
    with dw.connect() as cx:
        n_stg = cx.execute(text("SELECT COUNT(*) FROM stg.sac_atendimentos")).scalar()
        stg = pd.read_sql(text("SELECT protocolo, origem_sistema "
                               "FROM stg.sac_atendimentos"), cx)
    qa.igual("SAC: registros na stg x arquivo", n_stg, esperado,
             "contagem independente feita com o módulo csv")
    qa.sem_nulos("SAC: protocolo preenchido", stg["protocolo"].replace("", pd.NA))
    qa.dominio("SAC: origem_sistema", stg["origem_sistema"], {"LEGADO", "ECOM"},
               "sem origem válida o pedido não pode ser ligado (decisão 6)")
    print(f"  stg.sac_atendimentos: {n_stg} linhas")


# ---------------------------------------------------------------------------
# Avaliações (NDJSON)
# ---------------------------------------------------------------------------
def _txt(v):
    """Na stg tudo vira texto; só o valor ausente/nulo vira NULL."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def le_avaliacoes():
    """Devolve (DataFrame, linhas inválidas, linhas não vazias do arquivo).

    Chave ausente e chave nula NÃO são a mesma coisa. Para não perder a
    distinção, a stg guarda `_chaves` (as chaves presentes no objeto) e
    `rating_tipo` (int, str, NoneType ou ausente).
    """
    registros, invalidas, nao_vazias = [], 0, 0
    with open(ARQ_AVALIACOES, encoding="utf-8") as f:
        for n, bruto in enumerate(f, start=1):
            bruto = bruto.strip()
            if not bruto:
                continue
            nao_vazias += 1
            try:
                o = json.loads(bruto)
            except json.JSONDecodeError:
                invalidas += 1
                continue
            if not isinstance(o, dict):
                invalidas += 1
                continue
            ordem = o["order"] if isinstance(o.get("order"), dict) else {}
            prod = o["product"] if isinstance(o.get("product"), dict) else {}
            registros.append({
                "review_id": _txt(o.get("review_id")),
                "order_ref": _txt(ordem.get("ref")),
                "order_channel": _txt(ordem.get("channel")),
                "product_sku": _txt(prod.get("sku")),
                "submitted_at": _txt(o.get("submitted_at")),
                "rating": _txt(o.get("rating")),
                "rating_tipo": ("ausente" if "rating" not in o
                                else type(o["rating"]).__name__),
                "title": _txt(o.get("title")),
                "comment": _txt(o.get("comment")),
                "verified_purchase": _txt(o.get("verified_purchase")),
                "helpful_votes": _txt(o.get("helpful_votes")),
                "_chaves": ",".join(o.keys()),
                "_linha_origem": n,
            })
    return pd.DataFrame(registros), invalidas, nao_vazias


def extrai_avaliacoes(qa):
    print("avaliações (NDJSON)")
    df, invalidas, nao_vazias = le_avaliacoes()
    dw = eng_dw()
    grava_stg(df, "avaliacoes_marketplace", dw, True)
    with dw.connect() as cx:
        n_stg = cx.execute(text(
            "SELECT COUNT(*) FROM stg.avaliacoes_marketplace")).scalar()
        stg = pd.read_sql(text("SELECT review_id FROM stg.avaliacoes_marketplace"), cx)
    qa.igual("avaliações: linhas JSON inválidas", invalidas, 0,
             "linha que não vira objeto JSON não pode sumir em silêncio")
    qa.igual("avaliações: registros na stg x arquivo", n_stg, nao_vazias,
             "uma linha não vazia do arquivo = um registro na stg")
    qa.sem_nulos("avaliações: review_id preenchido", stg["review_id"])
    qa.unico("avaliações: review_id", stg["review_id"])
    print(f"  stg.avaliacoes_marketplace: {n_stg} linhas")


if __name__ == "__main__":
    qa = Suite(etapa="01_extracao")
    extrai_mysql(qa)
    extrai_postgres(qa)
    extrai_sac(qa)
    extrai_avaliacoes(qa)

    qa.registrar(eng_dw())
    qa.imprimir()
