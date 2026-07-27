"""O prompt da Renata carrega regras que não podem sumir numa edição.

O teste central desta suíte é o golden (`test_prompt_e_identico_a_evidencia_
com_as_mudancas_documentadas`): ele lê docs/evidencias/prompt-renata-n8n.md,
aplica as transformações documentadas e exige igualdade byte a byte com
SYSTEM_PROMPT. Isso protege qualquer parágrafo do SOP, não só as âncoras
abaixo — as âncoras continuam existindo porque dão um erro legível (qual
trecho sumiu) quando o golden falha, em vez de só um diff gigante.
"""

from pathlib import Path

import pytest

from whatsapp_langchain.agents.catalog.elevec_sdr.prompts import SYSTEM_PROMPT
from whatsapp_langchain.agents.catalog.elevec_sdr.tools import TOOLS_ELEVEC

EVIDENCE_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "evidencias" / "prompt-renata-n8n.md"
)


def _extrair_bloco_verbatim() -> str:
    """Extrai o system message bruto do bloco "verbatim" da evidência."""
    texto = EVIDENCE_PATH.read_text(encoding="utf-8")
    apos_titulo = texto[texto.index("## System message (verbatim)") :]
    inicio_fence = apos_titulo.index("```\n") + len("```\n")
    fim_fence = apos_titulo.index("```", inicio_fence)
    return apos_titulo[inicio_fence:fim_fence]


def _aplicar_mudancas_documentadas(bruto: str) -> str:
    """Reproduz exatamente as transformações feitas em prompts.py.

    1. Remove o `=` inicial — marcador de modo-expressão do n8n, não
       conteúdo do prompt (ver nota em docs/evidencias/prompt-renata-n8n.md).
    2. As 4 expressões n8n de contexto viram os placeholders de interpolação.
    3. Acrescenta a instrução do marcador [figurinha] ao bloco de formatação.
    """
    texto = bruto.removeprefix("=")

    substituicoes = {
        "{{ $('Fields').item.json.pushName }}": "{nome}",
        "{{ $('Add New Lead').item.json.source }}": "{origem}",
        "{{ $('Fields').item.json.phone }}": "{telefone}",
        (
            "{{ $now.setZone('America/Sao_Paulo')"
            ".toFormat('dd/MM/yyyy HH:mm:ss') }} "
            "({{ $now.setZone('America/Sao_Paulo')"
            ".toFormat('EEEE') }})"
        ): "{data_hoje}",
    }
    for antiga, nova in substituicoes.items():
        ocorrencias = texto.count(antiga)
        assert ocorrencias == 1, (
            f"esperava 1 ocorrência de {antiga!r} na evidência, achei {ocorrencias}"
        )
        texto = texto.replace(antiga, nova)

    ancora_formatacao = "- Separe mensagens apenas usando múltiplos itens no array.\n"
    instrucao_figurinha = (
        "- Quando a mensagem do lead vier como `[figurinha]`, ele mandou uma"
        " figurinha\n"
        "  (sticker), não texto. Trate como uma reação positiva breve —"
        " reconheça e siga\n"
        "  a conversa do ponto em que estava. Não peça para ele mandar"
        " texto.\n"
    )
    assert texto.count(ancora_formatacao) == 1
    texto = texto.replace(ancora_formatacao, ancora_formatacao + instrucao_figurinha)

    return texto


def test_prompt_e_identico_a_evidencia_com_as_mudancas_documentadas():
    """Golden test: SYSTEM_PROMPT == evidência + as 2 mudanças documentadas.

    Qualquer deriva no SOP — apagar um parágrafo, reescrever uma frase,
    mudar a ordem de uma fase — quebra este teste, mesmo que nenhuma das
    âncoras abaixo tenha sumido.
    """
    esperado = _aplicar_mudancas_documentadas(_extrair_bloco_verbatim())
    assert SYSTEM_PROMPT == esperado


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


FASES_SOP = {
    0: "0. Identificação de Contexto (Início):",
    1: "1. Acolhimento:",
    2: "2. Diagnóstico & Filtro (Obrigatório):",
    3: "3. Aprofundamento:",
    4: "4. Ponte e Transição (O Micro-Compromisso):",
    5: "5. Disponibilidade de Agenda:",
    6: "6. Portão de E-mail:",
    7: "7. Portão de Faturamento (OBRIGATÓRIO ANTES DE AGENDAR):",
    8: "8. Agendamento (Final):",
}


@pytest.mark.parametrize("fase, cabecalho", sorted(FASES_SOP.items()))
def test_prompt_tem_as_nove_fases_do_sop(fase, cabecalho):
    """Fase 0 inclusa: é ela que impede a Renata de repetir a saudação."""
    assert f"\n{cabecalho}" in SYSTEM_PROMPT, (
        f"fase {fase} sumiu ou mudou de título no SOP"
    )


def test_prompt_exige_email_e_faturamento_antes_de_agendar():
    assert "INVIOLÁVEL" in SYSTEM_PROMPT
    assert "TERMINANTEMENTE PROIBIDO" in SYSTEM_PROMPT


def test_prompt_tem_placeholders_de_contexto():
    for campo in ("{nome}", "{origem}", "{telefone}", "{data_hoje}"):
        assert campo in SYSTEM_PROMPT


def test_prompt_nao_tem_residuo_de_sintaxe_n8n():
    assert "{{" not in SYSTEM_PROMPT
    assert "$now" not in SYSTEM_PROMPT


def test_toda_tool_citada_no_prompt_existe_no_agente():
    """O SOP manda chamar sete tools — o agente precisa ter as sete.

    O par natural das âncoras acima: elas garantem que o prompt continua
    citando `update_crm` e `human_handover`, isto garante que citá-las não
    é promessa vazia. Sem esta checagem, um `tools=TOOLS_AGENDA` esquecido
    em `agent.py` deixaria a Renata mandando o lead para um humano que
    nunca é avisado, sem nada quebrar em teste.
    """
    disponiveis = {tool.name for tool in TOOLS_ELEVEC}
    citadas = {
        "calendar_get_many",
        "calendar_agendar",
        "calendar_update",
        "calendar_delete",
        "calendar_get_event",
        "update_crm",
        "human_handover",
    }
    assert citadas <= disponiveis
    for nome in citadas:
        assert nome in SYSTEM_PROMPT
