"""Texto livre do lead -> faixa de faturamento do funil da EleveC.

No n8n o faturamento chegava por múltipla escolha de formulário: a faixa já
vinha pronta. Numa conversa de WhatsApp o lead escreve "uns 10 mil", "entre
8 e 12", "faturo 3k". Alguém precisa mapear isso para a faixa, e esse
alguém **não pode ser o modelo**: medido com LLM real, ele grava "20 mil"
cru numa rodada e a faixa certa noutra.

`if` garante, prompt só pede (`docs/AGENTE_ELEVEC.md`). O prompt coleta o
número; esta função decide a faixa; o código aplica a consequência.

O corte que importa é **R$ 5 mil**: abaixo disso o lead perde a reunião.
Errar para baixo tira reunião de quem tinha direito; errar para cima enche
a agenda do Silvio. Por isso o indecidível devolve `None` — quem chama
aciona humano em vez de adivinhar.
"""

import pytest

from whatsapp_langchain.agents.catalog.elevec_sdr.faixas import (
    ABAIXO_DO_CORTE,
    ACIMA_DE_25K,
    FAIXAS,
    faixa_de_faturamento,
)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        # As seis opções do formulário, verbatim — é o que o n8n grava e o
        # que chega em `leads_crm.faturamento_mensal` pelos workflows.
        ("Menos de R$ 3 mil/mês", "Menos de R$ 3 mil/mês"),
        ("R$ 3 mil a R$ 5 mil/mês", "R$ 3 mil a R$ 5 mil/mês"),
        ("R$ 5 mil a R$ 8 mil/mês", "R$ 5 mil a R$ 8 mil/mês"),
        ("R$ 8mil a R$ 15 mil/mês", "R$ 8mil a R$ 15 mil/mês"),
        ("R$ 15 mil a R$ 25 mil/mês", "R$ 15 mil a R$ 25 mil/mês"),
        ("Acima de R$25 mil/mês", "Acima de R$25 mil/mês"),
    ],
)
def test_faixa_do_formulario_volta_identica(texto, esperado):
    assert faixa_de_faturamento(texto) == esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("uns 6 mil por mês", "R$ 5 mil a R$ 8 mil/mês"),
        ("6000", "R$ 5 mil a R$ 8 mil/mês"),
        ("R$ 6.000,00", "R$ 5 mil a R$ 8 mil/mês"),
        ("6k", "R$ 5 mil a R$ 8 mil/mês"),
        ("uns 10 mil", "R$ 8mil a R$ 15 mil/mês"),
        ("gira em torno de 20 mil", "R$ 15 mil a R$ 25 mil/mês"),
        ("uns 40 mil por mês", "Acima de R$25 mil/mês"),
        # 3.000 exatos NÃO é "menos de 3 mil" — sobe para a faixa seguinte.
        ("faturo 3 mil", "R$ 3 mil a R$ 5 mil/mês"),
        ("faturo 2999", "Menos de R$ 3 mil/mês"),
        ("uns 4 mil", "R$ 3 mil a R$ 5 mil/mês"),
        ("2.500", "Menos de R$ 3 mil/mês"),
        ("100 mil", "Acima de R$25 mil/mês"),
        ("1 milhão", "Acima de R$25 mil/mês"),
    ],
)
def test_texto_livre_vira_faixa(texto, esperado):
    assert faixa_de_faturamento(texto) == esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("entre 8 e 12 mil", "R$ 8mil a R$ 15 mil/mês"),
        # teto exclusivo no meio da escala: 8.000 é o piso da faixa de cima
        ("de 5 a 8 mil", "R$ 8mil a R$ 15 mil/mês"),
        ("de 5 a 7 mil", "R$ 5 mil a R$ 8 mil/mês"),
        ("uns 20 a 30 mil", "Acima de R$25 mil/mês"),
    ],
)
def test_intervalo_usa_o_teto(texto, esperado):
    """Intervalo casa pelo maior valor.

    "entre 3 e 6 mil" é um lead de 6k que está sendo conservador, não um de
    3k. Usar o piso desqualificaria quem passa do corte — e tirar reunião de
    quem tinha direito é o erro caro."""
    assert faixa_de_faturamento(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "",
        None,
        "   ",
        "prefiro não dizer",
        "depende do mês",
        "varia bastante",
        "sou CLT",
        "não tenho faturamento, sou funcionário",
        "abc",
    ],
)
def test_indecidivel_devolve_none(texto):
    """`None` não é 'zero': é 'não sei'. Quem chama aciona humano.

    Tratar "prefiro não dizer" como abaixo do corte cancelaria a reunião de
    alguém que talvez faturasse 50 mil."""
    assert faixa_de_faturamento(texto) is None


@pytest.mark.parametrize(
    ("texto", "abaixo"),
    [
        ("R$ 4.999", True),
        ("uns 5 mil", False),
        ("5000", False),
        ("Menos de R$ 3 mil/mês", True),
        ("R$ 3 mil a R$ 5 mil/mês", True),
        ("R$ 5 mil a R$ 8 mil/mês", False),
    ],
)
def test_o_corte_de_5k_cai_no_lugar_certo(texto, abaixo):
    """O limite exato: R$ 5.000 qualifica, R$ 4.999 não.

    É este teste que protege a regra que custa uma reunião."""
    faixa = faixa_de_faturamento(texto)
    assert (faixa in ABAIXO_DO_CORTE) is abaixo


def test_acima_de_25k_e_uma_faixa_so():
    assert faixa_de_faturamento("30 mil") == ACIMA_DE_25K
    assert faixa_de_faturamento("uns 26 mil") == ACIMA_DE_25K
    assert faixa_de_faturamento("25 mil") != ACIMA_DE_25K


def test_toda_faixa_devolvida_esta_no_catalogo():
    """Nenhuma string inventada: o valor gravado em `leads_crm` precisa ser
    idêntico ao que o formulário grava, senão o mesmo lead fica com duas
    grafias e o relatório não fecha."""
    entradas = ["3 mil", "4 mil", "6 mil", "10 mil", "20 mil", "40 mil"]
    for e in entradas:
        assert faixa_de_faturamento(e) in FAIXAS
