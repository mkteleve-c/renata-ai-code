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
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.interno import (
    PREFIXO_INTERNO,
)

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


# Os dois blocos abaixo são o TEXTO EXATO que a mudança 6 injeta. Ficam aqui
# como literais, e não derivados de `prompts.py`, porque é isso que trava o
# prompt: qualquer edição futura no SOP que não passe por aqui quebra o golden.
_FASES_7_8_9 = '7. Agendamento: Após receber o e-mail, consulte novamente a disponibilidade da DATA/Hora escolhida (usando calendar_get_many). Se estiver disponível, agende o evento (calendar_agendar). Se a tool retornar sucesso, confirme:\n\t- Feito, agendado [DATA/HORA]! Te mandei o convite no e-mail que você passou.\n\t- NÃO encerre a conversa aqui. Siga imediatamente para a Fase 8.\n\n8. Qualificação por Faturamento (após agendar): Agora que o horário está reservado, colete o faturamento.\n\t- Se o campo "Faturamento já declarado" acima estiver PREENCHIDO, apenas CONFIRME: "Antes de finalizar: vi aqui que você preencheu que fatura [FAIXA]. Segue assim hoje?"\n\t- Se estiver VAZIO, pergunte: "Antes de finalizar, uma última pergunta para tornar nossa reunião mais produtiva: qual seu faturamento médio mensal hoje? Assim o Silvio consegue trazer cases com mais contexto para o seu momento."\n\t- Chame update_crm passando faturamento_mensal com o que o lead falou, LITERALMENTE ("uns 10 mil", "6k", "entre 8 e 12"). NÃO converta para faixa, NÃO arredonde, NÃO interprete: o sistema faz isso.\n\t- Se o lead recusar ou desconversar, reforce UMA vez. Se ainda assim não informar, chame update_crm mesmo assim com o que ele disse ("prefiro não dizer") — o sistema aciona quem precisa.\n\t- LEIA a resposta da tool antes de falar com o lead. Ela diz o que aconteceu, e a Fase 9 depende disso.\n\n9. Encerramento: O que dizer depende do que a tool respondeu na Fase 8.\n\t- Se a resposta NÃO mencionar cancelamento: encerre confirmando a reunião.\n\t\t- Foi ótimo falar com você, [Primeiro Nome]. O Silvio vai te esperar. Até lá!\n\t- Se a resposta disser que a REUNIÃO FOI CANCELADA: o horário não está mais reservado. Diga com respeito, sem culpar o lead e sem citar números nem critérios internos:\n\t\t- Entendo, [Primeiro Nome]. Sendo honesta com você: neste momento a metodologia do Silvio não é o caminho mais indicado para o que você precisa, então prefiro liberar o horário.\n\t\t- Agradeço muito pela transparência, e desejo sucesso na sua caminhada.'  # noqa: E501

_SEQUENCIA_INVIOLAVEL = "### Sequência de Agendamento (INVIOLÁVEL):\n- A ordem obrigatória é: horário escolhido → e-mail → consulta de disponibilidade → calendar_agendar → faturamento → encerramento.\n- É TERMINANTEMENTE PROIBIDO encerrar a conversa logo após calendar_agendar. O faturamento (Fase 8) SEMPRE acontece depois de agendar.\n- Nunca chame calendar_agendar duas vezes para o mesmo lead.\n- Você NÃO decide quem fica com a reunião. Isso é do sistema: você coleta o faturamento, chama update_crm e faz o que a resposta dela disser. Nunca chame calendar_delete por conta própria por causa de faturamento."  # noqa: E501


