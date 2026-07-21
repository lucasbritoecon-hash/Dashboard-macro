"""
Busca séries do SGS/BCB (API oficial) e salva em data/fiscal_data.json.

Se uma série falhar, mantém os dados anteriores dessa série (cache),
para a página nunca ficar sem dado — só potencialmente desatualizada.

Rodado pelo GitHub Actions (.github/workflows/update-data.yml).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# codigo das series no SGS/BCB
SERIES = {
    "nominal_pct": 5727,       # Resultado Nominal, % do PIB
    "primario_pct": 5793,      # Resultado Primario, % do PIB
    "nominal_brl": 5012,       # Resultado Nominal, R$ milhoes
    "primario_brl": 5078,      # Resultado Primario, R$ milhoes
    "divida_liquida_pct": 4513,  # Divida Liquida do Setor Publico, % do PIB
}

BASE_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"

# raiz do repo = pasta pai de scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "fiscal_data.json"


def fetch_series(codigo, tries=3, wait=8):
    """Busca uma serie do SGS/BCB com retentativas. Levanta erro se todas falharem."""
    url = BASE_URL.format(codigo=codigo)
    params = {"formato": "json"}
    last_err = None

    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, params=params, timeout=25)
            resp.raise_for_status()
            raw = resp.json()
            return [
                {"data": item["data"], "valor": float(str(item["valor"]).replace(",", "."))}
                for item in raw
                if item.get("valor") not in (None, "")
            ]
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[serie {codigo}] tentativa {attempt}/{tries} falhou: {e}")
            if attempt < tries:
                time.sleep(wait)

    raise RuntimeError(f"falha definitiva na serie {codigo}: {last_err}")


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
    previous_series = (previous or {}).get("series", {})

    result = {}
    any_failure = False
    failed_keys = []

    for key, codigo in SERIES.items():
        try:
            result[key] = fetch_series(codigo)
            print(f"[{key} / serie {codigo}] OK - {len(result[key])} pontos")
        except Exception as e:  # noqa: BLE001
            print(f"[{key} / serie {codigo}] ERRO DEFINITIVO: {e}")
            any_failure = True
            failed_keys.append(key)
            if key in previous_series and previous_series[key]:
                result[key] = previous_series[key]
                print(f"[{key}] usando cache anterior ({len(result[key])} pontos)")
            else:
                result[key] = []
                print(f"[{key}] sem cache anterior disponivel, salvando vazio")

    payload = {
        "series": result,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "had_failure": any_failure,
        "failed_keys": failed_keys,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\nSalvo em {OUTPUT_PATH}")
    if any_failure:
        print(f"Atencao: series com falha (usando cache): {failed_keys}")


if __name__ == "__main__":
    main()
