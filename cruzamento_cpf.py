"""
Cruzamento de chaves entre legado (MySQL) e e-commerce (PostgreSQL).
Somente leitura. Roda dentro do contêiner etl.

Uso (PowerShell, na pasta do projeto):
  Get-Content cruzamento_cpf.py | podman compose exec -T etl python - | Out-File -Encoding utf8 cruzamento.txt
"""
import re
import pandas as pd
from sqlalchemy import create_engine, text

# --- conexões (nomes dos serviços dentro da rede do compose) ---
eng_my = create_engine(
    "mysql+pymysql://etl_ro:etl_ro@mysql_legado:3306/loja_legado?charset=latin1"
)


def eng_postgres():
    for driver in ("postgresql+psycopg2", "postgresql+psycopg"):
        try:
            e = create_engine(f"{driver}://etl_ro:etl_ro@pg_ecommerce:5432/ecommerce")
            with e.connect() as cx:
                cx.execute(text("SELECT 1"))
            return e
        except Exception as ex:  # tenta o próximo driver
            ultimo = ex
    raise ultimo


eng_pg = eng_postgres()

# --- leitura ---
leg = pd.read_sql(text("SELECT ID_CLIENTE, NOME, CPF, EMAIL, TELEFONE FROM CLIENTES"), eng_my)
eco = pd.read_sql(text("SELECT customer_id, full_name, tax_id, email, phone FROM shop.customer"), eng_pg)


def so_digitos(s):
    return re.sub(r"\D", "", s) if isinstance(s, str) else None


def cpf_valido(c):
    if not c or len(c) != 11 or c == c[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(c[i]) * (n + 1 - i) for i in range(n))
        dv = (soma * 10) % 11 % 10
        if dv != int(c[n]):
            return False
    return True


leg["cpf_n"] = leg["CPF"].map(so_digitos).replace("", None)
eco["cpf_n"] = eco["tax_id"].map(so_digitos).replace("", None)
leg["tel_n"] = leg["TELEFONE"].map(so_digitos)
eco["tel_n"] = eco["phone"].map(so_digitos)


def pct(a, b):
    return f"{a} ({100 * a / b:.2f}%)" if b else f"{a}"


print("=" * 60)
print("CPF — cobertura por sistema")
print("=" * 60)
for nome, df in (("legado", leg), ("ecommerce", eco)):
    n = len(df)
    com = df["cpf_n"].notna().sum()
    validos = df["cpf_n"].dropna().map(cpf_valido).sum()
    print(f"{nome}: clientes={n} | com CPF={pct(com, n)} | "
          f"CPFs distintos={df['cpf_n'].nunique()} | "
          f"com dígito verificador válido={pct(int(validos), int(com))}")

s_leg, s_eco = set(leg["cpf_n"].dropna()), set(eco["cpf_n"].dropna())
inter = s_leg & s_eco
print()
print("=" * 60)
print("CPF — interseção entre os sistemas")
print("=" * 60)
print(f"CPFs distintos no legado:    {len(s_leg)}")
print(f"CPFs distintos no ecommerce: {len(s_eco)}")
print(f"CPFs nos dois sistemas:      {len(inter)}")
print(f"  = {100 * len(inter) / len(s_leg):.2f}% dos CPFs do legado")
print(f"  = {100 * len(inter) / len(s_eco):.2f}% dos CPFs do ecommerce")
inv = sum(1 for c in inter if not cpf_valido(c))
print(f"Na interseção, CPFs com dígito verificador inválido: {pct(inv, len(inter))}")

print()
print("=" * 60)
print("Duplicidade de CPF dentro de cada sistema")
print("=" * 60)
for nome, df in (("legado", leg), ("ecommerce", eco)):
    c = df["cpf_n"].dropna().value_counts()
    print(f"{nome}: CPFs que aparecem em mais de 1 cliente = {(c > 1).sum()} "
          f"(linhas envolvidas: {int(c[c > 1].sum())})")

print()
print("=" * 60)
print("Outras chaves candidatas (interseção)")
print("=" * 60)
e_leg = set(leg["EMAIL"].dropna().str.lower().str.strip())
e_eco = set(eco["email"].dropna().str.lower().str.strip())
print(f"e-mail em comum: {len(e_leg & e_eco)}")
t_leg, t_eco = set(leg["tel_n"].dropna()), set(eco["tel_n"].dropna())
print(f"telefone (só dígitos) em comum: {len(t_leg & t_eco)}")
# telefone sem o prefixo +55 do e-commerce
t_eco2 = {t[2:] if t.startswith("55") and len(t) > 11 else t for t in t_eco}
print(f"telefone em comum (e-commerce sem prefixo 55): {len(t_leg & t_eco2)}")

print()
print("Pronto. Guarde esta saída: ela alimenta a seção a) do relatório.")
