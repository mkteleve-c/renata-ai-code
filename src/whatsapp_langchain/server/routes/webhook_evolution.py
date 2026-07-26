"""Webhook da Evolution API (integração WHATSAPP-BUSINESS).

O payload chega no formato Baileys mesmo quando a integração é a Cloud API
oficial — a Evolution normaliza os dois casos:

    {
      "event": "messages.upsert",
      "instance": "instancia-apioficial",
      "data": {
        "key": {"remoteJid": "...@s.whatsapp.net", "remoteJidAlt": "...",
                "fromMe": false, "id": "..."},
        "pushName": "Fulano",
        "messageType": "conversation",
        "message": {"conversation": "texto"}
      }
    }

Diferente dos outros canais do harness, aqui o gate de ingestão roda ANTES
de enfileirar: `fromMe` é eco de mensagem enviada por humano e não pode
virar item de fila, e filtrar antes evita ocupar a fila com descarte.

O nome do evento varia entre versões da Evolution (`messages.upsert` ou
`messages`) — ambos são aceitos.

Mídia recebida pela Evolution só é baixável via getBase64FromMediaMessage,
que exige a key completa (remoteJid + fromMe + id), não só o id. Por isso
`data.key` inteiro é gravado em `provider_message_key` quando a mensagem
tem mídia — o worker (Task 9) usa esse valor para baixar o conteúdo.
"""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from whatsapp_langchain.agents.loader import AgentNotFoundError, list_agents
from whatsapp_langchain.server.dependencies import check_rate_limit
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.phone import to_e164
from whatsapp_langchain.shared.queue import enqueue_or_buffer

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

EVENTOS_DE_MENSAGEM = {"messages.upsert", "messages"}

MEDIA_TYPE_MAP: dict[str, str] = {
    "imagemessage": "image/jpeg",
    "stickermessage": "image/webp",
    "audiomessage": "audio/ogg",
    "pttmessage": "audio/ogg",
    "videomessage": "video/mp4",
    "documentmessage": "application/octet-stream",
}


def _extrair_conteudo(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Devolve (texto, media_url, media_type) do payload da Evolution."""
    msg = data.get("message") or {}
    tipo = (data.get("messageType") or "").strip().lower()

    texto = msg.get("conversation") or (
        (msg.get("extendedTextMessage") or {}).get("text") or ""
    )

    if tipo in ("conversation", "extendedtextmessage", "text") or not tipo:
        return texto, None, None

    media_type = MEDIA_TYPE_MAP.get(tipo)
    url = None
    for campo in ("audioMessage", "imageMessage", "videoMessage", "documentMessage"):
        if isinstance(msg.get(campo), dict):
            url = msg[campo].get("url")
            texto = texto or msg[campo].get("caption") or ""
            break

    return texto, url, media_type


@router.post("/webhook/evolution")
async def webhook_evolution_receive(
    request: Request,
    agent: str = Query(description="ID do agente para processar a mensagem"),
) -> dict[str, Any]:
    if agent not in list_agents():
        raise AgentNotFoundError(agent)

    try:
        payload = await request.json()
    except Exception:
        logger.warning("evolution_json_invalido")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    evento = (payload.get("event") or "").strip().lower()
    if evento not in EVENTOS_DE_MENSAGEM:
        logger.debug("evolution_evento_ignorado", evento=evento or None)
        return {"status": "ignorado", "motivo": "evento_nao_tratado"}

    data = payload.get("data") or {}
    key = data.get("key") or {}

    pool = await get_pool()
    resultado = await aplicar_gate(pool, key, push_name=data.get("pushName"))

    if not resultado.aceito:
        logger.info(
            "evolution_descartado",
            motivo=resultado.motivo,
            phone=resultado.canonico,
        )
        return {"status": "ignorado", "motivo": resultado.motivo}

    assert resultado.canonico is not None
    phone_e164 = to_e164(resultado.canonico)
    texto, media_url, media_type = _extrair_conteudo(data)
    provider_message_key = key if media_url else None

    try:
        await check_rate_limit(phone_e164)
    except HTTPException:
        logger.warning("evolution_rate_limit", phone=phone_e164)
        return {"status": "ignorado", "motivo": "rate_limit"}

    enfileirado = await enqueue_or_buffer(
        pool=pool,
        phone_number=phone_e164,
        agent_id=agent,
        body=texto,
        channel=MessagingChannel.EVOLUTION,
        media_url=media_url,
        media_type=media_type,
        message_id=key.get("id"),
        buffer_seconds=settings.message_buffer_seconds,
        provider_message_key=provider_message_key,
    )

    logger.info(
        "webhook_evolution_recebido",
        phone=phone_e164,
        agent_id=agent,
        instance=payload.get("instance"),
        queue_id=enfileirado.message_id,
        buffered=enfileirado.is_buffered,
        phase=resultado.lead.get("phase") if resultado.lead else None,
    )

    return {"status": "ok", "queue_id": enfileirado.message_id}
