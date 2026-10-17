"""
Validação das chaves de junção — somente leitura. Roda dentro do contêiner etl.

Uso (PowerShell, na pasta do projeto):
  Get-Content validacao_chaves.py | podman compose exec -T -e PYTHONIOENCODING=utf-8 etl python - | Out-File -Encoding utf8 validacao.txt
"""
import json
import os
import re
import unicodedata

import pandas as pd
from sqlalchemy import create_engine, text

eng_my = create_engine(
    "mysql+pymysql://etl_ro:etl_ro@mysql_legado:3306/loja_legado?charset=latin1"
)


def eng_postgres():
    ultimo = None
    for driver in ("postgresql+psycopg2", "postgresql+psycopg"):
        try:
            e = create_engine(f"{driver}://etl_ro:etl_ro@pg_ecommerce:5432/ecommerce")
            with e.connect() as cx:
                cx.execute(text("SELECT 1"))
            return e
        except Exception as ex:
            ultimo = ex
    raise ultimo


eng_pg = eng_postgres()


def so_digitos(s):
    return re.sub(r"\D", "", s) if isinstance(s, str) else None


def norm_nome(s):
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def pct(a, b):
    return f"{a} ({100 * a / b:.2f}%)" if b else f"{a}"


def achar(*caminhos):
    for c in caminhos:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"não achei nenhum destes: {caminhos}")


# ------------------------------------------------------------------
print("=" * 60)
print("1) CPF igual => mesma pessoa? (comparação de nomes nos pares)")
print("=" * 60)
leg = pd.read_sql(text("SELECT ID_CLIENTE, NOME, CPF FROM CLIENTES"), eng_my)
eco = pd.read_sql(text("SELECT customer_id, full_name, tax_id FROM shop.customer"), eng_pg)
leg["cpf_n"] = leg["CPF"].map(so_digitos).replace("", None)
eco["cpf_n"] = eco["tax_id"].map(so_digitos).replace("", None)

pares = leg.dropna(subset=["cpf_n"]).merge(eco.dropna(subset=["cpf_n"]), on="cpf_n")
pares["n_leg"] = pares["NOME"].map(norm_nome)
pares["n_eco"] = pares["full_name"].map(norm_nome)
pares["nome_igual"] = pares["n_leg"] == pares["n_eco"]
pares["mesmo_primeiro_ultimo"] = pares.apply(
    lambda r: (r.n_leg.split()[:1] + r.n_leg.split()[-1:])
    == (r.n_eco.split()[:1] + r.n_eco.split()[-1:]),
    axis=1,
)
print(f"pares (cliente legado, cliente ecommerce) com mesmo CPF: {len(pares)}")
print(f"  nome idêntico (sem acento/caixa):       {pct(int(pares.nome_igual.sum()), len(pares))}")
print(f"  mesmo primeiro e último nome:           {pct(int(pares.mesmo_primeiro_ultimo.sum()), len(pares))}")
dif = pares[~pares.mesmo_primeiro_ultimo][["cpf_n", "NOME", "full_name"]].head(8)
if len(dif):
    print("  exemplos de pares com nome diferente:")
    print(dif.to_string(index=False))

# ------------------------------------------------------------------
print()
print("=" * 60)
print("2) IDs de pedido existem nos dois sistemas?")
print("=" * 60)
ids_leg = set(pd.read_sql(text("SELECT ID_PEDIDO FROM PEDIDOS"), eng_my)["ID_PEDIDO"])
ids_eco = set(pd.read_sql(text("SELECT order_id FROM shop.orders"), eng_pg)["order_id"])
print(f"pedidos legado: {len(ids_leg)} | pedidos ecommerce: {len(ids_eco)}")
print(f"números de pedido presentes nos DOIS sistemas: {len(ids_leg & ids_eco)}")

# ------------------------------------------------------------------
print()
print("=" * 60)
print("3) SAC -> pedido")
print("=" * 60)
caminho_sac = achar("/dados/atendimentos_sac.csv", "/app/dados/atendimentos_sac.csv",
                    "dados/atendimentos_sac.csv")
sac = pd.read_csv(caminho_sac, sep=";", encoding="latin-1", dtype=str, keep_default_na=False)
sac["id_n"] = pd.to_numeric(sac["id_pedido_origem"], errors="coerce")
print(f"linhas: {len(sac)} | id_pedido_origem não numérico: {int(sac.id_n.isna().sum())}")
for sistema, ids in (("LEGADO", ids_leg), ("ECOM", ids_eco)):
    sub = sac[sac["origem_sistema"] == sistema]
    liga = sub["id_n"].isin(ids).sum()
    print(f"  {sistema}: {len(sub)} atendimentos | ligam a pedido existente: {pct(int(liga), len(sub))}")
# o que aconteceria ignorando origem_sistema
liga_errada = sac[sac["origem_sistema"] == "LEGADO"]["id_n"].isin(ids_eco).sum()
print(f"  atendimentos LEGADO cujo número também existe como pedido ECOM "
      f"(ambiguidade se ignorar origem_sistema): {liga_errada}")

# CPF do SAC contra os CPFs dos clientes
sac["cpf_n"] = sac["cpf_cliente"].map(so_digitos).replace("", None)
todos = set(leg["cpf_n"].dropna()) | set(eco["cpf_n"].dropna())
com_cpf = sac["cpf_n"].notna().sum()
print(f"  SAC com CPF preenchido: {pct(int(com_cpf), len(sac))}")
print(f"  SAC com CPF que existe em algum cliente: {pct(int(sac['cpf_n'].isin(todos).sum()), len(sac))}")

# ------------------------------------------------------------------
print()
print("=" * 60)
print("4) Avaliações -> pedido e produto")
print("=" * 60)
caminho_av = achar("/dados/avaliacoes_marketplace.json", "/app/dados/avaliacoes_marketplace.json",
                   "dados/avaliacoes_marketplace.json")
refs, skus = [], []
with open(caminho_av, encoding="utf-8") as f:
    for linha in f:
        linha = linha.strip()
        if not linha:
            continue
        try:
            o = json.loads(linha)
        except Exception:
            continue
        refs.append((o.get("order") or {}).get("ref"))
        skus.append((o.get("product") or {}).get("sku"))
print(f"objetos lidos: {len(refs)}")

padrao = re.compile(r"^([A-Za-z]+)-(\d+)$")
prefixos, ok_leg, ok_eco, n_leg, n_eco, fora_formato = {}, 0, 0, 0, 0, 0
for r in refs:
    m = padrao.match(r) if isinstance(r, str) else None
    if not m:
        fora_formato += 1
        continue
    p, num = m.group(1).upper(), int(m.group(2))
    prefixos[p] = prefixos.get(p, 0) + 1
    if p == "ECOM":
        n_eco += 1
        ok_eco += num in ids_eco
    elif p.startswith("LEG"):
        n_leg += 1
        ok_leg += num in ids_leg
print(f"prefixos de order.ref: {prefixos} | fora do formato esperado: {fora_formato}")
print(f"  ECOM: {pct(int(ok_eco), n_eco)} ligam a pedido existente")
print(f"  LEG:  {pct(int(ok_leg), n_leg)} ligam a pedido existente")

skus_eco = set(pd.read_sql(text("SELECT sku FROM shop.product"), eng_pg)["sku"])
tem_sku = [s for s in skus if isinstance(s, str)]
print(f"  product.sku preenchido: {pct(len(tem_sku), len(skus))}")
print(f"  product.sku existe em shop.product: {pct(sum(s in skus_eco for s in tem_sku), len(tem_sku))}")

print()
print("Pronto. Guarde esta saída.")
