"""
Busca os 4 graficos do painel "IPCA - Decomposicao" e salva em
data/inflacao_data.json.

    Grafico 1 - Variacao acum. 12m por grupo (mes mais recente x mesmo mes
                do ano anterior), 9 grupos do IPCA.
    Grafico 2 - Indice de Difusao (serie mensal, Brasil).
    Grafico 3 - Precos Livres, Comercializaveis, Nao comercializaveis e
                Monitorados (acum. 12m).
    Grafico 4 - Bens Duraveis, Semiduraveis, Nao duraveis, Servicos e
                Monitorados (acum. 12m).

CORRECAO IMPORTANTE (v2 deste script): a primeira versao tentava buscar
esses dados na API do SIDRA/IBGE (tabela 7060), mas os codigos que estavam
nas notas do CIEM (1635, 1636 ... 11428, 4447, 4448 ... 10841-10844,
21379) na verdade sao codigos de SERIE do SGS/Banco Central, nao
classificacoes do SIDRA -- por isso a tentativa anterior dava erro
(coluna inexistente / 400 Bad Request). Todos esses codigos foram
reconferidos: sao series de "IPCA - Variacao mensal (%)" por grupo/
segmento no SGS, exceto a de difusao (21379), que ja vem pronta em % do
proprio SGS (nao precisa acumular). Ver:
https://www3.bcb.gov.br/sgspub/localizarseries/localizarSeries.do

Como os grupos/segmentos so tem a serie MENSAL disponivel no SGS, o
acumulado em 12 meses (o que o grafico do CIEM mostra) e calculado aqui
mesmo, com o mesmo metodo (produto dos fatores mensais, composto) usado
em scripts/fetch_politica_monetaria.py pro IPCA geral.

Segue o mesmo padrao de robustez dos outros scripts: se uma serie falhar,
mantem o dado anterior (cache) pra pagina nunca ficar sem grafico -- so
potencialmente desatualizada.

Rodar:
    python scripts/fetch_inflacao_data.py

Gerado por GitHub Actions 1x por dia (.github/workflows/update-data.yml).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
DATA_INICIAL = "01/01/2014"  # 1 ano de folga antes de 2015 pra ja poder acumular 12m desde jan/15

SGS_DIFUSAO = 21379  # IPCA - Indice de difusao (%, ja pronto, nao precisa acumular)

GRUPOS = {
    "Educação": 1643,
    "Despesas Pessoais": 1642,
    "Saúde e Cuidados Pessoais": 1641,
    "Comunicação": 1640,
    "Transportes": 1639,
    "Vestuário": 1638,
    "Artigos de Residência": 1637,
    "Habitação": 1636,
    "Alimentação e Bebidas": 1635,
}

LIVRES_MONITORADOS = {
    "Livres": 11428,
    "Comercializáveis": 4447,
    "Não Comercializáveis": 4448,
    "Monitorados": 4449,
}

DURAVEIS_SERVICOS = {
    "Duráveis": 10843,
    "Semiduráveis": 10842,
    "Não Duráveis": 10841,
    "Serviços": 10844,
    "Monitorados": 4449,
}

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "inflacao_data.json"


def fetch_series(codigo, tries=3, wait=8):
    url = BASE_URL.format(codigo=codigo)
    params = {"formato": "json", "dataInicial": DATA_INICIAL}
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
            serie = {}
            for item in raw:
                if item.get("valor") in (None, ""):
                    continue
                dia, mes, ano = item["data"].split("/")
                data_str = f"{ano}-{mes}-01"
                serie[data_str] = float(str(item["valor"]).replace(",", "."))
            return serie
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[serie {codigo}] tentativa {attempt}/{tries} falhou: {e}")
            if attempt < tries:
                time.sleep(wait)
    raise RuntimeError(f"falha definitiva na serie {codigo}: {last_err}")


def acumulado_12m(serie_mensal_pct):
    """Recebe {data: valor_mensal_%} e devolve {data: valor_acum_12m_%},
    so preenchendo datas com os 12 meses anteriores completos (composto)."""
    datas = sorted(serie_mensal_pct)
    out = {}
    for i, data_str in enumerate(datas):
        if i < 11:
            continue
        janela = datas[i - 11:i + 1]
        fator = 1.0
        for d in janela:
            fator *= (1 + serie_mensal_pct[d] / 100)
        out[data_str] = round((fator - 1) * 100, 2)
    return out


def build_grupo_series(codigos_nomes: dict):
    """Busca a serie mensal de cada grupo/segmento, acumula em 12m, e
    devolve no formato {"datas": [...], "series": {nome: [valores...]}}
    com o eixo de datas unificado (uniao de todas as series)."""
    acumulados = {}
    for nome, codigo in codigos_nomes.items():
        mensal = fetch_series(codigo)
        acumulados[nome] = acumulado_12m(mensal)

    todas_datas = sorted(set().union(*[set(v) for v in acumulados.values()]))
    series = {
        nome: [acumulados[nome].get(d) for d in todas_datas]
        for nome in codigos_nomes
    }
    return {"datas": todas_datas, "series": series}


def build_grupos():
    """Grafico 1: mesmo motor de build_grupo_series, mas guarda o
    resultado na chave 'grupos' (em vez de 'series') pra bater com o que
    o front-end (renderGruposChart) espera."""
    resultado = build_grupo_series(GRUPOS)
    return {"datas": resultado["datas"], "grupos": resultado["series"]}


def build_difusao():
    """Grafico 2: indice de difusao -- ja vem em % pronto do SGS, sem
    precisar acumular nada."""
    mensal = fetch_series(SGS_DIFUSAO)
    datas = sorted(mensal)
    return {"datas": datas, "difusao_pct": [round(mensal[d], 2) for d in datas]}


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
    previous_graficos = (previous or {}).get("graficos", {})

    had_failure = False
    failed_keys = []
    graficos = {}

    tarefas = {
        "grupos": build_grupos,
        "difusao": build_difusao,
        "livres_monitorados": lambda: build_grupo_series(LIVRES_MONITORADOS),
        "duraveis_servicos": lambda: build_grupo_series(DURAVEIS_SERVICOS),
    }

    for chave, fn in tarefas.items():
        try:
            graficos[chave] = fn()
            print(f"[{chave}] OK")
        except Exception as e:  # noqa: BLE001
            print(f"[{chave}] ERRO DEFINITIVO: {e}")
            had_failure = True
            failed_keys.append(chave)
            if chave in previous_graficos:
                graficos[chave] = previous_graficos[chave]
                print(f"[{chave}] usando cache anterior")
            else:
                graficos[chave] = None
                print(f"[{chave}] sem cache anterior disponivel, salvando vazio")

    payload = {
        "graficos": graficos,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "had_failure": had_failure,
        "failed_keys": failed_keys,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\nSalvo em {OUTPUT_PATH}")
    if had_failure:
        print(f"Atencao: graficos com falha (usando cache): {failed_keys}")


if __name__ == "__main__":
    main()
