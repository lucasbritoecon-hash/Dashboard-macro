"""
Busca SELIC (SGS 432 - Taxa Selic definida pelo COPOM, % a.a.) e IPCA mensal
(SGS 433) no SGS/BCB e monta a serie anual "Evolucao de Taxa de Juros Real"
(SELIC de fim de ano x IPCA acumulado no ano), salvando em
data/juros_real_data.json no mesmo padrao dos outros paineis do CIEM.

Metodologia (replica o grafico "Cenario Economico e Financeiro - Mercados"
do CIEM):
  - selic_ano   = ultima leitura da serie 432 disponivel no ano (idealmente
                  31/12, mas cai pro ultimo valor do ano se o ano ainda nao
                  fechou -- ver abaixo).
  - ipca_ano    = IPCA acumulado no ano (produto dos fatores mensais de
                  jan a dez, serie 433). Pro ano corrente/ainda em curso,
                  acumula so até o ultimo mes com dado disponivel (dado mais
                  recente parcial, sem projecao) -- decisao confirmada com o
                  Lucas: nao projetar via Focus aqui, so usar o realizado.
  - juro_real   = (1 + selic_ano) / (1 + ipca_ano) - 1   (Fisher exato)

Anos fechados (todos os 12 meses de IPCA realizados) ficam marcados como
`fechado: true`; o ano corrente (parcial) fica `fechado: false` pra a pagina
poder sinalizar visualmente (ex.: marcador tracejado, como no grafico
original em Excel do CIEM).

Rodar:
    python scripts/fetch_juros_real.py

Gerado por GitHub Actions 1x por dia (.github/workflows/update-data.yml).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SGS_SELIC_ANUAL = 432   # Taxa Selic definida pelo COPOM (% a.a.), serie diaria/por reuniao
SGS_IPCA_MENSAL = 433   # IPCA - Variacao mensal (%)

ANO_INICIAL = 2013  # mesmo ponto de partida do grafico original do CIEM

BASE_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "juros_real_data.json"


def _fetch_janela(codigo, data_inicial, data_final, tries=3, wait=8):
    """Busca uma unica janela (<=10 anos) da serie, com retentativas."""
    url = BASE_URL.format(codigo=codigo)
    params = {"formato": "json", "dataInicial": data_inicial, "dataFinal": data_final}
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[serie {codigo}] {data_inicial}-{data_final} tentativa {attempt}/{tries} falhou: {e}")
            if attempt < tries:
                import time
                time.sleep(wait)
    raise RuntimeError(f"falha definitiva na serie {codigo} ({data_inicial}-{data_final}): {last_err}")


def fetch_series(codigo, data_inicial, tries=3, wait=8):
    """Busca a serie inteira desde `data_inicial` (dd/mm/aaaa) ate hoje.

    Desde 26/03/2025 o SGS/BCB exige dataInicial+dataFinal e limita cada
    consulta a no maximo 10 anos (senao retorna erro, ex.: 406). Por isso
    quebramos o periodo em janelas de <=10 anos e concatenamos o resultado.
    """
    dia, mes, ano = (int(x) for x in data_inicial.split("/"))
    inicio = datetime(ano, mes, dia)
    hoje = datetime.now()

    pontos = []
    janela_ini = inicio
    while janela_ini <= hoje:
        # janela de ate 10 anos menos 1 dia (limite do BCB e' "ate 10 anos")
        janela_fim = min(
            datetime(janela_ini.year + 10, janela_ini.month, janela_ini.day) - timedelta(days=1),
            hoje,
        )
        pontos.extend(
            _fetch_janela(
                codigo,
                janela_ini.strftime("%d/%m/%Y"),
                janela_fim.strftime("%d/%m/%Y"),
                tries=tries,
                wait=wait,
            )
        )
        janela_ini = janela_fim + timedelta(days=1)

    return pontos


def selic_por_ano(pontos):
    """Ultima leitura da SELIC (% a.a.) disponivel em cada ano-calendario."""
    por_ano = {}
    for item in pontos:
        if item.get("valor") in (None, ""):
            continue
        dia, mes, ano = item["data"].split("/")
        data_dt = datetime(int(ano), int(mes), int(dia))
        valor = float(str(item["valor"]).replace(",", "."))
        ano_i = int(ano)
        atual = por_ano.get(ano_i)
        if atual is None or data_dt > atual[0]:
            por_ano[ano_i] = (data_dt, valor, mes == "12" and int(dia) >= 29)
    # retorna {ano: (valor_pct, fechado_no_fim_do_ano)}
    return {ano: (v, fechado) for ano, (_, v, fechado) in por_ano.items()}


def ipca_acumulado_por_ano(pontos):
    """IPCA acumulado no ano (composto jan->dez) para cada ano-calendario.
    Ano com os 12 meses -> fechado=True. Ano parcial -> acumula so o que
    tiver, fechado=False."""
    mensal_por_ano = {}
    for item in pontos:
        if item.get("valor") in (None, ""):
            continue
        _, mes, ano = item["data"].split("/")
        ano_i = int(ano)
        valor = float(str(item["valor"]).replace(",", ".")) / 100
        mensal_por_ano.setdefault(ano_i, {})[int(mes)] = valor

    resultado = {}
    for ano_i, meses in mensal_por_ano.items():
        fator = 1.0
        n_meses = 0
        for mes in sorted(meses):
            fator *= (1 + meses[mes])
            n_meses += 1
        resultado[ano_i] = (fator - 1, n_meses == 12)
    return resultado


def montar_series(selic_ano, ipca_ano):
    anos = sorted(set(selic_ano) & set(ipca_ano))
    anos = [a for a in anos if a >= ANO_INICIAL]

    out = {"anos": [], "selic_pct": [], "ipca_pct": [], "juro_real_pct": [], "ano_fechado": []}
    for ano in anos:
        selic_v, selic_fechado = selic_ano[ano]
        ipca_v, ipca_fechado = ipca_ano[ano]
        fechado = selic_fechado and ipca_fechado
        juro_real = (1 + selic_v / 100) / (1 + ipca_v) - 1

        out["anos"].append(ano)
        out["selic_pct"].append(round(selic_v, 2))
        out["ipca_pct"].append(round(ipca_v * 100, 2))
        out["juro_real_pct"].append(round(juro_real * 100, 2))
        out["ano_fechado"].append(fechado)
    return out


def load_previous():
    if OUTPUT_PATH.exists():
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"aviso: nao consegui ler o JSON anterior ({e})")
    return None


def main():
    previous = load_previous()
    had_failure = False

    try:
        selic_raw = fetch_series(SGS_SELIC_ANUAL, f"01/01/{ANO_INICIAL}")
        ipca_raw = fetch_series(SGS_IPCA_MENSAL, f"01/01/{ANO_INICIAL}")
        selic_ano = selic_por_ano(selic_raw)
        ipca_ano = ipca_acumulado_por_ano(ipca_raw)
        series = montar_series(selic_ano, ipca_ano)
        print(f"[juros_real] OK - anos {series['anos'][0]}-{series['anos'][-1]}"
              f" ({'fechado' if series['ano_fechado'][-1] else 'parcial'} o ultimo)")
    except Exception as e:  # noqa: BLE001
        print(f"[juros_real] ERRO DEFINITIVO: {e}")
        had_failure = True
        if previous and previous.get("series"):
            series = previous["series"]
            print(f"[juros_real] usando cache anterior ({len(series.get('anos', []))} anos)")
        else:
            series = {"anos": [], "selic_pct": [], "ipca_pct": [], "juro_real_pct": [], "ano_fechado": []}
            print("[juros_real] sem cache anterior disponivel, salvando vazio")

    payload = {
        "series": series,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "had_failure": had_failure,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\nSalvo em {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
