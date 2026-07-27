"""`update_crm` — a tool que move o lead no funil.

Porte do workflow `#4 CRM Control` do n8n. Três regras vieram de lá e são o
motivo de esta tool existir em vez de um `UPDATE` solto:

1. **Fase igual não é reescrita.** O prompt diz "nunca reescreva a mesma
   phase", e a diferença não é cosmética: reescrever `agendou_sessao` por
   cima de `agendou_sessao` desligaria `followup_active` de novo num lead
   que um humano pode ter reativado à mão, e mandaria o card ao Pipedrive
   toda vez que o modelo repetisse a chamada. Aqui a fase igual sai antes de
   tocar banco ou CRM.
2. **`agendou_sessao` e `desqualificado` desligam o follow-up.** Reunião
   marcada e lead descartado são os dois desfechos em que continuar cobrando
   pelo WhatsApp só queima a relação.
3. **`desqualificado` não move card.** O funil comercial não tem estágio de
   descarte — o card fica onde está e quem cuida disso é o time, não o
   agente.

**O telefone vem do turno, nunca do modelo.** Mesmo contrato das tools de
agenda (`telefone_do_turno`): um `phone` por argumento deixaria o modelo
desqualificar o lead da conversa anterior.

**Card primeiro, banco depois.** As duas escritas podem falhar isoladamente
e a ordem decide qual estado sobra:

- Pipedrive antes do banco: uma falha na gravação deixa o card movido e a
  fase antiga. A tool devolve falha, o agente repete, o `PUT` é idempotente
  e o banco é tentado de novo — converge.
- Banco antes do Pipedrive: uma falha no card deixa a fase nova gravada, e
  aí a regra 1 faz a repetição sair pelo curto-circuito de "fase igual". O
  card fica parado para sempre, sem ninguém saber.

Por isso o card vai primeiro. Falha dele **não** aborta a gravação: o
funil interno (que é quem alimenta a régua de follow-up e o gate de
ingestão) não pode ficar refém do CRM externo estar de pé. O que sobra é um
`logger.error` com `deal_id` e `stage_id` — tudo que a reconciliação manual
precisa — e um aviso no texto devolvido ao agente.

**`email` e `faturamento_mensal` passam a ser persistidos aqui.** Até a Task
5 nenhuma tool gravava esses dois campos, e por isso `calendar_agendar` os
aceitava por argumento — o que permitia ao modelo satisfazer a "sequência
INVIOLÁVEL" na mesma chamada, sem nunca ter perguntado ao lead. Com a
gravação acontecendo aqui, `_preferir_coluna` (em `agenda.py`) passa a ter
o que preferir: o valor que o lead **realmente disse** está no banco e vence
o argumento do turno.

**`name` NÃO é escrito por esta tool, de propósito.** `leads_crm.name` é
interpolado dentro do system prompt da Renata; hoje ele só nasce do
`pushName` do WhatsApp, que tem teto prático de ~25 caracteres. Dar ao
modelo um caminho de escrita nesse campo abriria uma origem sem teto para
texto que volta como instrução. `sanitizar_nome` continua sendo a defesa na
leitura — mas a defesa mais barata é não abrir a porta.
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
) -> bool:
    """Grava a fase (e o que mais vier) no lead. `False` = nada foi gravado.

    `followup_active = followup_active and not %s` desliga sem religar: uma
    fase que não desliga o follow-up deixa a coluna exatamente como estava,
    inclusive `false` posto por um `human_handover` anterior. Um
    `followup_active = %s` com `true` religaria a cobrança de um lead que um
    humano pausou.

    `email` e `faturamento_mensal` usam `coalesce(nullif(%s, ''), coluna)`:
    argumento vazio nunca apaga o que já está cadastrado.

    **Zero linhas afetadas é falha, não sucesso silencioso.** Lead ausente de
    `leads_crm` com o card já movido no Pipedrive é o caminho concreto de um
    funil interno que diverge do externo sem nada no log.
    """
    canonico = canonico_do_lead(telefone)
    desliga = phase in DESLIGAM_FOLLOWUP
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "update leads_crm set"
                "  phase = %s::lead_phase,"
                "  followup_active = followup_active and not %s,"
                "  email = coalesce(nullif(%s, ''), email),"
                "  faturamento_mensal ="
                "    coalesce(nullif(%s, ''), faturamento_mensal)"
                " where phone = %s",
                (
                    phase,
                    desliga,
                    email or "",
                    faturamento_mensal or "",
                    canonico,
                ),
            )
            afetadas = cur.rowcount
    except Exception as erro:
        logger.error(
            "crm_fase_nao_gravada", phone=canonico, phase=phase, erro=str(erro)
        )
        return False

    if afetadas == 0:
        logger.error("crm_lead_inexistente_na_gravacao", phone=canonico, phase=phase)
        return False

    logger.info(
        "crm_fase_gravada",
        phone=canonico,
        phase=phase,
        followup_desligado=desliga,
    )
    return True


async def mover_card(pipedriveid: Any, phase: str) -> tuple[bool, str]:
    """Move o card, se houver o que mover. Devolve `(moveu, motivo)`.

    Nunca levanta: falha de CRM externo não pode derrubar o turno nem
    impedir a gravação da fase no banco. `moveu=False` com `motivo=""`
    significa "não havia card para mover" — que é o caso normal de
    `desqualificado` e de lead sem `pipedriveid`, não um erro.
    """
    stage_id = stage_da_fase(phase)
    if stage_id is None:
        logger.info("crm_fase_nao_move_card", phase=phase)
        return False, ""

    deal_id = (pipedriveid or "").strip() if isinstance(pipedriveid, str) else ""
    if not deal_id:
        logger.info("crm_lead_sem_pipedriveid", phase=phase)
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
        return f"Fase '{phase}' não existe. Use uma de: {', '.join(FASES_PERMITIDAS)}."

    telefone = telefone_do_turno()
    if not telefone:
        logger.warning("crm_sem_telefone_no_config")
        return (
            "Não consegui identificar o lead nesta conversa e não vou "
            "atualizar o funil às cegas. Acione o human_handover."
        )

    estado = await carregar_estado(telefone)
    if estado is None:
        return (
            "Não encontrei este lead no cadastro, então não há fase para "
            "atualizar. Siga a conversa e acione o human_handover se "
            "precisar de registro manual."
        )

    if estado["phase"] == alvo:
        # Curto-circuito antes de banco e CRM: ver a regra 1 no docstring.
        logger.info("crm_fase_inalterada", phase=alvo)
        return f"O lead já está em '{alvo}'. Nada a atualizar."

    moveu, aviso_card = await mover_card(estado["pipedriveid"], alvo)

    gravou = await gravar_fase(
        telefone,
        alvo,
        email=email,
        faturamento_mensal=faturamento_mensal,
    )
    if not gravou:
        return (
            f"Não consegui registrar a fase '{alvo}' no cadastro do lead. "
            "Tente de novo; se persistir, acione o human_handover."
        )

    partes = [f"Fase atualizada para '{alvo}'."]
    if alvo in DESLIGAM_FOLLOWUP:
        partes.append("Follow-up automático desligado.")
    if moveu:
        partes.append("Card movido no Pipedrive.")
    if aviso_card:
        partes.append(f"ATENÇÃO: {aviso_card} — avise o time comercial.")

    return " ".join(partes)


TOOLS_CRM = [update_crm]
