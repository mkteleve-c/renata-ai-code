"""`update_crm` — a tool que move o lead no funil.

Porte do workflow `#4 CRM Control` do n8n. Três regras vieram de lá e são o
motivo de esta tool existir em vez de um `UPDATE` solto:

1. **Fase igual não é reescrita.** O prompt diz "nunca reescreva a mesma
   phase", e a diferença não é cosmética: reescrever `agendou_sessao` por
   cima de `agendou_sessao` desligaria `followup_active` de novo num lead
   que um humano pode ter reativado à mão, e mandaria o card ao Pipedrive
   toda vez que o modelo repetisse a chamada.
2. **`agendou_sessao` e `desqualificado` desligam o follow-up.** Reunião
   marcada e lead descartado são os dois desfechos em que continuar cobrando
   pelo WhatsApp só queima a relação.
3. **`desqualificado` não move card.** O funil comercial não tem estágio de
   descarte — o card fica onde está e quem cuida disso é o time, não o
   agente.

**O telefone vem do turno, nunca do modelo.** Mesmo contrato das tools de
agenda (`telefone_do_turno`): um `phone` por argumento deixaria o modelo
desqualificar o lead da conversa anterior.

**Quem decide a mudança é o `UPDATE`, não uma comparação em Python.** A
regra 1 parece um `if` e não é: ler a fase, comparar e depois gravar é um
TOCTOU, e as duas janelas existem de verdade neste sistema — o `ToolNode` do
LangGraph executa em paralelo as `tool_calls` de uma mesma mensagem do
modelo, e o `claim_next` usa `FOR UPDATE SKIP LOCKED` sem serializar por
telefone, então duas réplicas do worker processam turnos do mesmo lead ao
mesmo tempo. Medido: duas `update_crm` concorrentes com alvos diferentes
moviam o card **duas vezes** e deixavam a fase final não determinística.

A guarda mora no SQL — `where phone = %s and phase is distinct from
%s::lead_phase` — e **só quem casou uma linha move o card**. A serialização
é do Postgres: das duas transações concorrentes, a segunda reavalia o
predicado depois do commit da primeira e casa zero linhas.

**O card vem depois do banco, e a ordem não é o que evita divergência.**
Registrado com precisão porque a versão anterior deste texto vendia demais:
com a gravação não abortando por falha de card, **as duas ordens** deixam o
mesmo estado divergente possível — card falha, fase grava, a próxima
chamada casa zero linhas pela guarda e o card fica atrás. O que a ordem
banco-primeiro compra é a atomicidade da regra 1 (sem ela não há guarda), e
o que sobra da falha de card é `crm_card_nao_movido` com `deal_id` e
`stage_id` no log, mais um aviso no texto devolvido ao agente. É um caminho
de reconciliação manual, não uma garantia de convergência.

Falha do Pipedrive não aborta a gravação de propósito: o funil interno é
quem alimenta a régua de follow-up e o gate de ingestão, e não pode ficar
refém de o CRM externo estar de pé.

**`email` e `faturamento_mensal` passam a ser persistidos aqui.** Até a Task
5 nenhuma tool gravava esses dois campos, e por isso `calendar_agendar` os
aceitava por argumento — o que permitia ao modelo satisfazer a "sequência
INVIOLÁVEL" na mesma chamada, sem nunca ter perguntado ao lead. Com a
gravação acontecendo aqui, `_preferir_coluna` (em `agenda.py`) passa a ter
o que preferir. **O portão foi condicionado, não fechado**: `_preferir_coluna`
é `coluna or argumento`, então a coluna só vence quando está preenchida —
sem um `update_crm` prévio, um e-mail inventado pelo modelo ainda satisfaz a
sequência. Fechar de vez é trabalho da Task 7 (ver o relatório).

**`name` NÃO é escrito por esta tool, de propósito.** `leads_crm.name` é
interpolado dentro do system prompt da Renata; hoje ele só nasce do
`pushName` do WhatsApp, que tem teto prático de ~25 caracteres. Dar ao
modelo um caminho de escrita nesse campo abriria uma origem sem teto para
texto que volta como instrução. `sanitizar_nome` continua sendo a defesa na
leitura — mas a defesa mais barata é não abrir a porta.

**Todo retorno desta tool é marcado `[sistema]`** — nenhum deles carrega
conteúdo que o lead precise ouvir. Ver `interno.py`.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.tools import tool

from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.phone import canonico_do_lead
from whatsapp_langchain.shared.pipedrive import PipedriveClient

from ..contexto import telefone_do_turno
from .interno import interno

logger = structlog.get_logger()

# As três fases que o SOP dá à Renata. `formulario_preenchido` e
# `iniciou_conversa` são escritas pelo gate de ingestão, e `perdido` é
# julgamento do time comercial — nenhuma delas é decisão do agente.
FASES_PERMITIDAS = ("qualificado", "agendou_sessao", "desqualificado")

# Reunião marcada e lead descartado encerram a régua de cobrança.
DESLIGAM_FOLLOWUP = frozenset({"agendou_sessao", "desqualificado"})

_cliente: PipedriveClient | None = None


def obter_cliente() -> PipedriveClient:
    """Cliente único do processo. Token ausente levanta `ValueError`."""
    global _cliente
    if _cliente is None:
        _cliente = PipedriveClient(api_token=settings.pipedrive_api_token)
    return _cliente


def resetar_cliente() -> None:
    """Descarta o cliente guardado. Existe para os testes."""
    global _cliente
    _cliente = None


def stage_da_fase(phase: str) -> int | None:
    """`stage_id` do Pipedrive para a fase, ou `None` quando não move card.

    Lê `settings` a cada chamada em vez de congelar um dicionário no import:
    os dois valores são configuráveis por ambiente e um mapa de módulo
    ficaria preso ao que estava no `.env` na hora do boot do processo.
    """
    if phase == "qualificado":
        return settings.pipedrive_stage_qualificado
    if phase == "agendou_sessao":
        return settings.pipedrive_stage_agendado
    return None


async def carregar_estado(telefone: str) -> dict[str, Any] | None:
    """Fase atual e `pipedriveid` do lead. Nunca levanta."""
    canonico = canonico_do_lead(telefone)
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select phase, pipedriveid from leads_crm where phone = %s",
                (canonico,),
            )
            linha = await cur.fetchone()
    except Exception as erro:
        logger.warning("crm_lead_nao_lido", phone=canonico, erro=str(erro))
        return None

    if linha is None:
        logger.info("crm_lead_ausente", phone=canonico)
        return None

    return {"phase": linha[0], "pipedriveid": linha[1]}


async def gravar_fase(
    telefone: str,
    phase: str,
    email: str = "",
    faturamento_mensal: str = "",
) -> str:
    """Grava a fase **se ela for diferente da atual**. Devolve o que houve.

    Desfechos: `"gravou"`, `"inalterada"` (a fase já era essa — ninguém
    escreveu nada), `"recusada"` (a mudança seria um retrocesso), `"ausente"`
    (não há linha para este telefone) e `"erro"`. Quem chama decide o card e
    a frase a partir daí.

    **`qualificado` nunca sobrescreve `agendou_sessao`.** É a mesma doutrina
    que `shared/leads.py` já aplica na consolidação de duplicatas —
    *"agendou_sessao vence tudo: reunião marcada é fato verificável (existe
    evento no Google Calendar)"*. Sem essa recusa, duas chamadas concorrentes
    com alvos contraditórios deixam o funil andando para trás: medido, em 1
    de 5 rodadas o lead terminava em `qualificado` com a reunião marcada na
    agenda do Silvio. `desqualificado` continua podendo sobrescrever — não é
    estágio anterior, é saída, e um lead pode revelar um desqualificador
    depois de agendar.

    **A regra "nunca reescreva a mesma phase" mora aqui, no `where`.** Fazer
    a comparação em Python — ler, comparar, gravar — é um TOCTOU, e as duas
    janelas existem neste sistema (ver o docstring do módulo). Medido contra
    o banco: duas chamadas concorrentes com alvos diferentes moviam o card
    duas vezes e deixavam a fase final não determinística. Com o predicado
    no `UPDATE`, das duas transações a segunda reavalia depois do commit da
    primeira e casa zero linhas — e quem casa zero linhas não move card.

    `is distinct from` e não `<>`: a coluna é nullable (a 007 declara
    `DEFAULT`, não `NOT NULL`), e `phase <> 'qualificado'` é NULL — logo
    falso — para um lead com fase nula, que nunca sairia do lugar.

    **A recusa vale enquanto o evento existir.** O predicado carrega
    `google_event_id is not null` porque é ele que sustenta a doutrina: em
    `shared/leads.py` a razão de `agendou_sessao` vencer é *"reunião marcada
    é fato verificável (existe evento no Google Calendar)"*. Sem evento
    vinculado não há fato a preservar, e `qualificado` deixa de ser
    retrocesso — vira a volta legítima de uma reunião cancelada. Sem essa
    condição a fase virava terminal: `agenda.py` não escreve `phase` em
    lugar nenhum, então um lead com a reunião cancelada ficava preso em
    `agendou_sessao` com o follow-up morto e nenhum caminho de volta.

    `followup_active` tem duas regras, e a ordem do `case` importa:

    - **voltar de `agendou_sessao` para `qualificado` religa a cobrança**,
      mas só se `agent_active` — o lead precisa ser perseguido para
      remarcar, exceto quando um `human_handover` pausou o agente.
    - qualquer outra transição usa `followup_active and not %s`, que desliga
      sem religar: um `followup_active = %s` com `true` religaria a cobrança
      de um lead que uma pessoa pausou.

    O lado direito do `SET` enxerga a linha **antiga**, então `phase` dentro
    do `case` é a fase de origem — é o que permite decidir pelo par
    (origem, destino) sem um `select` extra.

    `email` e `faturamento_mensal` usam `coalesce(nullif(%s, ''), coluna)`:
    argumento vazio nunca apaga o que já está cadastrado.

    **Zero linhas afetadas nunca é sucesso.** Distinguir "não existe" de "já
    estava nessa fase" custa um `select` no mesmo bloco de conexão e evita
    reportar lead ausente como se fosse corrida — dois incidentes bem
    diferentes.
    """
    canonico = canonico_do_lead(telefone)
    desliga = phase in DESLIGAM_FOLLOWUP
    gravada: tuple[Any, ...] | None = None
    atual: tuple[Any, ...] | None = None
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "update leads_crm set"
                "  phase = %s::lead_phase,"
                "  followup_active = case"
                "    when %s::lead_phase = 'qualificado'"
                "     and phase = 'agendou_sessao' then agent_active"
                "    else followup_active and not %s"
                "  end,"
                "  email = coalesce(nullif(%s, ''), email),"
                "  faturamento_mensal ="
                "    coalesce(nullif(%s, ''), faturamento_mensal)"
                " where phone = %s"
                "   and phase is distinct from %s::lead_phase"
                "   and not ("
                "     %s::lead_phase = 'qualificado'"
                "     and phase = 'agendou_sessao'"
                "     and google_event_id is not null"
                "   )"
                " returning phase",
                (
                    phase,
                    phase,
                    desliga,
                    email or "",
                    faturamento_mensal or "",
                    canonico,
                    phase,
                    phase,
                ),
            )
            gravada = await cur.fetchone()

            if gravada is None:
                cur = await conn.execute(
                    "select phase from leads_crm where phone = %s", (canonico,)
                )
                atual = await cur.fetchone()
    except Exception as erro:
        logger.error(
            "crm_fase_nao_gravada", phone=canonico, phase=phase, erro=str(erro)
        )
        return "erro"

    if gravada is None:
        if atual is None:
            logger.error(
                "crm_lead_inexistente_na_gravacao", phone=canonico, phase=phase
            )
            return "ausente"
        if atual[0] == phase:
            logger.info("crm_fase_ja_estava_aplicada", phone=canonico, phase=phase)
            return "inalterada"
        logger.warning(
            "crm_retrocesso_de_fase_recusado",
            phone=canonico,
            atual=atual[0],
            pedida=phase,
        )
        return "recusada"

    logger.info(
        "crm_fase_gravada",
        phone=canonico,
        phase=phase,
        followup_desligado=desliga,
    )
    return "gravou"


async def reverter_fase_apos_cancelamento(telefone: str) -> str:
    """Devolve o lead de `agendou_sessao` para `qualificado`. Nunca levanta.

    Desfechos: `"revertida"`, `"nao_aplicavel"` (o lead não estava em
    `agendou_sessao`) e `"erro"`.

    **Quem cancela a reunião é quem mexe na fase.** Deixar isso a cargo de o
    modelo lembrar de chamar `update_crm` depois de `calendar_delete` seria
    o mesmo erro que este projeto vem trocando por `if`: três parágrafos
    pedem, um `if` garante. `calendar_delete` chama esta função no caminho
    de sucesso e o funil anda junto com a agenda, atomicamente do ponto de
    vista de quem olha o CRM.

    **`where phase = 'agendou_sessao'` faz o trabalho de três guardas.** Ele
    (a) impede que um lead `desqualificado` que tinha reunião marcada seja
    promovido de volta a `qualificado` por um cancelamento, (b) torna a
    função idempotente — cancelar duas vezes não reescreve nada — e (c)
    dispensa ler a fase antes.

    **`followup_active = agent_active` é a religada com trava.** O lead sem
    reunião precisa voltar para a régua de cobrança, senão o cancelamento o
    aposenta em silêncio. Mas `human_handover` desliga `agent_active`, e
    religar a cobrança de um lead que uma pessoa pausou desfaria a decisão
    dela. Copiar `agent_active` resolve os dois casos numa expressão.
    """
    canonico = canonico_do_lead(telefone)
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "update leads_crm set"
                "  phase = 'qualificado'::lead_phase,"
                "  followup_active = agent_active"
                " where phone = %s and phase = 'agendou_sessao'"
                " returning followup_active",
                (canonico,),
            )
            linha = await cur.fetchone()
    except Exception as erro:
        logger.error("crm_reversao_falhou", phone=canonico, erro=str(erro))
        return "erro"

    if linha is None:
        logger.info("crm_reversao_nao_aplicavel", phone=canonico)
        return "nao_aplicavel"

    logger.info(
        "crm_fase_revertida_apos_cancelamento",
        phone=canonico,
        followup_religado=linha[0],
    )
    return "revertida"


async def mover_card(pipedriveid: Any, phase: str) -> tuple[bool, str]:
    """Move o card, se houver o que mover. Devolve `(moveu, motivo)`.

    Nunca levanta: falha de CRM externo não pode derrubar o turno nem
    desfazer a fase já gravada. `moveu=False` com `motivo=""` significa "não
    havia card para mover" — o caso normal de `desqualificado`, de lead sem
    `pipedriveid` e de deploy sem Pipedrive configurado, nenhum deles um
    erro.

    **Token ausente é configuração, não incidente.** Sem essa checagem o
    `ValueError` do construtor cairia no `except` genérico e *toda* transição
    para `qualificado`/`agendou_sessao` devolveria "o card não foi movido —
    avise o time comercial", indistinguível de CRM fora do ar. Deploy sem
    Pipedrive é uma escolha válida.
    """
    stage_id = stage_da_fase(phase)
    if stage_id is None:
        logger.info("crm_fase_nao_move_card", phase=phase)
        return False, ""

    deal_id = (pipedriveid or "").strip() if isinstance(pipedriveid, str) else ""
    if not deal_id:
        logger.info("crm_lead_sem_pipedriveid", phase=phase)
        return False, ""

    if not settings.pipedrive_api_token.strip():
        logger.info("crm_pipedrive_nao_configurado", phase=phase, deal_id=deal_id)
        return False, ""

    try:
        await obter_cliente().mover_card(deal_id, stage_id)
    except Exception as erro:
        logger.error(
            "crm_card_nao_movido",
            deal_id=deal_id,
            stage_id=stage_id,
            phase=phase,
            erro=str(erro),
        )
        return False, "o card no Pipedrive não foi movido"

    return True, ""


@tool
async def update_crm(
    phase: str,
    email: str = "",
    faturamento_mensal: str = "",
) -> str:
    """Registra a mudança de fase do lead no funil.

    Só chame quando houver mudança REAL de estágio — nunca para reescrever a
    fase em que o lead já está.

    Args:
        phase: `qualificado` (passou no filtro de qualificação),
            `agendou_sessao` (consultoria marcada com sucesso) ou
            `desqualificado` (bateu num fator de desqualificação).
        email: e-mail do lead, se você acabou de recebê-lo. Registrar aqui
            evita ter que repeti-lo na hora de agendar.
        faturamento_mensal: faturamento médio mensal como o lead falou
            ("uns 30 mil"), se você acabou de recebê-lo.
    """
    alvo = (phase or "").strip().lower()
    if alvo not in FASES_PERMITIDAS:
        return interno(
            f"Fase '{phase}' não existe. Use uma de: {', '.join(FASES_PERMITIDAS)}."
        )

    telefone = telefone_do_turno()
    if not telefone:
        logger.warning("crm_sem_telefone_no_config")
        return interno(
            "Não consegui identificar o lead nesta conversa e não vou "
            "atualizar o funil às cegas. Acione o human_handover."
        )

    # Leitura adiantada só para o `pipedriveid` e para a recusa amigável da
    # fase repetida. Ela NÃO é a guarda — a guarda é o `where` de
    # `gravar_fase`, e é ela que decide se o card se move.
    estado = await carregar_estado(telefone)
    if estado is None:
        return interno(
            "Não encontrei este lead no cadastro, então não há fase para "
            "atualizar. Siga a conversa e acione o human_handover se "
            "precisar de registro manual."
        )

    if estado["phase"] == alvo:
        logger.info("crm_fase_inalterada", phase=alvo)
        return interno(f"O lead já está em '{alvo}'. Nada a atualizar.")

    resultado = await gravar_fase(
        telefone,
        alvo,
        email=email,
        faturamento_mensal=faturamento_mensal,
    )

    if resultado == "erro":
        return interno(
            f"Não consegui registrar a fase '{alvo}' no cadastro do lead. "
            "Tente de novo; se persistir, acione o human_handover."
        )

    if resultado == "ausente":
        return interno(
            f"O lead sumiu do cadastro antes de eu gravar a fase '{alvo}'. "
            "Acione o human_handover para registro manual."
        )

    if resultado == "inalterada":
        # Outra execução chegou nesta fase primeiro (turnos concorrentes do
        # mesmo lead). Ela já moveu o card; mover de novo seria duplicar.
        return interno(f"O lead já está em '{alvo}'. Nada a atualizar.")

    if resultado == "recusada":
        # Só chega aqui com o evento AINDA vinculado ao lead. Cancelar de
        # verdade é o que libera a volta — e `calendar_delete` já devolve a
        # fase sozinho, então esta instrução é verificável.
        return interno(
            "Este lead tem sessão agendada e o evento continua na agenda, "
            f"então não vou voltar o funil para '{alvo}'. Se a reunião foi "
            "cancelada, chame calendar_delete — ele já devolve o lead para "
            "'qualificado'. Se ele foi desqualificado, use 'desqualificado'."
        )

    moveu, aviso_card = await mover_card(estado["pipedriveid"], alvo)

    partes = [f"Fase atualizada para '{alvo}'."]
    if alvo in DESLIGAM_FOLLOWUP:
        partes.append("Follow-up automático desligado.")
    if moveu:
        partes.append("Card movido no Pipedrive.")
    if aviso_card:
        partes.append(f"ATENÇÃO: {aviso_card} — avise o time comercial.")

    return interno(" ".join(partes))


TOOLS_CRM = [update_crm]
