"""Webhook da uazapi (uazapiGO) — recebimento de mensagens.

Formato real entregue pela uazapi (auditado em runtime):

    {
      "EventType": "messages" | "messages_update" | "connection" | ...,
      "BaseUrl": "https://meucliente.uazapi.com",
      "instanceName": "...",
      "owner": "<owner id>",
      "token": "<instance token — usado para outbound>",
      "chatSource": "...",
      "chat": { ... ChatLead/Conversation ... },
      "message": { ... Message ... }
    }

O `token` top-level é o mesmo retornado por `POST /instance/init` e é o
que precisamos para autenticar chamadas outbound (header `token`) ao
responder. Persistimos em `message_queue.outbound_token` para o worker
usar na hora do envio.

Para o evento `messages`, `message` traz o schema Message — campos
relevantes: `text`, `messageType`, `fromMe`, `isGroup`, `messageid`,
`fileURL`, `chatid`, `sender`, `senderName`. O `chat` complementa com
`wa_chatid`/`phone`/`name`/`lead_*`.

A uazapi não assina o body do webhook. A defesa primária é o token da
instância vindo no payload — sem ele não conseguimos responder no canal
correto. Para hardening adicional, restrinja por IP/header no Traefik.

Diferenças vs webhook Twilio/Meta:
- POST recebe JSON com chaves `EventType`/`message`/`chat` (não
  `event`/`data`); aceitamos os dois formatos para compatibilidade.
- Mídia já vem com `fileURL` pública (não precisa download autenticado).
- Eventos não-`messages` são ignorados com 200.
- Mensagens `fromMe=true` filtradas para evitar loops (mesmo papel do
  filtro `excludeMessages: ["wasSentByApi"]` no painel).

Fluxo: uazapi -> POST /webhook/uazapi?agent=... -> Fila -> Worker
"""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from whatsapp_langchain.agents.loader import AgentNotFoundError, list_agents
from whatsapp_langchain.server.dependencies import check_rate_limit
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.queue import enqueue_or_buffer

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

# messageType vem em variações de caixa conforme a versão da uazapi
# (ex: `Conversation`, `conversation`, `ExtendedTextMessage`). Comparação
# é sempre feita em lowercase.
TEXT_MESSAGE_TYPES = {"conversation", "extendedtextmessage", "text"}

# messageType (lowercase) -> MIME prefix usado pelo preprocessor de mídia.
# media.py decide pelo prefixo "image/" ou "audio/"; o restante cai em
# "unsupported" e o usuário recebe a auto-resposta padrão.
MEDIA_TYPE_MAP: dict[str, str] = {
    "imagemessage": "image/jpeg",
    "stickermessage": "image/webp",
    "audiomessage": "audio/ogg",
    "pttmessage": "audio/ogg",
    "videomessage": "video/mp4",
    "documentmessage": "application/octet-stream",
}


def _normalize_phone_from_chatid(chatid: str) -> str:
    """Converte chatid uazapi (`<numero>@s.whatsapp.net`) em E.164 com '+'.

    Retorna string vazia se o formato é inesperado (ex: JID de grupo).
    """
    if not chatid:
        return ""
    # JID de grupo (termina em @g.us) ou outras variantes — não tratamos.
    if "@g.us" in chatid:
        return ""
    digits = chatid.split("@", 1)[0]
    digits = digits.lstrip("+").strip()
    if not digits.isdigit():
        return ""
    return f"+{digits}"


def _extract_instance_token(payload: dict[str, Any], data: dict[str, Any]) -> str:
    """Localiza o token da instância no payload do webhook.

    A uazapi inclui o token em campos diferentes conforme a versão/modo. Tenta
    em ordem (top-level → dentro de data) e cai no fallback estático
    `UAZAPI_INSTANCE_TOKEN` se nada bater.
    """
    candidates = (
        payload.get("token"),
        payload.get("instance_token"),
        payload.get("instanceToken"),
        data.get("token"),
        data.get("instance_token"),
        data.get("owner_token"),
    )
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    fallback = (settings.uazapi_instance_token or "").strip()
    return fallback


