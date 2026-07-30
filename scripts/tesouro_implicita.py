"""
Inflação implícita via Tesouro Direto (curva nominal x curva real, cubic spline).

Baixa o CSV consolidado do Tesouro Transparente (todos os títulos, todas as
datas) e monta TRÊS pares de curva nominal x real:
  - "zero"     : LTN x NTN-B Principal (ambos bullet/zero-coupon)
  - "cupom"    : NTN-F x NTN-B com Juros Semestrais (ambos com cupom semestral)
  - "misturado": todos os nominais x todos os reais numa curva só (método antigo)

Ajusta uma cubic spline em cada curva de cada par, e a partir do breakeven
[(1+nominal)/(1+real) - 1] deriva uma PROJEÇÃO MENSAL de IPCA implícito para
os próximos N meses (decompondo a taxa acumulada anualizada em taxas mensais
mês a mês). Por padrão (metodologia="auto") usa o par "zero" onde ele cobrir
o prazo e cai pro "cupom" fora dessa cobertura; "misturado" fica disponível
só pra comparação.

Usado por fetch_politica_monetaria.py no lugar da projeção fixa do Excel.
"""

import io
import unicodedata
from datetime import date

import numpy as np
import pandas as pd
import requests
import xlrd
from scipy.interpolate import CubicSpline

URL_TESOURO = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/"
    "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
    "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv"
)

URL_FERIADOS_ANBIMA = "https://www.anbima.com.br/feriados/arqs/feriados_nacionais.xls"

_FERIADOS_CACHE: np.ndarray | None = None


