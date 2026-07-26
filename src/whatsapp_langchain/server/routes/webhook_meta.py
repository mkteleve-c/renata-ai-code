"""Webhook do Meta WhatsApp Cloud API — handshake + recebimento de mensagens.

Endpoints:
- GET  /webhook/meta — handshake de verificação (hub.verify_token + hub.challenge)
- POST /webhook/meta — payload JSON com mensagens, validado via X-Hub-Signature-256

Diferenças vs webhook Twilio:
- POST recebe JSON (não form-encoded)
- Assinatura usa HMAC-SHA256 (não SHA1) com APP_SECRET
- Identidade do remetente vem em entry[].changes[].value.messages[].from (sem '+')
- Mídia vem como ID (não URL) — exige download separado via Graph API (TODO)
- Status updates (delivered, read) chegam aqui também e são ignorados

Fluxo: Meta -> POST /webhook/meta -> Fila (PostgreSQL) -> Worker
"""

import hashlib
import hmac
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response

from whatsapp_langchain.agents.loader import AgentNotFoundError, list_agents
from whatsapp_langchain.server.dependencies import check_rate_limit
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.queue import enqueue_or_buffer

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

SUPPORTED_MEDIA_TYPES = {"image", "audio", "video", "document", "sticker"}


@router.get("/webhook/meta")
async def webhook_meta_verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    """Handshake de verificação do webhook Meta.

    O Meta envia um GET com hub.mode=subscribe + hub.verify_token. Se o token
    bater com META_VERIFY_TOKEN configurado, devolvemos hub.challenge em texto
    puro com 200. Qualquer outra coisa retorna 403.
    """
    if not settings.meta_verify_token:
        logger.error("meta_verify_token_not_configured")
        raise HTTPException(
            status_code=500,
            detail="Meta verify token not configured",
        )

    if hub_mode != "subscribe":
        logger.warning("meta_handshake_invalid_mode", mode=hub_mode)
        raise HTTPException(status_code=403, detail="Invalid hub.mode")

    if not hmac.compare_digest(hub_verify_token, settings.meta_verify_token):
        logger.warning("meta_handshake_token_mismatch")
        raise HTTPException(status_code=403, detail="Verify token mismatch")

    logger.info("meta_handshake_ok")
    return Response(content=hub_challenge, media_type="text/plain")


def _validate_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Valida X-Hub-Signature-256: HMAC-SHA256(app_secret, raw_body).

    Raises:
        HTTPException 403: assinatura ausente ou inválida.
        HTTPException 500: APP_SECRET não configurado mas validação habilitada.
    """
    if not settings.meta_validate_signature:
        return

    if not signature_header:
        logger.warning("meta_signature_missing")
        raise HTTPException(status_code=403, detail="Missing X-Hub-Signature-256")

    if not settings.meta_app_secret:
        logger.error("meta_app_secret_not_configured")
        raise HTTPException(
            status_code=500,
            detail="Meta app secret not configured",
        )

    expected_prefix = "sha256="
    if not signature_header.startswith(expected_prefix):
        logger.warning("meta_signature_invalid_format")
        raise HTTPException(status_code=403, detail="Invalid signature format")

    received_hex = signature_header[len(expected_prefix) :]
    expected_hex = hmac.new(
        settings.meta_app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(received_hex, expected_hex):
        logger.warning("meta_signature_invalid")
        raise HTTPException(status_code=403, detail="Invalid signature")


def _extract_message_payload(msg: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Extrai (body, media_url, media_type) de uma message do payload Meta.

    Suporta texto e mídia (image/audio/video/document/sticker). Para mídia,
    a Cloud API retorna apenas o ID — o download via Graph API ainda não
    está implementado, então salvamos só a caption (se houver) e marcamos
    com placeholder no body.
    """
    msg_type = msg.get("type", "")

    if msg_type == "text":
        return msg.get("text", {}).get("body", ""), None, None

    if msg_type in SUPPORTED_MEDIA_TYPES:
        media = msg.get(msg_type, {}) or {}
        caption = media.get("caption", "") or ""
        body = (
            caption
            or f"[mensagem com {msg_type} — download Meta media não implementado]"
        )
        return body, None, None

    if msg_type in {"location", "contacts", "interactive", "button", "reaction"}:
        return f"[mensagem do tipo {msg_type} — não suportada]", None, None

    return f"[mensagem do tipo {msg_type or 'desconhecido'}]", None, None


@router.post("/webhook/meta")
async def webhook_meta_receive(
    request: Request,
    agent: str = Query(description="ID do agente para processar a mensagem"),
) -> dict[str, str | int]:
    """Recebe payload do Meta, valida assinatura, normaliza e enfileira.

    Estrutura típica do payload (resumida):
        {
          "object": "whatsapp_business_account",
          "entry": [{
            "changes": [{
              "value": {
                "metadata": {"phone_number_id": "...", "display_phone_number": "..."},
                "messages": [{
                  "from": "5511999999999",
                  "id": "wamid.XXX",
                  "type": "text",
                  "text": {"body": "..."}
                }]
              }
            }]
          }]
        }

    Status updates (delivered, read) chegam aqui também — são ignorados.
    """
    # 1. Validar agent
    available_agents = list_agents()
    if agent not in available_agents:
        raise AgentNotFoundError(agent)

    # 2. Validar assinatura ANTES de parsear (precisa do raw body)
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    _validate_signature(raw_body, signature)

    # 3. Parse JSON
    try:
        payload = await request.json()
    except Exception:
        logger.warning("meta_invalid_json")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if payload.get("object") != "whatsapp_business_account":
        logger.warning("meta_unexpected_object", object=payload.get("object"))
        return {"status": "ignored", "processed": 0}

    # 4. Extrair mensagens (ignora status updates)
    pool = await get_pool()
    processed = 0
    skipped = 0

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {}) or {}

            # Status updates (delivered, read, sent) — ignora
            if "statuses" in value and not value.get("messages"):
                continue

            metadata = value.get("metadata", {}) or {}
            display_phone = metadata.get("display_phone_number", "") or ""

            for msg in value.get("messages", []) or []:
                from_raw = msg.get("from", "") or ""
                if not from_raw:
                    logger.warning(
                        "meta_message_missing_from", message_id=msg.get("id")
                    )
                    skipped += 1
                    continue

                # Meta envia 'from' como E.164 sem '+' (ex: "5511999999999")
                phone_number = from_raw if from_raw.startswith("+") else f"+{from_raw}"
                wamid = msg.get("id", "") or ""

                body, media_url, media_type = _extract_message_payload(msg)

                # Rate limit por telefone
                try:
                    await check_rate_limit(phone_number)
                except HTTPException:
                    logger.warning(
                        "meta_rate_limit_exceeded",
                        phone=phone_number,
                        wamid=wamid,
                    )
                    skipped += 1
                    continue

                result = await enqueue_or_buffer(
                    pool=pool,
                    phone_number=phone_number,
                    agent_id=agent,
                    body=body,
                    channel=MessagingChannel.META,
                    media_url=media_url,
                    media_type=media_type,
                    to_number=display_phone,
                    message_id=wamid,
                    buffer_seconds=settings.message_buffer_seconds,
                )

                logger.info(
                    "webhook_meta_received",
                    phone=phone_number,
                    agent_id=agent,
                    wamid=wamid,
                    type=msg.get("type"),
                    message_id=result.message_id,
                    buffered=result.is_buffered,
                )
                processed += 1

    # Meta exige 200 rapidamente — qualquer outra coisa dispara retry
    return {"status": "ok", "processed": processed, "skipped": skipped}