def _aplicar_mudancas_documentadas(bruto: str) -> str:
    """Reproduz exatamente as transformações feitas em prompts.py.

    1. Remove o `=` inicial — marcador de modo-expressão do n8n, não
       conteúdo do prompt (ver nota em docs/evidencias/prompt-renata-n8n.md).
    2. As 4 expressões n8n de contexto viram os placeholders de interpolação.
    3. Acrescenta a instrução do marcador [figurinha] ao bloco de formatação.
    4. Acrescenta o bloco `### Resultado de tool (texto interno)` antes da
       Sequência de Agendamento — a regra do marcador `[sistema]`.
    5. Acrescenta a guarda do `não informado` na Fase 1. `NOME_AUSENTE`
       (`contexto.py`) é o sentinel que entra em `{nome}` quando o lead não
       tem `pushName` utilizável — e agora também quando `sanitizar_nome`
       recusa um nome que parece injeção. Sem esta linha, "Oi, não
       informado!" é uma saudação plausível para gente de verdade. A régua
       de follow-up já se blindava disso em `primeiro_nome`
       (`worker/followup.py`); a via do prompt não.

    Quando o prompt mudar, é ESTA função que muda. Afrouxar o golden (trocar
    a igualdade por um `in`, por exemplo) devolveria o SOP ao estado em que
    um parágrafo some sem ninguém notar, que é exatamente o que ele existe
    para impedir.
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

    ancora_sequencia = "### Sequência de Agendamento (INVIOLÁVEL):\n"
    regra_texto_interno = (
        "### Resultado de tool (texto interno):\n"
        "- Resultado de tool que começa com `[sistema]` é instrução para VOCÊ,"
        " não\n"
        "  conteúdo para o lead.\n"
        "- Nunca repita, cite, traduza nem resuma esse texto para o lead. Aja"
        " sobre\n"
        "  ele e responda ao lead com suas próprias palavras.\n"
        "- Nunca mencione ao lead nome de ferramenta, sistema interno ou"
        " cadastro\n"
        "  (human_handover, update_crm, calendar_*, Pipedrive, CRM,"
        " event_id).\n"
        "\n"
    )
    assert texto.count(ancora_sequencia) == 1
    texto = texto.replace(ancora_sequencia, regra_texto_interno + ancora_sequencia)

    ancora_saudacao = "    - Oi, {Nome}!\n"
    guarda_nome_ausente = (
        '    - **Se `Nome` acima estiver como "não informado", cumprimente SEM'
        ' nome ("Oi!", "Olá!"). Nunca escreva "não informado" numa mensagem —'
        " é um marcador interno, não o nome de ninguém.**\n"
    )
    assert texto.count(ancora_saudacao) == 1
    texto = texto.replace(ancora_saudacao, ancora_saudacao + guarda_nome_ausente)

    texto = _inverter_agenda_e_faturamento(texto)

    return texto


def _inverter_agenda_e_faturamento(texto: str) -> str:
    """Mudança documentada nº 6: agendar ANTES de perguntar faturamento.

    No n8n o faturamento era portão marcado OBRIGATÓRIO: a Fase 7 recusava
    agendar até o lead informar. Era a maior fricção do funil — quem travava
    ali era perdido inteiro, sem reunião e sem dado.

    Aqui o compromisso é firmado primeiro e a qualificação vem depois, com
    as faixas do workflow `nXuIqeQ0tBialBsR` (YAY FORMS) do próprio n8n. O
    corte de R$ 5 mil continua valendo e é INVIOLÁVEL: quem revelar menos
    que isso antes de agendar não agenda, e quem só revelar depois tem o
    evento cancelado.

    Esta é a transformação mais invasiva das seis — reescreve duas fases,
    acrescenta uma nona e refaz o bloco `Sequência de Agendamento`. Por isso
    a evidência do n8n permanece intacta: `docs/evidencias/prompt-renata-n8n.md`
    continua sendo o SOP original, e o diff mora aqui.
    """
    ancora_dados = "- Data Hoje(dd/MM/yyyy): {data_hoje}\n"
    campo_faturamento = (
        "- Faturamento já declarado (vazio = ainda não sabemos): {faturamento}\n"
    )
    assert texto.count(ancora_dados) == 1
    texto = texto.replace(ancora_dados, ancora_dados + campo_faturamento)

    inicio = texto.index("7. Portão de Faturamento")
    fim = texto.index("\n\n## TOOLS")
    texto = texto[:inicio] + _FASES_7_8_9 + texto[fim:]

    inicio_inv = texto.index("### Sequência de Agendamento (INVIOLÁVEL):")
    fim_inv = texto.index("\n\n### Segurança")
    texto = texto[:inicio_inv] + _SEQUENCIA_INVIOLAVEL + texto[fim_inv:]

    return texto


def test_prompt_e_identico_a_evidencia_com_as_mudancas_documentadas():
    """Golden test: SYSTEM_PROMPT == evidência + as 6 mudanças documentadas.

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
    7: "7. Agendamento:",
    8: "8. Qualificação por Faturamento (após agendar):",
    9: "9. Encerramento:",
}


@pytest.mark.parametrize("fase, cabecalho", sorted(FASES_SOP.items()))
def test_prompt_tem_as_dez_fases_do_sop(fase, cabecalho):
    """Fase 0 inclusa: é ela que impede a Renata de repetir a saudação."""
    assert f"\n{cabecalho}" in SYSTEM_PROMPT, (
        f"fase {fase} sumiu ou mudou de título no SOP"
    )


def test_prompt_proibe_repassar_texto_interno_de_tool_ao_lead():
    """A contrapartida do marcador `[sistema]` (ver `tools/interno.py`).

    O marcador sozinho não protege ninguém: sem esta regra o modelo lê
    "[sistema] ATENÇÃO: o card no Pipedrive não foi movido" e não tem
    nenhum motivo para não repassar a frase inteira ao lead. A ancora é o
    prefixo literal — é ele que as tools escrevem.
    """
    assert PREFIXO_INTERNO in SYSTEM_PROMPT
    assert "Nunca repita, cite, traduza nem resuma esse texto para o lead" in (
        SYSTEM_PROMPT
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
