"""
Busca os dados do painel "Mercado de Trabalho" e salva em
data/mercado_trabalho_data.json.

    Gráfico 1 - Taxa de Desocupação (PNAD Contínua, trimestre móvel),
                com uma segunda linha dessazonalizada via X-13ARIMA-SEATS.
    Gráfico 2 - Taxa de Informalidade (PNAD Contínua, trimestre móvel).
    Gráfico 3 - % de Pessoas Desalentadas, série histórica completa (barras).
    Gráfico 4 - NAIRU estimada com Filtro Hodrick-Prescott.
    Gráfico 5 - NAIRU estimada com Filtro de Baxter-King.

Fontes:
    - Taxa de Desocupação:  SIDRA/IBGE, tabela 6381, variável 4099.
    - Taxa de Informalidade: SIDRA/IBGE, tabela 8513, variável 12466.
    - % Pessoas Desalentadas: SIDRA/IBGE, tabela 6807, variável 9869,
      série histórica completa (antes só se buscava "p/last 1" -- agora
      vira gráfico de barras, não mais um KPI de ponto único).

SOBRE A DESSAZONALIZAÇÃO (X-13ARIMA-SEATS): a série de desocupação é
mensal (trimestre móvel, mas publicada todo mês), então faz sentido
tratá-la como uma série mensal com sazonalidade de período 12 para fins
de ajuste sazonal -- é exatamente esse o uso padrão do X-13 em séries de
mercado de trabalho (a própria IBGE dessazonaliza a série de desocupação
dessa forma). Usa-se statsmodels.tsa.x13.x13_arima_analysis, que por sua
vez chama o binário x13as (Census Bureau) por fora do Python -- não é
puro Python. O binário PRECISA estar disponível no runner (variável de
ambiente X13PATH ou parâmetro x12path); ver nota no fim do arquivo sobre
como instalar isso no workflow do GitHub Actions. Se o binário não
estiver disponível (ou a rotina X-13 falhar por qualquer motivo), a
função levanta e o loop principal cai pro cache anterior de
"desemprego_sa" -- igual ao padrão de robustez do resto do arquivo.

SOBRE OS CÓDIGOS DE PERÍODO DO SIDRA (D3C): pra essas 3 tabelas (PNAD
Contínua, trimestre móvel, uma única variável e sem classificação/grupo
extra), o SIDRA sempre reserva D1 para o território e D2 para a dimensão
"Variável" -- mesmo pedindo uma única variável (ex.: v/4099) ela ainda ocupa
D2, com uma única categoria. O período (Trimestre Móvel) fica em D3, no
formato "AAAAMM", onde MM é o mês de ENCERRAMENTO do trimestre móvel (ex.:
"202412" = trimestre out-nov-dez/2024). Por isso cada linha é convertida
direto pra uma data "AAAA-MM-01" a partir de D3C, sem precisar fazer parsing
do texto (D3N) do período.

SOBRE A NAIRU (traduzido dos dois scripts em R do usuário, "para ficar
igual"): em vez da série do Ipeadata (PNADC12_TDESOC12, via pacote
ipeadatar, que não tem equivalente estável em Python/GitHub Actions), os
dois filtros abaixo usam a série de Taxa de Desocupação do SIDRA já buscada
para o Gráfico 1 -- é a mesma PNAD Contínua, trimestre móvel, então o
resultado fica equivalente.

    - NAIRU (HP): agrega a série mensal em trimestres civis (média, igual
      ao floor_date(...,"quarter") do R) e aplica o filtro Hodrick-Prescott
      (statsmodels.tsa.filters.hp_filter.hpfilter, lamb=1600 -- equivalente
      a mFilter::hpfilter(ts, freq=4)). A NAIRU é a tendência (trend).

    - NAIRU (Baxter-King): mantém a série mensal (sem agregar), estende a
      série pra frente com a mediana do Focus mensal para "Taxa de
      desocupação" (Olinda/BCB, mesma função de scripts/focus_mensal.py) e,
      depois do fim do Focus, replica o último valor por mais 36 meses --
      igual ao R, é só pra "empurrar" a perda de borda do filtro pra frente
      no tempo e não cortar as estimativas mais recentes. Aplica então
      statsmodels.tsa.filters.bk_filter.bkfilter(low=18, high=96, K=12) --
      equivalente a mFilter::bkfilter(ts, pl=18, pu=96) (K=12 é o default
      dos dois pacotes). A NAIRU é desemprego - componente cíclico (cycle),
      igual ao R. O filtro BK sempre descarta as primeiras/últimas K=12
      observações da série informada -- é por isso que a extensão de 36
      meses existe: sem ela, os últimos 12 meses REAIS de desemprego seriam
      descartados também.

Segue o mesmo padrão de robustez dos outros scripts: se uma série falhar,
mantém o dado anterior (cache) pra página nunca ficar sem gráfico -- só
potencialmente desatualizada.

Rodar:
    python scripts/fetch_mercado_trabalho.py

Gerado por GitHub Actions 1x por dia (.github/workflows/update-data.yml).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from statsmodels.tsa.filters.bk_filter import bkfilter
from statsmodels.tsa.filters.hp_filter import hpfilter
from statsmodels.tsa.x13 import x13_arima_analysis

from focus_mensal import buscar_focus_mensal

SIDRA_BASE = "https://apisidra.ibge.gov.br/values"

# tabela, variavel (ver docstring acima)
SIDRA_DESEMPREGO = (6381, 4099)
SIDRA_INFORMALIDADE = (8513, 12466)
SIDRA_DESALENTADOS = (6807, 9869)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "mercado_trabalho_data.json"

# Cache em memória pra não buscar a série de desemprego 3x (grafico 1,
# NAIRU-HP e NAIRU-BK todos partem dela).
_DESEMPREGO_RAW_CACHE = None


def montar_url_sidra(tabela, variavel, periodo="all"):
    periodo_enc = str(periodo).replace(" ", "%20")
    return (
        f"{SIDRA_BASE}/t/{tabela}/n1/all/v/{variavel}"
        f"/p/{periodo_enc}/d/v{variavel}%201"
    )


def fetch_sidra_rows(tabela, variavel, periodo="all", tries=3, wait=8):
    url = montar_url_sidra(tabela, variavel, periodo)
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            linhas = resp.json()
            return linhas[1:]  # descarta a linha de cabecalho (D1C, D1N, ...)
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[sidra t/{tabela} v/{variavel}] tentativa {attempt}/{tries} falhou: {e}")
            if attempt < tries:
                time.sleep(wait)
    raise RuntimeError(f"falha definitiva no sidra t/{tabela} v/{variavel}: {last_err}")


def linha_para_data_valor(linha):
    """Converte uma linha do SIDRA em (data_str, valor) ou None se invalido.

    IMPORTANTE: nessas tabelas (PNAD Continua, trimestre movel, uma unica
    variavel e sem classificacao/grupo extra), o SIDRA sempre reserva D1
    para o territorio e D2 para a dimensao "Variavel" -- mesmo quando so se
    pede UMA variavel (ex.: v/4099) ela ainda ocupa D2, com uma unica
    categoria. O periodo (Trimestre Movel) fica em D3, nao em D2. Confirmado
    direto na API: t/6381/n1/all/v/4099/p/all/d/v4099%201 devolve linhas
    como {"D1C":"1","D1N":"Brasil","D2C":"4099","D2N":"Taxa de
    desocupacao...","D3C":"201203","D3N":"jan-fev-mar 2012"}.

    D3C vem no formato "AAAAMM", onde MM e o mes de ENCERRAMENTO do
    trimestre movel (ex.: "201203" = jan-fev-mar/2012, "202503" =
    jan-fev-mar/2025) -- da pra converter direto pra uma data "AAAA-MM-01"
    sem precisar fazer parsing do texto (D3N)."""
    valor_raw = linha.get("V")
    if valor_raw in (None, "", "...", "-", "X"):
        return None
    d3c = linha.get("D3C")
    if not d3c or len(d3c) != 6:
        return None
    ano_str, mes_str = d3c[:4], d3c[4:6]
    try:
        mes_int = int(mes_str)
        if not (1 <= mes_int <= 12):
            return None
        valor = float(str(valor_raw).replace(",", "."))
    except ValueError:
        return None
    return f"{ano_str}-{mes_str}-01", valor


def fetch_sidra_series(tabela, variavel, periodo="all"):
    linhas = fetch_sidra_rows(tabela, variavel, periodo)
    serie = {}
    for linha in linhas:
        par = linha_para_data_valor(linha)
        if par:
            data_str, valor = par
            serie[data_str] = valor
    if not serie:
        raise RuntimeError(f"sidra t/{tabela} v/{variavel}: nenhum valor valido retornado")
    return serie


def obter_desemprego_raw():
    """Serie de Taxa de Desocupacao do SIDRA (cache em memoria -- ver acima)."""
    global _DESEMPREGO_RAW_CACHE
    if _DESEMPREGO_RAW_CACHE is None:
        _DESEMPREGO_RAW_CACHE = fetch_sidra_series(*SIDRA_DESEMPREGO)
    return _DESEMPREGO_RAW_CACHE


def add_months(data_str, n):
    """Soma n meses a uma data 'AAAA-MM-01' (sem depender de dateutil)."""
    ano, mes = int(data_str[:4]), int(data_str[5:7])
    mes_total = (mes - 1) + n
    novo_ano = ano + mes_total // 12
    novo_mes = mes_total % 12 + 1
    return f"{novo_ano:04d}-{novo_mes:02d}-01"


# ------------------------------------------------------------------
# Graficos 1 e 2 -- series simples do SIDRA
# ------------------------------------------------------------------

def build_desemprego():
    serie = obter_desemprego_raw()
    datas = sorted(serie)
    return {"datas": datas, "valores": [round(serie[d], 2) for d in datas]}


def build_informalidade():
    serie = fetch_sidra_series(*SIDRA_INFORMALIDADE)
    datas = sorted(serie)
    return {"datas": datas, "valores": [round(serie[d], 2) for d in datas]}


# ------------------------------------------------------------------
# Grafico 3 -- % de pessoas desalentadas (serie historica completa,
# antes so se buscava o dado mais recente para um KPI)
# ------------------------------------------------------------------

def build_desalentados():
    serie = fetch_sidra_series(*SIDRA_DESALENTADOS)
    datas = sorted(serie)
    return {"datas": datas, "valores": [round(serie[d], 2) for d in datas]}


# ------------------------------------------------------------------
# Grafico 1b -- Taxa de Desocupacao dessazonalizada (X-13ARIMA-SEATS)
# ------------------------------------------------------------------

def build_desemprego_sa():
    serie = obter_desemprego_raw()
    datas = sorted(serie)
    idx = pd.to_datetime(datas)
    valores = pd.Series([serie[d] for d in datas], index=idx, name="taxa")

    # asfreq("MS") so pra garantir frequencia mensal explicita pro X-13 --
    # a serie do SIDRA ja vem sem buracos (um ponto por mes), entao isso
    # nao deve introduzir NaN na pratica.
    valores = valores.asfreq("MS")
    if valores.isna().any():
        raise RuntimeError("desemprego_sa: serie mensal com buracos, X-13 exige serie regular")
    if len(valores) < 36:
        raise RuntimeError("desemprego_sa: serie curta demais para o X-13 (minimo recomendado: 3 anos)")

    # x12path: tenta localizar o binario x13as automaticamente via o
    # pacote "x13binary" (pip install x13binary), que empacota o binario
    # do Census Bureau pronto pra uso -- funciona tanto local (Windows/
    # Linux/Mac) quanto no runner do GitHub Actions, sem precisar mexer
    # em PATH/X13PATH na mao. Se o pacote nao estiver instalado, cai pro
    # comportamento padrao do statsmodels (procura no PATH ou em
    # X13PATH/X12PATH). Se, mesmo assim, o binario nao for encontrado (ou
    # a rotina X-13 falhar por qualquer outro motivo), cai no fallback
    # 100% Python (STL) em vez de deixar "desemprego_sa" falhar todo dia
    # -- ver nota logo abaixo.
    x12path = None
    try:
        import x13binary
        x12path = x13binary.find_x13_bin()
    except ImportError:
        pass  # pacote nao instalado -- statsmodels tenta o PATH/X13PATH normalmente

    try:
        resultado = x13_arima_analysis(valores, outlier=True, print_stdout=False, x12path=x12path)
        ajustada = resultado.seasadj.dropna()
    except Exception as e:  # noqa: BLE001
        print(f"[desemprego_sa] aviso: X-13ARIMA-SEATS indisponivel ({e}); usando fallback STL")
        ajustada = _dessazonalizar_stl_fallback(valores)

    datas_sa = [d.strftime("%Y-%m-%d") for d in ajustada.index]
    return {"datas": datas_sa, "valores": [round(v, 2) for v in ajustada.tolist()]}


def _dessazonalizar_stl_fallback(valores):
    """Ajuste sazonal alternativo quando o binario x13as nao esta disponivel
    no runner (ex.: "x12a and x13as not found on path").

    Usa statsmodels.tsa.seasonal.STL, que e puro Python/numpy -- nao chama
    nenhum binario externo, entao SEMPRE funciona, independente de como o
    ambiente do GitHub Actions esta configurado. Nao e identico ao
    X-13ARIMA-SEATS (STL nao faz deteccao/tratamento de outliers regARIMA
    como o X-13), mas e o substituto padrao em Python para dessazonalizar
    uma serie mensal com sazonalidade de periodo 12, e evita que o painel
    fique dependendo de instalar um binario externo pra nunca falhar.

    Se o X13PATH/X12PATH estiver configurado corretamente no runner, essa
    funcao nem chega a ser chamada -- o X-13 de verdade continua sendo
    usado preferencialmente (ver build_desemprego_sa acima)."""
    from statsmodels.tsa.seasonal import STL

    stl_result = STL(valores, period=12, robust=True).fit()
    return (valores - stl_result.seasonal).dropna()


# ------------------------------------------------------------------
# NAIRU -- Filtro Hodrick-Prescott (traducao do script HP em R)
# ------------------------------------------------------------------

def build_nairu_hp():
    serie = obter_desemprego_raw()
    df = pd.DataFrame(
        {"data": pd.to_datetime(list(serie.keys())), "valor": list(serie.values())}
    ).sort_values("data")

    # Media trimestral (trimestre civil), igual ao floor_date(data,"quarter")
    # + summarise(mean) do R.
    trimestral = (
        df.set_index("data")["valor"]
        .resample("QS")
        .mean()
        .dropna()
    )

    if len(trimestral) < 8:
        raise RuntimeError("nairu_hp: serie trimestral curta demais para o filtro HP")

    # lamb=1600 == mFilter::hpfilter(ts, freq=4) (default para dados trimestrais)
    _cycle, trend = hpfilter(trimestral.values, lamb=1600)

    datas = [d.strftime("%Y-%m-%d") for d in trimestral.index]
    return {
        "datas": datas,
        "desemprego": [round(v, 2) for v in trimestral.tolist()],
        "nairu": [round(v, 2) for v in trend.tolist()],
    }


# ------------------------------------------------------------------
# NAIRU -- Filtro de Baxter-King (traducao do script BK em R)
# ------------------------------------------------------------------

def build_nairu_bk():
    serie_hist = dict(obter_desemprego_raw())
    ultima_data_hist = max(serie_hist)

    # Projecoes do Focus mensal (mediana) pra "Taxa de desocupacao", so os
    # meses posteriores ao ultimo dado historico -- igual ao R, que so usa
    # o Focus a partir de jul/2025 (mes seguinte ao ultimo dado do Ipeadata).
    try:
        focus_bruto = buscar_focus_mensal("Taxa de desocupação")
    except Exception as e:  # noqa: BLE001
        print(f"[nairu_bk] aviso: Focus (Taxa de desocupacao) falhou, seguindo sem projecao: {e}")
        focus_bruto = {}
    focus_futuro = {d: v for d, v in focus_bruto.items() if d > ultima_data_hist}

    combinado = dict(serie_hist)
    combinado.update(focus_futuro)

    # Replica o ultimo valor combinado (historico ou Focus) por mais 36
    # meses -- igual ao R (projecao_simples), so pra "empurrar" a perda de
    # borda do filtro BK pra frente no tempo.
    datas_comb = sorted(combinado)
    ultima_data_comb = datas_comb[-1]
    ultimo_valor_comb = combinado[ultima_data_comb]
    for i in range(1, 37):
        combinado[add_months(ultima_data_comb, i)] = ultimo_valor_comb

    datas_full = sorted(combinado)
    valores_full = np.array([combinado[d] for d in datas_full])

    K = 12  # default do mFilter::bkfilter e do statsmodels.bk_filter
    if len(valores_full) <= 2 * K:
        raise RuntimeError("nairu_bk: serie curta demais para o filtro Baxter-King")

    # pl=18, pu=96 (R) == low=18, high=96 (statsmodels) -- ciclos de 18 a 96
    # meses, equivalente a 6-32 trimestres (default de mFilter) convertido
    # pra base mensal.
    cycle = bkfilter(valores_full, low=18, high=96, K=K)

    # O filtro BK descarta as primeiras/ultimas K observacoes.
    datas_validas = datas_full[K:-K]
    valores_validos = valores_full[K:-K]
    nairu_validos = valores_validos - cycle

    return {
        "datas": datas_validas,
        "desemprego": [round(v, 2) for v in valores_validos.tolist()],
        "nairu": [round(v, 2) for v in nairu_validos.tolist()],
        # ultima_data_hist = ultimo ponto REAL (SIDRA) antes de entrar Focus
        # e depois a extensao/replicacao -- o front usa isso pra colorir de
        # verde/em bolinhas a parte que ainda nao ocorreu.
        "data_corte": ultima_data_hist,
    }


# ------------------------------------------------------------------
# Persistencia (mesmo padrao dos outros scripts do painel)
# ------------------------------------------------------------------

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
    previous_dados = (previous or {}).get("dados", {})

    had_failure = False
    failed_keys = []
    dados = {}

    tarefas = {
        "desemprego": build_desemprego,
        "desemprego_sa": build_desemprego_sa,
        "informalidade": build_informalidade,
        "desalentados": build_desalentados,
        "nairu_hp": build_nairu_hp,
        "nairu_bk": build_nairu_bk,
    }

    for chave, fn in tarefas.items():
        try:
            dados[chave] = fn()
            print(f"[{chave}] OK")
        except Exception as e:  # noqa: BLE001
            print(f"[{chave}] ERRO DEFINITIVO: {e}")
            had_failure = True
            failed_keys.append(chave)
            if chave in previous_dados:
                dados[chave] = previous_dados[chave]
                print(f"[{chave}] usando cache anterior")
            else:
                dados[chave] = None
                print(f"[{chave}] sem cache anterior disponivel, salvando vazio")

    payload = {
        "dados": dados,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "had_failure": had_failure,
        "failed_keys": failed_keys,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\nSalvo em {OUTPUT_PATH}")
    if had_failure:
        print(f"Atencao: dados com falha (usando cache): {failed_keys}")


if __name__ == "__main__":
    main()


# ------------------------------------------------------------------
# NOTA -- binario x13as no GitHub Actions
# ------------------------------------------------------------------
# build_desemprego_sa() depende do binario x13as (Census Bureau), que o
# statsmodels chama por fora do Python -- nao vem com "pip install
# statsmodels". Precisa entrar como um passo a mais no
# .github/workflows/update-data.yml, ANTES de rodar este script, por
# exemplo:
#
#   - name: Instalar X-13ARIMA-SEATS
#     run: |
#       wget -q https://www2.census.gov/software/x-13arima-seats/x13as/unix-linux/program-archives/x13as_ascii-v1-1-b59.tar.gz
#       mkdir -p x13as && tar -xzf x13as_ascii-v1-1-b59.tar.gz -C x13as
#       chmod +x x13as/x13as
#       echo "X13PATH=$PWD/x13as" >> "$GITHUB_ENV"
#
# (confira a versao/URL atual no site do Census antes de usar -- o link
# muda de vez em quando). Sem esse passo, "desemprego_sa" simplesmente
# falha todo dia e o painel cai pro cache anterior -- a pagina nunca
# quebra, so o grafico fica sem a linha dessazonalizada ate o binario
# ser instalado no runner.