def _baixar_feriados_anbima(url: str = URL_FERIADOS_ANBIMA) -> np.ndarray:
    """Baixa e parseia o calendário oficial de feriados nacionais da ANBIMA/B3
    (mesmo arquivo .xls usado pra convenção DU/252 oficial). Retorna array de
    np.datetime64[D] pro parâmetro `holidays` de numpy.busday_count."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    wb = xlrd.open_workbook(file_contents=resp.content)
    ws = wb.sheet_by_index(0)

    datas = []
    i = 1  # linha 0 é cabeçalho
    while i < ws.nrows and ws.cell_type(i, 0) == xlrd.XL_CELL_DATE:
        ano, mes, dia, _h, _mi, _s = xlrd.xldate_as_tuple(ws.cell_value(i, 0), wb.datemode)
        datas.append(date(ano, mes, dia))
        i += 1

    if not datas:
        raise ValueError("Não foi possível parsear nenhum feriado do arquivo ANBIMA.")

    return np.array(datas, dtype="datetime64[D]")


def _feriados_cache() -> np.ndarray:
    """Cache em memória de processo -- evita rebaixar o xls a cada par/metodologia
    dentro da mesma execução (fetch_politica_monetaria.py chama calcular_projecao_mensal
    uma vez por metodologia)."""
    global _FERIADOS_CACHE
    if _FERIADOS_CACHE is None:
        _FERIADOS_CACHE = _baixar_feriados_anbima()
    return _FERIADOS_CACHE


def _prazo_du252(data_base: pd.Timestamp, data_fim) -> np.ndarray:
    """Prazo em anos, convenção DU/252 (dias úteis / 252) com o calendário
    ANBIMA/B3 -- aceita `data_fim` escalar (pd.Timestamp) ou vetor (pd.Series)."""
    inicio = np.datetime64(data_base.date())
    if isinstance(data_fim, pd.Series):
        fins = data_fim.values.astype("datetime64[D]")
    else:
        fins = np.datetime64(data_fim.date())
    du = np.busday_count(inicio, fins, holidays=_feriados_cache())
    return du / 252

TITULOS_NOMINAIS_ZERO = ["Tesouro Prefixado"]                          # LTN
TITULOS_NOMINAIS_CUPOM = ["Tesouro Prefixado com Juros Semestrais"]     # NTN-F
TITULOS_REAIS_ZERO = ["Tesouro IPCA+"]                                  # NTN-B Principal
TITULOS_REAIS_CUPOM = ["Tesouro IPCA+ com Juros Semestrais"]            # NTN-B c/ juros

# mantidos por compatibilidade (uso em baixar_dados_tesouro, que so filtra o CSV)
TITULOS_NOMINAIS = TITULOS_NOMINAIS_ZERO + TITULOS_NOMINAIS_CUPOM
TITULOS_REAIS = TITULOS_REAIS_ZERO + TITULOS_REAIS_CUPOM

COLUNA_TAXA_PADRAO = "Taxa Venda Manha"


def baixar_dados_tesouro() -> pd.DataFrame:
    resp = requests.get(URL_TESOURO, timeout=120)
    resp.raise_for_status()

    df = pd.read_csv(io.BytesIO(resp.content), sep=";", decimal=",", encoding="latin1")

    def normaliza(col: str) -> str:
        col = unicodedata.normalize("NFKD", col).encode("ascii", "ignore").decode()
        return col.strip()

    df.columns = [normaliza(c) for c in df.columns]
    df["Data Base"] = pd.to_datetime(df["Data Base"], dayfirst=True)
    df["Data Vencimento"] = pd.to_datetime(df["Data Vencimento"], dayfirst=True)

    # Só precisamos dos títulos usados na curva nominal/real -> reduz o
    # dataframe bastante antes de qualquer outra operação.
    tipos_relevantes = TITULOS_NOMINAIS + TITULOS_REAIS
    return df[df["Tipo Titulo"].isin(tipos_relevantes)].copy()


def monta_curva(df: pd.DataFrame, tipos_titulo: list[str], data_base: pd.Timestamp,
                 coluna_taxa: str = COLUNA_TAXA_PADRAO) -> pd.DataFrame:
    curva = df[(df["Tipo Titulo"].isin(tipos_titulo)) & (df["Data Base"] == data_base)].copy()
    curva["prazo_anos"] = _prazo_du252(data_base, curva["Data Vencimento"])
    curva["taxa_decimal"] = curva[coluna_taxa] / 100
    curva = curva.dropna(subset=["prazo_anos", "taxa_decimal"])
    curva = curva.sort_values("prazo_anos").drop_duplicates(subset="prazo_anos")
    return curva[["Tipo Titulo", "Data Vencimento", "prazo_anos", "taxa_decimal"]]


def _ajusta_par(df: pd.DataFrame, tipos_nominal: list[str], tipos_real: list[str],
                 data_base: pd.Timestamp, coluna_taxa: str, nome_par: str):
    """Ajusta spline nominal x real para UM par (zero-coupon OU com cupom).
    Retorna None se não houver >=3 pontos em cada lado (par insuficiente)."""
    curva_nominal = monta_curva(df, tipos_nominal, data_base, coluna_taxa)
    curva_real = monta_curva(df, tipos_real, data_base, coluna_taxa)

    if len(curva_nominal) < 3 or len(curva_real) < 3:
        return None

    spline_nominal = CubicSpline(curva_nominal["prazo_anos"], curva_nominal["taxa_decimal"])
    spline_real = CubicSpline(curva_real["prazo_anos"], curva_real["taxa_decimal"])
    prazo_min = max(curva_nominal["prazo_anos"].min(), curva_real["prazo_anos"].min())
    prazo_max = min(curva_nominal["prazo_anos"].max(), curva_real["prazo_anos"].max())

    return {
        "nome": nome_par,
        "spline_nominal": spline_nominal,
        "spline_real": spline_real,
        "prazo_min": prazo_min,
        "prazo_max": prazo_max,
    }


def ajustar_splines(df: pd.DataFrame, data_base: pd.Timestamp,
                     coluna_taxa: str = COLUNA_TAXA_PADRAO) -> dict:
    """
    Ajusta TRÊS pares nominal x real, pra permitir comparar metodologias:

      - par "zero": LTN (Tesouro Prefixado) x NTN-B Principal (Tesouro IPCA+)
        -> ambos sao bullet, duration == prazo ate o vencimento nos dois
        lados. Par mais limpo metodologicamente.
      - par "cupom": NTN-F x NTN-B com Juros Semestrais -> ambos pagam cupom
        semestral, mais parecidos entre si do que zero-coupon vs cupom, mas
        nao identicos (cupom nominal != cupom real em tamanho).
      - par "misturado": TODOS os titulos nominais (LTN+NTN-F) numa curva so
        x TODOS os titulos reais (NTN-B+NTN-B c/juros) na outra -- o jeito
        que estava antes, misturando bullet com cupom na mesma curva.
        Simplificacao comum (varias casas fazem assim), mas mistura duration
        diferente no mesmo prazo nominal.

    Retorna {"zero": {...}|None, "cupom": {...}|None, "misturado": {...}|None}.
    Levanta ValueError só se os TRÊS pares vierem sem pontos suficientes.
    """
    par_zero = _ajusta_par(df, TITULOS_NOMINAIS_ZERO, TITULOS_REAIS_ZERO, data_base, coluna_taxa, "zero")
    par_cupom = _ajusta_par(df, TITULOS_NOMINAIS_CUPOM, TITULOS_REAIS_CUPOM, data_base, coluna_taxa, "cupom")
    par_misturado = _ajusta_par(df, TITULOS_NOMINAIS, TITULOS_REAIS, data_base, coluna_taxa, "misturado")

    if par_zero is None and par_cupom is None and par_misturado is None:
        raise ValueError(
            f"Pontos insuficientes nos TRÊS pares (zero-coupon, cupom e "
            f"misturado) para spline em {data_base.date()}."
        )

    return {"zero": par_zero, "cupom": par_cupom, "misturado": par_misturado}


def _breakeven_no_prazo(pares: dict, prazo: float, metodologia: str = "auto"):
    """Calcula o breakeven num prazo dado, segundo a metodologia escolhida:

      - "auto" (padrão): prefere o par 'zero' quando o prazo cai dentro da
        cobertura observada dele (metodologicamente mais limpo); só usa
        'cupom' se 'zero' não cobrir esse prazo ou não existir; nunca usa
        'misturado' (fica só disponível pra comparação/relatório).
      - "zero" / "cupom" / "misturado": força o uso daquele par específico
        (extrapola se o prazo cair fora da cobertura observada dele).
    """
    zero, cupom, misturado = pares.get("zero"), pares.get("cupom"), pares.get("misturado")

    if metodologia == "zero":
        par = zero
    elif metodologia == "cupom":
        par = cupom
    elif metodologia == "misturado":
        par = misturado
    elif metodologia == "auto":
        def dentro(p):
            return p is not None and p["prazo_min"] <= prazo <= p["prazo_max"]

        if dentro(zero):
            par = zero
        elif dentro(cupom):
            par = cupom
        elif zero is not None:
            par = zero
        else:
            par = cupom
    else:
        raise ValueError(f"metodologia desconhecida: {metodologia!r}")

    if par is None:
        raise ValueError(f"Par '{metodologia}' indisponível (sem pontos suficientes) nessa data-base.")

    taxa_nominal = float(par["spline_nominal"](prazo))
    taxa_real = float(par["spline_real"](prazo))
    return (1 + taxa_nominal) / (1 + taxa_real) - 1, par["nome"]


def calcular_projecao_mensal(df: pd.DataFrame, data_base: pd.Timestamp, n_meses: int = 24,
                              coluna_taxa: str = COLUNA_TAXA_PADRAO,
                              metodologia: str = "auto") -> dict:
    """
    Deriva a projeção de IPCA mês a mês a partir da curva de breakeven.
    Retorna {"YYYY-MM-01": taxa_mensal_decimal, ...} para os próximos n_meses
    meses a partir de data_base.

    metodologia:
      - "auto" (padrão): zero-coupon (LTN x NTN-B Principal) onde cobrir o
        prazo, cupom (NTN-F x NTN-B c/juros) fora dessa cobertura.
      - "zero": força só o par zero-coupon (mesmo que precise extrapolar).
      - "cupom": força só o par com cupom.
      - "misturado": força a curva antiga, todos os nominais x todos os
        reais numa curva só (útil pra comparar com o método novo).

    Nota: os primeiros meses podem cair abaixo do prazo mínimo coberto pelos
    títulos mais curtos disponíveis -- nesse caso a CubicSpline extrapola
    (comportamento padrão do scipy). Isso é esperado e geralmente aceitável
    para poucos meses de extrapolação; se a curva estiver com prazo_min muito
    distante (ex.: títulos curtos escassos), a extrapolação pode distorcer os
    primeiros meses -- vale checar prazo_min/prazo_max no retorno do log.
    """
    pares = ajustar_splines(df, data_base, coluna_taxa)

    projecao = {}
    cum_anterior = 1.0
    for m in range(1, n_meses + 1):
        data_mes = (data_base + pd.DateOffset(months=m)).replace(day=1)
        prazo = float(_prazo_du252(data_base, data_mes))

        breakeven, _par_usado = _breakeven_no_prazo(pares, prazo, metodologia)

        cum_atual = (1 + breakeven) ** prazo
        taxa_mensal = cum_atual / cum_anterior - 1
        cum_anterior = cum_atual

        projecao[data_mes.strftime("%Y-%m-01")] = taxa_mensal

    return projecao
