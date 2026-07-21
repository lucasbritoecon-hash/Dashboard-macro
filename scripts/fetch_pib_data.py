"""
Busca PIB e FBCF no SIDRA/IBGE (tabela 6612), calcula variacao real anual
e hiato do produto via filtro HP, e salva em data/activity_data.json.

Segue o mesmo padrao de robustez do fetch_bcb_data.py: se a coleta falhar,
mantem os dados anteriores (cache) para a pagina nunca ficar sem dado -- so
potencialmente desatualizada.

Rodado pelo GitHub Actions (.github/workflows/update-data.yml).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from statsmodels.tsa.filters.hp_filter import hpfilter

SIDRA_BASE = "https://apisidra.ibge.gov.br/values"

# mesmas queries do script original (tabela 6612 - Series Trimestrais, valores encadeados)
API_PIB = "/t/6612/n1/all/v/9318/p/all/c11255/90707/d/v9318%202"
API_FBCF = "/t/6612/n1/all/v/all/p/all/c11255/93406/d/v9318%202"

MES_INICIO_TRIMESTRE = {1: "01", 2: "04", 3: "07", 4: "10"}

# raiz do repo = pasta pai de scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "activity_data.json"


def get_sidra(api_path: str, tries: int = 3, wait: int = 8) -> pd.DataFrame:
    """
    Busca uma tabela do SIDRA com retentativas. O SIDRA devolve uma lista
    de dicionarios: o primeiro item e um "cabecalho" mapeando os codigos
    tecnicos (V, D3N, etc.) para os nomes legiveis (Valor, Trimestre, etc.).
    """
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(SIDRA_BASE + api_path, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
            header = raw[0]
            df = pd.DataFrame(raw[1:])
            return df.rename(columns=header)
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[SIDRA {api_path}] tentativa {attempt}/{tries} falhou: {e}")
            if attempt < tries:
                time.sleep(wait)
    raise RuntimeError(f"falha definitiva ao buscar {api_path}: {last_err}")


def parse_trimestral(df: pd.DataFrame, col_valor_saida: str) -> pd.DataFrame:
    out = df[["Trimestre", "Valor"]].copy()
    out["Valor"] = pd.to_numeric(out["Valor"], errors="coerce")

    out["ano"] = out["Trimestre"].str.extract(r"(\d{4})").astype(int)
    out["trimestre"] = out["Trimestre"].str.extract(r"^(\d)").astype(int)

    out["data"] = pd.to_datetime(
        out["ano"].astype(str) + "-" + out["trimestre"].map(MES_INICIO_TRIMESTRE) + "-01"
    )

    out = out.rename(columns={"Valor": col_valor_saida})
    return out[["data", "ano", "trimestre", col_valor_saida]].sort_values("data")


def build_series() -> dict:
    """Coleta + processa PIB/FBCF/hiato. Levanta excecao se a coleta falhar."""
    dados_pib_raw = get_sidra(API_PIB)
    dados_fbcf_raw = get_sidra(API_FBCF)

    dados_pib = parse_trimestral(dados_pib_raw, "pib_milhoes")
    dados_fbcf = parse_trimestral(dados_fbcf_raw, "fbcf_milhoes")

    dados_completos = pd.merge(
        dados_pib, dados_fbcf, on=["data", "ano", "trimestre"], how="outer"
    )

    # Mantem apenas anos com os 4 trimestres completos para PIB e FBCF.
    # Isso evita que um ano em andamento (ex.: 2026 com so 1-2 trimestres
    # divulgados) entre "picado" na soma anual e distorca a variacao e o
    # filtro HP. Conforme o ano avanca e mais trimestres saem, ele passa
    # a entrar automaticamente -- nao precisa ajustar isso na mao.
    completude = dados_completos.groupby("ano").agg(
        pib_trimestres=("pib_milhoes", lambda s: s.notna().sum()),
        fbcf_trimestres=("fbcf_milhoes", lambda s: s.notna().sum()),
    )
    anos_completos = completude[
        (completude["pib_trimestres"] == 4) & (completude["fbcf_trimestres"] == 4)
    ].index
    dados_completos = dados_completos[dados_completos["ano"].isin(anos_completos)]

    anual = (
        dados_completos.groupby("ano", as_index=False)
        .agg(pib_anual=("pib_milhoes", "sum"), fbcf_anual=("fbcf_milhoes", "sum"))
        .sort_values("ano")
        .reset_index(drop=True)
    )

    anual["pib_var_pct"] = anual["pib_anual"].pct_change() * 100
    anual["fbcf_var_pct"] = anual["fbcf_anual"].pct_change() * 100

    # remove o primeiro ano (sem comparacao)
    anual = anual.dropna(subset=["pib_var_pct"]).reset_index(drop=True)
    anual = anual.round(2)

    # Filtro HP -> tendencia (produto potencial) e ciclo (hiato)
    # ATENCAO: statsmodels devolve (ciclo, tendencia) -- ordem invertida
    # em relacao ao mFilter::hpfilter do R.
    ciclo_hp, tendencia_hp = hpfilter(anual["pib_anual"], lamb=100)
    anual["tendencia_hp"] = tendencia_hp
    anual["ciclo_hp"] = ciclo_hp
    anual["hiato_pct"] = (
        (anual["pib_anual"] - anual["tendencia_hp"]) / anual["tendencia_hp"] * 100
    ).round(2)

    return {
        "ano": anual["ano"].astype(int).tolist(),
        "pib_anual": anual["pib_anual"].round(2).tolist(),
        "fbcf_anual": anual["fbcf_anual"].round(2).tolist(),
        "pib_var_pct": anual["pib_var_pct"].round(2).tolist(),
        "fbcf_var_pct": anual["fbcf_var_pct"].round(2).tolist(),
        "tendencia_hp": anual["tendencia_hp"].round(2).tolist(),
        "hiato_pct": anual["hiato_pct"].round(2).tolist(),
    }


def load_previous():
    """Carrega o JSON anterior, se existir, para servir de fallback."""
    if OUTPUT_PATH.exists():
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"aviso: nao consegui ler o JSON anterior ({e})")
    return None


def main():
    previous = load_previous()
    previous_series = (previous or {}).get("series")

    had_failure = False
    failed_keys = []

    try:
        series = build_series()
        print(f"[atividade] OK - {len(series['ano'])} anos ({series['ano'][0]}-{series['ano'][-1]})")
    except Exception as e:  # noqa: BLE001
        print(f"[atividade] ERRO DEFINITIVO: {e}")
        had_failure = True
        failed_keys.append("atividade")
        if previous_series:
            series = previous_series
            print(f"[atividade] usando cache anterior ({len(series.get('ano', []))} anos)")
        else:
            series = {"ano": [], "pib_anual": [], "fbcf_anual": [], "pib_var_pct": [],
                      "fbcf_var_pct": [], "tendencia_hp": [], "hiato_pct": []}
            print("[atividade] sem cache anterior disponivel, salvando vazio")

    payload = {
        "series": series,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "had_failure": had_failure,
        "failed_keys": failed_keys,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\nSalvo em {OUTPUT_PATH}")
    if had_failure:
        print(f"Atencao: falha na coleta (usando cache): {failed_keys}")


if __name__ == "__main__":
    main()
