"""Webhook do ChatWoot — a etiqueta `pausar_agente` desliga a Renata.

É o mecanismo pelo qual um humano assume uma conversa: o atendente aplica a
etiqueta no ChatWoot e o agente para de responder aquele lead. Enquanto esta
rota não existiu, aplicar a etiqueta **não fazia nada** no harness — a Renata
continuava respondendo por cima do atendimento humano, sem erro, sem log de
falha, e o único sinal era ela falando junto.

O contrato veio do workflow `#1.1 Handover to Human via ChatWoot com
Etiqueta` do n8n (nó `Code in JavaScript`), lido do próprio painel, não
suposto:

    {
      "event": "conversation_updated",
      "labels": ["pausar_agente", "vip"],
      "meta": {"sender": {"identifier": "5511999999999@s.whatsapp.net"}}
    }

**Não são três eventos, é um.** O ChatWoot manda a lista COMPLETA de
etiquetas a cada mudança, então "adicionou" e "removeu" são presença e
ausência no mesmo array. O n8n resolvia isso com um `Switch` de dois ramos
sobre um campo derivado; aqui é um `if`.

Duas divergências deliberadas em relação ao n8n:

1. **O telefone é canonicalizado.** Lá o `WHERE` usava os dígitos crus do
   identifier (`5511999999999`, com o 9º dígito). No harness
   `leads_crm.phone` é canônico, garantido pelo CHECK da migração 014 — o
   `UPDATE` do n8n não casaria nenhuma linha aqui, e a etiqueta pareceria
   funcionar (200, sem erro) sem pausar ninguém.
2. **As duas formas são consultadas** (`variacoes`), mesma regra do gate e
   da blocklist: a etiqueta precisa alcançar o lead independentemente da
   forma que o ChatWoot mandar.

A assimetria entre ligar e desligar é do n8n e foi mantida de propósito —
ver `_SQL_DESLIGAR`/`_SQL_LIGAR` abaixo.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request

from whatsapp_langchain.server.dependencies import verify_chatwoot_webhook_secret
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.phone import canonicalizar, variacoes

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

ETIQUETA_PAUSA = "pausar_agente"

# Desligar mexe em TRÊS colunas; religar mexe em UMA. A assimetria é do n8n e
# está certa: religar `followup_active` junto faria a régua voltar a
# perseguir alguém que uma pessoa pausou. É a mesma doutrina de
# `update_crm` (`followup_active and not %s`, tools/crm.py) e de
# `_vencedor_pausa` (shared/leads.py) — errar para o lado de não mandar
# mensagem é recuperável; mandar para quem pediu silêncio, não.
#
# `agent_reactivate_at = NULL` também vem de lá: a pausa por etiqueta não
# tem prazo, quem religa é o humano tirando a etiqueta.
_SQL_DESLIGAR = """
update leads_crm
set agent_active = false, followup_active = false, agent_reactivate_at = null
where phone in (%s, %s)
"""

_SQL_LIGAR = """
update leads_crm
set agent_active = true
where phone in (%s, %s)
"""


def _identifier(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    sender = meta.get("sender") if isinstance(meta, dict) else None
    valor = sender.get("identifier") if isinstance(sender, dict) else None
    return valor if isinstance(valor, str) and valor else None


def _etiquetas(payload: dict[str, Any]) -> list[str]:
    bruto = payload.get("labels")
    if isinstance(bruto, list):
        return [item for item in bruto if isinstance(item, str)]
    return []


@router.post("/webhook/chatwoot")
async def webhook_chatwoot_receive(
    request: Request,
    _secret: None = Depends(verify_chatwoot_webhook_secret),
) -> dict[str, Any]:
    """Aplica (ou remove) a pausa do agente conforme as etiquetas.

    Sempre 200, mesma doutrina de `webhook_evolution`: o ChatWoot reentrega
    em resposta >= 400, e nenhuma reentrega conserta payload malformado ou
    lead inexistente. A exceção é o 401 do secret, na dependency.
    """
    try:
        payload = await request.json()
    except Exception:
        logger.error("chatwoot_body_invalido")
        return {"status": "ignored", "motivo": "body_invalido"}

    if not isinstance(payload, dict):
        logger.error("chatwoot_body_nao_e_objeto", tipo=type(payload).__name__)
        return {"status": "ignored", "motivo": "body_invalido"}

    identifier = _identifier(payload)
    if not identifier:
        # `evento=`, não `event=`: structlog reserva `event` para o nome da
        # mensagem, e o kwarg colide com TypeError em tempo de execução —
        # transformando um payload malformado (que deve responder 200) num
        # 500, que faz o ChatWoot reentregar em loop.
        logger.warning("chatwoot_sem_identifier", evento=payload.get("event"))
        return {"status": "ignored", "motivo": "sem_identifier"}

    canonico = canonicalizar(identifier)
    if not canonico:
        # Grupo (`@g.us`), LID ou telefone malformado: `canonicalizar` recusa
        # e não há lead para alcançar. Criar um aqui seria pior — a etiqueta
        # nunca deve inventar identidade.
        logger.info("chatwoot_identifier_irresolvivel", identifier=identifier)
        return {"status": "ignored", "motivo": "telefone_invalido"}

    pausar = ETIQUETA_PAUSA in _etiquetas(payload)
    com_9, sem_9 = variacoes(canonico)

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            _SQL_DESLIGAR if pausar else _SQL_LIGAR, (com_9, sem_9)
        )
        afetadas = cur.rowcount

    logger.info(
        "chatwoot_etiqueta_aplicada",
        telefone=canonico,
        acao="pausou" if pausar else "religou",
        leads_afetados=afetadas,
    )
    return {
        "status": "ok",
        "acao": "pausou" if pausar else "religou",
        "leads_afetados": afetadas,
    }
