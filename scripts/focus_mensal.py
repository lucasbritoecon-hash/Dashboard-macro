"""
Busca a mediana do Focus mensal (Boletim Focus) pro IPCA, mês de referência a
mês de referência, pra comparar com a curva implícita do Tesouro Direto
(tesouro_implicita.py) e com a projeção implícita a partir de LTN/NTN-F x
NTN-B.

Fonte: API Olinda/BCB, recurso "ExpectativaMercadoMensais" (não confundir com
"ExpectativasMercadoAnuais", que é a expectativa pro IPCA fechado do ano, nem
com "ExpectativasMercadoInflacao12Meses", que é a expectativa rolante pros
próximos 12 meses -- aqui queremos a mediana POR MÊS DE REFERÊNCIA específico,
ex.: "qual a mediana das expectativas de mercado pro IPCA de agosto/2026").

A API devolve o histórico completo de pesquisas (uma linha por dia em que
alguma instituição atualizou a expectativa); pra cada DataReferencia (mês
alvo) ficamos só com a pesquisa mais recente (maior "Data").

Documentação: https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/aplicacao
"""

from datetime import datetime
from urllib.parse import quote, urlencode

import requests

URL_FOCUS_MENSAL = (
    "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/"
    "ExpectativaMercadoMensais"
)


def buscar_focus_mensal(indicador: str, base_calculo: int = 0) -> dict:
    """
    Versão genérica de buscar_focus_mensal_ipca (ver abaixo) para qualquer
    indicador do Focus mensal, não só IPCA -- por exemplo "Taxa de
    desocupação" (usado no painel de Mercado de Trabalho).

    Retorna {"YYYY-MM-01": mediana_bruta, ...} com a mediana MAIS RECENTE do
    Focus mensal pra cada mês de referência disponível (histórico completo,
    não só os próximos N meses -- quem consome filtra o horizonte que quiser).
    A mediana vem NA MESMA UNIDADE que a API devolve (para IPCA, por
    exemplo, em % -- quem consome decide se converte pra fração).

    base_calculo:
      - 0 (padrão): expectativas informadas nos últimos 30 dias antes do
        cálculo da estatística (janela mais larga, mais estável).
      - 1: só expectativas dos últimos 4 dias úteis (mais sensível a notícia
        recente, mais próximo do que o mercado pensa "hoje").
    """
    # Esse endpoint (ExpectativaMercadoMensais) quebra com $filter usando
    # baseCalculo, com ou sem aspas -- sempre devolve 400 "Edm.Boolean e
    # Edm.String nao sao compativeis" (bug conhecido da API do BCB, o mesmo
    # workaround aparece em projetos de terceiros que consomem esse recurso:
    # eles nao filtram baseCalculo via OData, filtram depois em pandas). Por
    # isso aqui so filtramos por Indicador no $filter e filtramos baseCalculo
    # no lado do cliente, depois de baixar os dados.
    params = {
        "$filter": f"Indicador eq '{indicador}'",
        "$orderby": "Data desc",
        "$top": 20000,
        "$format": "json",
        "$select": "Indicador,Data,DataReferencia,Mediana,baseCalculo",
    }
    resp = requests.get(
        URL_FOCUS_MENSAL,
        params=urlencode(params, quote_via=quote),
        timeout=60,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # A API do BCB devolve o motivo do 400 no corpo -- sem isso, fica só
        # adivinhando. Se acontecer de novo, o texto abaixo diz exatamente
        # qual parâmetro ela não engoliu.
        print(f"Corpo da resposta ({resp.status_code}): {resp.text[:1000]}")
        raise
    valores = resp.json()["value"]

    # baseCalculo pode vir como int (0/1) ou string ("0"/"1") dependendo do
    # dia/registro -- compara os dois jeitos pra não perder linha por causa
    # de tipo.
    valores = [
        item for item in valores
        if item.get("baseCalculo") in (base_calculo, str(base_calculo))
    ]

    # DataReferencia vem como "MM/YYYY" (mês-alvo da expectativa); Data vem
    # como "YYYY-MM-DD" (dia em que a pesquisa foi consolidada). Fica só a
    # pesquisa mais recente por mês-alvo.
    mais_recente = {}
    for item in valores:
        ref = item["DataReferencia"]
        data_pesquisa = item["Data"]
        mediana = item["Mediana"]
        if mediana is None:
            continue
        if ref not in mais_recente or data_pesquisa > mais_recente[ref][0]:
            mais_recente[ref] = (data_pesquisa, mediana)

    projecao = {}
    for ref, (_data_pesquisa, mediana) in mais_recente.items():
        mes_str, ano_str = ref.split("/")
        data_str = datetime(int(ano_str), int(mes_str), 1).strftime("%Y-%m-01")
        projecao[data_str] = mediana
    return projecao


def buscar_focus_mensal_ipca(base_calculo: int = 0) -> dict:
    """
    Wrapper específico pro IPCA (mantido por compatibilidade com quem já
    importava esta função) -- devolve a mediana já em FRAÇÃO (dividida por
    100), como antes.

    OBSERVAÇÃO: a linha "Focus" do gráfico reflete a mediana das expectativas
    de mercado coletadas pelo BCB nos ÚLTIMOS 30 DIAS (base_calculo=0) -- não
    é só a pesquisa do dia mais recente. Se algum dia quisermos a leitura
    "mais quente" (últimos 4 dias úteis), é só chamar com base_calculo=1.
    """
    bruto = buscar_focus_mensal("IPCA", base_calculo=base_calculo)
    return {data_str: mediana / 100 for data_str, mediana in bruto.items()}


if __name__ == "__main__":
    focus = buscar_focus_mensal_ipca()
    for data_str in sorted(focus):
        print(data_str, round(focus[data_str] * 100, 2), "%")