def _extract_message_payload(
    data: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """Extrai (body, media_url, media_type) de um Message da uazapi.

    Para texto, usa o campo `text`. Para mídia, usa `fileURL` + MIME inferido
    do `messageType`. Quando o tipo não é mapeado, retorna media_type=None
    para que a mensagem caia no caminho de "mídia incompleta" do preprocessor.

    Comparação de `messageType` é case-insensitive (a uazapi usa PascalCase
    nos webhooks reais — `Conversation`, `ExtendedTextMessage`, `ImageMessage`).
    """
    msg_type = (data.get("messageType") or "").strip().lower()
    text_field = data.get("text") or ""
    file_url = data.get("fileURL") or data.get("fileUrl") or None

    if msg_type in TEXT_MESSAGE_TYPES or not msg_type:
        return text_field, None, None

    media_type = MEDIA_TYPE_MAP.get(msg_type)

    if not file_url:
        # Mídia sem URL utilizável (ex: payload reduzido) — devolve só o
        # caption (se houver) e deixa o preprocessor decidir o auto-response.
        return text_field, None, media_type

    return text_field, file_url, media_type


@router.post("/webhook/uazapi")
async def webhook_uazapi_receive(
    request: Request,
    agent: str = Query(description="ID do agente para processar a mensagem"),
) -> dict[str, str | int]:
    """Recebe webhook da uazapi, normaliza e enfileira para processamento.

    Estrutura típica do payload (resumida):
        {
          "event": "messages",
          "instance": "<id>",
          "token": "<instance_token>",
          "data": {
            "messageid": "ABCD...",
            "chatid": "5511999999999@s.whatsapp.net",
            "fromMe": false,
            "isGroup": false,
            "messageType": "conversation",
            "text": "Olá",
            "fileURL": null
          }
        }

    Eventos que não são `messages` (status, connection, presence, etc.)
    são ignorados com 200, igual ao Meta. A uazapi exige resposta rápida
    para não disparar reentrega — qualquer outra coisa pode causar loops.
    """
    available_agents = list_agents()
    if agent not in available_agents:
        raise AgentNotFoundError(agent)

    try:
        payload = await request.json()
    except Exception:
        logger.warning("uazapi_invalid_json")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Aceita formato atual (`EventType`/`message`/`chat`) e o legado
    # (`event`/`data`) — varia por versão/fork da uazapi.
    event_name = (payload.get("EventType") or payload.get("event") or "").strip()
    instance_id = payload.get("instanceName") or payload.get("instance") or ""
    chat_obj = payload.get("chat") if isinstance(payload.get("chat"), dict) else {}

    raw_message = payload.get("message")
    raw_data = payload.get("data")

    # Coleta a lista de mensagens em ordem de prioridade (formato novo ->
    # legado), aceitando objeto único ou lista nos dois.
    candidate = raw_message if raw_message is not None else raw_data
    if isinstance(candidate, list):
        events: list[dict[str, Any]] = [d for d in candidate if isinstance(d, dict)]
    elif isinstance(candidate, dict):
        events = [candidate]
    else:
        events = []

    if event_name != "messages":
        logger.debug(
            "uazapi_event_ignored",
            uazapi_event=event_name or None,
            instance=instance_id or None,
        )
        return {"status": "ignored", "processed": 0}

    pool = await get_pool()
    processed = 0
    skipped = 0

    for message in events:
        # Filtra mensagens enviadas pela própria instância (loop guard).
        if message.get("fromMe") is True:
            skipped += 1
            continue

        # Por ora, não tratamos grupos no template.
        if message.get("isGroup") is True or (
            chat_obj.get("wa_isGroup") is True if chat_obj else False
        ):
            skipped += 1
            continue

        # chatid pode vir no message OU no chat (wa_chatid). chat.phone também
        # serve quando os IDs vêm sem sufixo "@s.whatsapp.net".
        chatid = (
            message.get("chatid")
            or message.get("sender")
            or (chat_obj.get("wa_chatid") if chat_obj else "")
            or (chat_obj.get("phone") if chat_obj else "")
            or ""
        )
        phone_number = _normalize_phone_from_chatid(chatid)
        if not phone_number:
            logger.warning(
                "uazapi_invalid_chatid",
                chatid=chatid,
                messageid=message.get("messageid"),
            )
            skipped += 1
            continue

        instance_token = _extract_instance_token(payload, message)
        if not instance_token:
            logger.warning(
                "uazapi_token_missing",
                phone=phone_number,
                messageid=message.get("messageid"),
                instance=instance_id or None,
            )
            # Sem token, o worker não consegue responder. Ainda enfileira
            # para que a falha apareça no log de retry e o operador veja.

        message_id = message.get("messageid") or message.get("id") or ""
        body, media_url, media_type = _extract_message_payload(message)

        try:
            await check_rate_limit(phone_number)
        except HTTPException:
            logger.warning(
                "uazapi_rate_limit_exceeded",
                phone=phone_number,
                messageid=message_id,
            )
            skipped += 1
            continue

        result = await enqueue_or_buffer(
            pool=pool,
            phone_number=phone_number,
            agent_id=agent,
            body=body,
            channel=MessagingChannel.UAZAPI,
            media_url=media_url,
            media_type=media_type,
            to_number=None,
            message_id=message_id,
            buffer_seconds=settings.message_buffer_seconds,
            outbound_token=instance_token or None,
        )

        logger.info(
            "webhook_uazapi_received",
            phone=phone_number,
            agent_id=agent,
            instance=instance_id or None,
            messageid=message_id or None,
            type=message.get("messageType"),
            queue_id=result.message_id,
            buffered=result.is_buffered,
            has_token=bool(instance_token),
        )
        processed += 1

    return {"status": "ok", "processed": processed, "skipped": skipped}
