"""
Busca o IPCA mensal realizado direto da API do BCB (SGS 433), mescla com a
projecao de inflacao implicita calculada a partir do Tesouro Direto (curva
nominal x real, cubic spline -- ver tesouro_implicita.py), com fallback para
o cache do Excel (data/cache_excel_ciem.json) caso o Tesouro Direto falhe ou
nao tenha pontos suficientes na curva. Gera data/politica_monetaria_data.json
no mesmo padrao dos outros paineis do CIEM (updated_at / had_failure /
series), pronto pro index.html consumir via fetch().

Rodar:
    python scripts/fetch_politica_monetaria.py

Gerado por GitHub Actions 1x por dia (.github/workflows/update-data.yml).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from focus_mensal import buscar_focus_mensal_ipca
from tesouro_implicita import baixar_dados_tesouro, calcular_projecao_mensal

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_EXCEL = REPO_ROOT / "data" / "cache_excel_ciem.json"
OUTPUT_JSON = REPO_ROOT / "data" / "politica_monetaria_data.json"

SGS_IPCA_MENSAL = 433
HORIZONTE_TRIMESTRES = 6
MESES_PROJECAO = 24  # cobre folgadamente o horizonte relevante (6 trimestres = 18 meses)

METAS_POR_ANO = {
    2020: (0.0400, 0.0250, 0.0550),
    2021: (0.0375, 0.0225, 0.0525),
    2022: (0.0350, 0.0200, 0.0500),
    2023: (0.0325, 0.0175, 0.0475),
    2024: (0.0300, 0.0150, 0.0450),
    2025: (0.0300, 0.0150, 0.0450),
    2026: (0.0300, 0.0150, 0.0450),
    2027: (0.0300, 0.0150, 0.0450),
    2028: (0.0300, 0.0150, 0.0450),
}


def buscar_ipca_mensal_bcb(data_inicial="01/01/2018"):
    url = (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{SGS_IPCA_MENSAL}/dados"
        f"?formato=json&dataInicial={data_inicial}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    serie = {}
    for item in resp.json():
        data = datetime.strptime(item["data"], "%d/%m/%Y").strftime("%Y-%m-%d")
        serie[data] = float(item["valor"]) / 100
    return serie


def carregar_cache_excel():
    with open(CACHE_EXCEL, "r", encoding="utf-8") as f:
        return {reg["data"]: reg for reg in json.load(f)}


def meta_do_mes(data_str):
    ano = int(data_str[:4])
    ano = min(max(ano, min(METAS_POR_ANO)), max(METAS_POR_ANO))
    return METAS_POR_ANO[ano]


def montar_registros(ipca_real, cache_excel, projecao_implicita):
    todas_datas = sorted(set(ipca_real) | set(cache_excel) | set(projecao_implicita))
    registros = []
    for data_str in todas_datas:
        centro, piso, teto = meta_do_mes(data_str)
        reg_excel = cache_excel.get(data_str, {})
        efetivo_real = ipca_real.get(data_str)
        eh_realizado = efetivo_real is not None

        if eh_realizado:
            ipca_mes = efetivo_real
            fonte_projecao = None
        elif data_str in projecao_implicita:
            ipca_mes = projecao_implicita[data_str]
            fonte_projecao = "implicita_tesouro"
        else:
            ipca_mes = reg_excel.get("ciem_projecao_mes")
            fonte_projecao = "excel_cache" if ipca_mes is not None else None

        registros.append({
            "data": data_str,
            "ipca_mes": ipca_mes,
            "realizado": eh_realizado,
            "fonte_projecao": fonte_projecao,
            "meta_centro": centro,
            "meta_piso": piso,
            "meta_teto": teto,
        })

    for i, reg in enumerate(registros):
        if i < 11:
            reg["ipca_acum12"] = None
            continue
        janela = [registros[j]["ipca_mes"] for j in range(i - 11, i + 1)]
        if any(v is None for v in janela):
            reg["ipca_acum12"] = None
        else:
            acumulado = 1.0
            for v in janela:
                acumulado *= (1 + v)
            reg["ipca_acum12"] = acumulado - 1
    return registros


def data_base_mais_recente(registros):
    realizados = [r["data"] for r in registros if r["realizado"]]
    return max(realizados) if realizados else None


def montar_serie_projecao(registros, ultimo_valor_realizado):
    """Extrai a série de projeção (em %) de UM conjunto de registros (de UMA
    metodologia), conectando ao último ponto realizado pra não deixar buraco
    visual na virada realizado -> projeção."""
    serie = []
    for r in registros:
        v = r["ipca_acum12"]
        v_pct = round(v * 100, 4) if v is not None else None
        if r["realizado"]:
            serie.append(None)
        elif v_pct is not None:
            if serie and all(x is None for x in serie):
                serie[-1] = ultimo_valor_realizado
            serie.append(v_pct)
        else:
            serie.append(None)
    return serie


def montar_serie_focus(datas, ipca_real, focus_mensal, ultimo_valor_realizado):
    """Monta a série do Focus mensal (acum. 12m) casada EXATAMENTE no mesmo
    eixo `datas` das demais séries do painel (evita desalinhar com os labels
    do Chart.js, já que o Focus tem seu próprio conjunto de meses de
    referência, que não necessariamente bate 1:1 com o das projeções
    implícitas). Mesma lógica de encadeamento realizado -> projeção das
    outras metodologias: usa IPCA real onde houver, Focus mensal fora disso."""
    ipca_mes = [ipca_real.get(d, focus_mensal.get(d)) for d in datas]

    acum12 = []
    for i in range(len(datas)):
        if i < 11:
            acum12.append(None)
            continue
        janela = ipca_mes[i - 11:i + 1]
        if any(v is None for v in janela):
            acum12.append(None)
        else:
            fator = 1.0
            for v in janela:
                fator *= (1 + v)
            acum12.append(fator - 1)

    serie = []
    for i, data_str in enumerate(datas):
        realizado = data_str in ipca_real
        v_pct = round(acum12[i] * 100, 4) if acum12[i] is not None else None
        if realizado:
            serie.append(None)
        elif v_pct is not None:
            if serie and all(x is None for x in serie):
                serie[-1] = ultimo_valor_realizado
            serie.append(v_pct)
        else:
            serie.append(None)
    return serie


def montar_series_para_chart(registros_por_metodologia, metodologias):
    """Monta as séries pro Chart.js: eixo/metas/realizado compartilhados
    (idênticos nas 3 metodologias, porque o realizado sempre vem do IPCA
    do BCB) + uma linha de projeção POR metodologia, pra comparação."""
    base = registros_por_metodologia[metodologias[0]]
    datas = [r["data"] for r in base]
    meta_centro = [round(r["meta_centro"] * 100, 4) for r in base]
    meta_piso = [round(r["meta_piso"] * 100, 4) for r in base]
    meta_teto = [round(r["meta_teto"] * 100, 4) for r in base]

    ipca_realizado = []
    ultimo_valor_realizado = None
    for r in base:
        v = r["ipca_acum12"]
        v_pct = round(v * 100, 4) if v is not None else None
        if r["realizado"] and v_pct is not None:
            ipca_realizado.append(v_pct)
            ultimo_valor_realizado = v_pct
        else:
            ipca_realizado.append(None)

    series = {
        "datas": datas,
        "ipca_acum12_realizado": ipca_realizado,
        "meta_centro": meta_centro,
        "meta_piso": meta_piso,
        "meta_teto": meta_teto,
    }
    for met in metodologias:
        series[f"ipca_acum12_projecao_{met}"] = montar_serie_projecao(
            registros_por_metodologia[met], ultimo_valor_realizado
        )
    return series


def main():
    had_failure = False
    METODOLOGIAS = ["zero", "misturado"]

    print("Buscando IPCA mensal real na API do BCB (SGS 433)...")
    try:
        ipca_real = buscar_ipca_mensal_bcb()
        print(f"  -> {len(ipca_real)} meses obtidos da API.")
    except Exception as e:
        print(f"  !! Falha ao acessar a API do BCB ({e}). Usando só o cache do Excel.")
        ipca_real = {}
        had_failure = True

    print("Calculando as curvas de inflação implícita (Tesouro Direto, cubic spline)...")
    projecoes = {met: {} for met in METODOLOGIAS}
    try:
        dados_tesouro = baixar_dados_tesouro()
        data_curva = dados_tesouro["Data Base"].max()
        for met in METODOLOGIAS:
            try:
                projecoes[met] = calcular_projecao_mensal(dados_tesouro, data_curva, MESES_PROJECAO, metodologia=met)
                print(f"  -> {met}: curva de {data_curva.date()}, {len(projecoes[met])} meses projetados.")
            except ValueError as e:
                print(f"  !! metodologia '{met}' sem pontos suficientes ({e}). Linha ficará vazia (cai no fallback do Excel).")
                had_failure = True
    except Exception as e:
        print(f"  !! Falha ao baixar dados do Tesouro Direto ({e}). Usando fallback do Excel nas 3 linhas.")
        had_failure = True

    print("Buscando Focus mensal (mediana por mês de referência, BCB Olinda)...")
    # OBSERVAÇÃO: buscar_focus_mensal_ipca() usa base_calculo=0 por padrão,
    # ou seja, estamos olhando a mediana das expectativas do Focus
    # informadas nos ÚLTIMOS 30 DIAS (não só a coleta do dia). Ver docstring
    # em focus_mensal.py se quiser trocar pra base_calculo=1 (últimos 4 dias
    # úteis).
    try:
        focus_mensal = buscar_focus_mensal_ipca()
        print(f"  -> {len(focus_mensal)} meses de referência obtidos do Focus.")
    except Exception as e:
        print(f"  !! Falha ao acessar o Focus mensal ({e}). Linha de comparação ficará vazia.")
        focus_mensal = {}
        # não marca had_failure: Focus é só comparação, não é a série principal do painel.

    cache_excel = carregar_cache_excel()
    registros_por_metodologia = {
        met: montar_registros(ipca_real, cache_excel, projecoes[met]) for met in METODOLOGIAS
    }
    base_date = data_base_mais_recente(registros_por_metodologia["zero"])
    series = montar_series_para_chart(registros_por_metodologia, METODOLOGIAS)

    ultimo_valor_realizado = next(v for v in series["ipca_acum12_realizado"][::-1] if v is not None)
    series["ipca_acum12_focus"] = montar_serie_focus(
        series["datas"], ipca_real, focus_mensal, ultimo_valor_realizado
    )

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "had_failure": had_failure,
        "data_base": base_date,
        "horizonte_relevante_trimestres": HORIZONTE_TRIMESTRES,
        "metodologias": METODOLOGIAS,
        "series": series,
    }

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"Data-base (último mês realizado): {base_date}")
    print(f"Arquivo gerado: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
