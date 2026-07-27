"""O prompt da Renata carrega regras que não podem sumir numa edição."""

import pytest

from whatsapp_langchain.agents.catalog.elevec_sdr.prompts import SYSTEM_PROMPT


@pytest.mark.parametrize(
    "trecho",
    [
        "Renata",
        "EleveC",
        "Silvio Hirata",
        "Consultoria de Alavancagem de Carreira",
        "faturamento",
        "human_handover",
        "update_crm",
        "calendar_get_many",
        "calendar_agendar",
        "[figurinha]",
    ],
)
def test_prompt_mantem_ancoras(trecho):
    assert trecho in SYSTEM_PROMPT


def test_prompt_tem_as_oito_fases_do_sop():
    for n in range(1, 9):
        assert f"\n{n}." in SYSTEM_PROMPT, f"fase {n} sumiu do SOP"


def test_prompt_exige_email_e_faturamento_antes_de_agendar():
    assert "INVIOLÁVEL" in SYSTEM_PROMPT
    assert "TERMINANTEMENTE PROIBIDO" in SYSTEM_PROMPT


def test_prompt_tem_placeholders_de_contexto():
    for campo in ("{nome}", "{origem}", "{telefone}", "{data_hoje}"):
        assert campo in SYSTEM_PROMPT
