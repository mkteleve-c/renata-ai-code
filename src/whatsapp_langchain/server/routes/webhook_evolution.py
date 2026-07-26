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

`data` também pode vir como array (formato nativo do Baileys, quando a
Evolution repassa o upsert cru) — cada item é uma mensagem e todos são
processados, mesmo caminho que `webhook_uazapi` já usa.

Diferente dos outros canais do harness, aqui o gate de ingestão roda ANTES
de enfileirar: `fromMe` é eco de mensagem enviada por humano e não pode
virar item de fila, e filtrar antes evita ocupar a fila com descarte.

O nome do evento varia entre versões da Evolution (`messages.upsert`,
`messages`, `MESSAGES_UPSERT`, `messages-upsert`) — separador e caixa são
normalizados antes da comparação.

Só vira linha na fila o que o agente consegue processar: evento sem texto
nem mídia (reação, enquete, mensagem apagada, protocolo) é descartado com
200, antes do gate. Sem esse corte o agente seria invocado com conteúdo
vazio e um emoji criaria lead novo em `leads_crm`.

Mídia sem `url` NÃO é descarte: a URL do payload aponta para conteúdo
cifrado e `download_media` nem a lê neste canal — quem baixa é a
`provider_message_key`. Basta `media_type` para a mensagem entrar na fila
como mídia.

Reentrega é esperada: a Evolution repete o POST em timeout ou resposta
>= 400. `enqueue_or_buffer` deduplica por (canal, message_id) e a rota
responde 200 com motivo `duplicata` — responder erro faria a Evolution
reentregar de novo, em loop.

Mídia recebida pela Evolution só é baixável via getBase64FromMediaMessage,
que exige a key completa (remoteJid + fromMe + id), não só o id. Por isso
`data.key` inteiro é gravado em `provider_message_key` sempre que a
mensagem tem mídia — a URL do payload aponta para conteúdo cifrado e é
ignorada no download, então a key é a única via.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from whatsapp_langchain.agents.loader import AgentNotFoundError, list_agents
from whatsapp_langchain.server.dependencies import (
    check_rate_limit,
    verify_evolution_webhook_secret,
)
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.phone import resolver_telefone, to_e164
from whatsapp_langchain.shared.queue import enqueue_or_buffer

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

EVENTOS_DE_MENSAGEM = {"messages.upsert", "messages"}

# messageType (lowercase) -> MIME usado pelo preprocessor de mídia.
MEDIA_TYPE_MAP: dict[str, str] = {
    "imagemessage": "image/jpeg",
    "stickermessage": "image/webp",
    "audiomessage": "audio/ogg",
    "pttmessage": "audio/ogg",
    "videomessage": "video/mp4",
    "documentmessage": "application/octet-stream",
}

# Campo dentro de `message` -> MIME de fallback. É o conteúdo que manda, não
# o `messageType`: em mensagem embrulhada (viewOnce, efêmera, documento com
# legenda) o tipo declarado é o do envelope, não o da mídia de dentro.
CAMPOS_DE_MIDIA: dict[str, str] = {
    "imageMessage": "image/jpeg",
    "stickerMessage": "image/webp",
    "audioMessage": "audio/ogg",
    "pttMessage": "audio/ogg",
    "videoMessage": "video/mp4",
    "documentMessage": "application/octet-stream",
}

# Envelopes que aninham a mensagem real em `message.<envelope>.message`.
ENVELOPES = (
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "documentWithCaptionMessage",
    "editedMessage",
)


def _normalizar_evento(bruto: Any) -> str:
    """Reduz o nome do evento a uma forma só: `messages.upsert`.

    A Evolution v2 emite o mesmo evento como `messages.upsert`,
    `MESSAGES_UPSERT` ou `messages-upsert` conforme a versão e o modo de
    entrega (webhook global, rabbitmq, websocket).
    """
    if not isinstance(bruto, str):
        return ""
    return bruto.strip().lower().replace("-", ".").replace("_", ".")


def _desembrulhar(msg: dict[str, Any], profundidade: int = 3) -> dict[str, Any]:
    """Desce nos envelopes até chegar na mensagem que carrega o conteúdo."""
    for _ in range(profundidade):
        for envelope in ENVELOPES:
            interno = msg.get(envelope)
            if isinstance(interno, dict):
                aninhada = interno.get("message")
                msg = aninhada if isinstance(aninhada, dict) else interno
                break
        else:
            return msg
    return msg


def _extrair_conteudo(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Devolve (texto, media_url, media_type) do payload da Evolution.

    Tudo que não é texto nem mídia conhecida sai como `("", None, None)` —
    a rota trata isso como evento a ignorar. Reação, atualização de enquete
    e `protocolMessage` de mensagem apagada caem aqui.
    """
    bruto = data.get("message")
    msg = _desembrulhar(bruto) if isinstance(bruto, dict) else {}
    tipo = str(data.get("messageType") or "").strip().lower()

    texto = msg.get("conversation")
    if not isinstance(texto, str) or not texto:
        estendida = msg.get("extendedTextMessage")
        candidato = estendida.get("text") if isinstance(estendida, dict) else None
        texto = candidato if isinstance(candidato, str) else ""

    for campo, mime_padrao in CAMPOS_DE_MIDIA.items():
        conteudo = msg.get(campo)
        if not isinstance(conteudo, dict):
            continue

        url = conteudo.get("url")
        legenda = conteudo.get("caption")
        if not texto and isinstance(legenda, str):
            texto = legenda
        return (
            texto,
            url if isinstance(url, str) and url else None,
            MEDIA_TYPE_MAP.get(tipo) or mime_padrao,
        )

    return texto, None, None


def _mensagens_do_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normaliza `data` (objeto único ou array do Baileys) numa lista."""
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


async def _processar_mensagem(
    data: dict[str, Any],
    agent: str,
    instance: Any,
) -> dict[str, Any]:
    """Roda gate, rate limit e enfileiramento para uma mensagem do payload.

    Ordem: extração e guard de conteúdo → rate limit → gate → fila. Tudo que
    não vai virar linha na fila é cortado ANTES do gate, que escreve em
    leads_crm. `fromMe` é a única exceção — ele nunca é aceito pelo gate, mas
    precisa chegar lá para desligar o agente (handover do atendente).
    """
    bruto = data.get("key")
    key: dict[str, Any] = bruto if isinstance(bruto, dict) else {}
    message_id = key.get("id") if isinstance(key.get("id"), str) else None
    eh_from_me = key.get("fromMe") is True

    texto, media_url, media_type = _extrair_conteudo(data)

    # Nem texto nem mídia: reação, enquete, mensagem apagada, protocolo.
    # Enfileirar isso invocaria o agente com conteúdo vazio. O corte é por
    # `media_type` e não por `media_url` porque na Evolution a URL aponta
    # para conteúdo cifrado e o download é feito pela key — mídia sem URL é
    # normal aqui, e descartá-la perderia áudio de lead em silêncio.
    #
    # Antes do gate: uma reação de lead que passasse por ele renovaria
    # last_interaction_at e zeraria followup_count, criando ou "reengajando"
    # um lead por um emoji que o agente nunca vê.
    if not texto and not media_type and not eh_from_me:
        logger.info(
            "evolution_conteudo_nao_suportado",
            phone=key.get("remoteJid"),
            message_type=data.get("messageType"),
            message_key_id=message_id,
            instance=instance,
        )
        return {"status": "ignorado", "motivo": "conteudo_nao_suportado"}

    # Rate limit antes do gate: o gate escreve (renova last_interaction_at,
    # zera followup_count) e mensagem barrada aqui nunca chega ao agente —
    # deixar o gate rodar antes contaria como engajamento o que foi jogado
    # fora. Sem telefone resolvível não há chave de rate limit; o gate
    # devolve `telefone_invalido` logo abaixo.
    #
    # `fromMe` fica fora do limite de propósito: é eco de humano assumindo a
    # conversa, nunca vira linha na fila (não há o que limitar) e o gate
    # precisa vê-lo para desligar o agente. Barrar aqui deixaria o bot
    # respondendo por cima do atendente.
    canonico_previo = resolver_telefone(key)
    if canonico_previo and not eh_from_me:
        try:
            await check_rate_limit(to_e164(canonico_previo))
        except HTTPException:
            # 200 de propósito: 429 faria a Evolution reentregar em loop.
            logger.warning(
                "evolution_rate_limit",
                phone=to_e164(canonico_previo),
                message_key_id=message_id,
                instance=instance,
            )
            return {"status": "ignorado", "motivo": "rate_limit"}

    pool = await get_pool()
    push_name = data.get("pushName")
    resultado = await aplicar_gate(
        pool, key, push_name=push_name if isinstance(push_name, str) else None
    )

    if not resultado.aceito:
        logger.info(
            "evolution_descartado",
            motivo=resultado.motivo,
            phone=resultado.canonico,
            message_key_id=message_id,
        )
        return {"status": "ignorado", "motivo": resultado.motivo}

    if resultado.canonico is None:
        # Não acontece — o gate só aceita depois de resolver o telefone.
        # Explícito em vez de assert porque assert some com -O.
        raise RuntimeError("gate aceitou mensagem sem telefone canônico")

    phone_e164 = to_e164(resultado.canonico)

    enfileirado = await enqueue_or_buffer(
        pool=pool,
        phone_number=phone_e164,
        agent_id=agent,
        body=texto,
        channel=MessagingChannel.EVOLUTION,
        media_url=media_url,
        media_type=media_type,
        message_id=message_id,
        buffer_seconds=settings.message_buffer_seconds,
        # A key é a única via de download na Evolution (`download_media`
        # ignora a URL nesse canal), então grava sempre que há mídia — mesmo
        # sem URL o media_type já identifica a mensagem como mídia.
        provider_message_key=key if media_type else None,
    )

    if enfileirado.is_duplicate:
        # 200: a Evolution reentrega tudo que responder >= 400.
        logger.info(
            "evolution_duplicata",
            phone=phone_e164,
            message_key_id=message_id,
            queue_id=enfileirado.message_id,
            instance=instance,
        )
        return {
            "status": "ignorado",
            "motivo": "duplicata",
            "queue_id": enfileirado.message_id,
        }

    logger.info(
        "webhook_evolution_recebido",
        phone=phone_e164,
        agent_id=agent,
        instance=instance,
        message_key_id=message_id,
        queue_id=enfileirado.message_id,
        buffered=enfileirado.is_buffered,
        phase=resultado.lead.get("phase") if resultado.lead else None,
    )

    return {"status": "ok", "queue_id": enfileirado.message_id}


@router.post("/webhook/evolution")
async def webhook_evolution_receive(
    request: Request,
    agent: str = Query(description="ID do agente para processar a mensagem"),
    _secret: None = Depends(verify_evolution_webhook_secret),
) -> dict[str, Any]:
    if agent not in list_agents():
        raise AgentNotFoundError(agent)

    try:
        payload = await request.json()
    except Exception:
        logger.warning("evolution_json_invalido")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(payload, dict):
        # Body válido como JSON mas fora do formato (lista, string, número).
        # 200 para não entrar em loop de reentrega do mesmo payload quebrado.
        logger.warning("evolution_payload_invalido", tipo=type(payload).__name__)
        return {"status": "ignorado", "motivo": "payload_invalido"}

    evento = _normalizar_evento(payload.get("event") or payload.get("EventType"))
    if evento not in EVENTOS_DE_MENSAGEM:
        # INFO e não DEBUG: com LOG_LEVEL=info (default) um nome de evento
        # novo sumiria sem deixar rastro.
        logger.info("evolution_evento_ignorado", evento=evento or None)
        return {"status": "ignorado", "motivo": "evento_nao_tratado"}

    instance = payload.get("instance")
    mensagens = _mensagens_do_payload(payload)
    if not mensagens:
        logger.warning("evolution_sem_mensagem", instance=instance)
        return {"status": "ignorado", "motivo": "payload_sem_mensagem"}

    resultados = [
        await _processar_mensagem(data, agent, instance) for data in mensagens
    ]

    # Caso normal (a Evolution manda uma mensagem por POST): resposta direta,
    # com `queue_id` no topo. Array só aparece quando o upsert vem cru.
    if len(resultados) == 1:
        return resultados[0]

    return {
        "status": "ok",
        "processados": sum(1 for r in resultados if r["status"] == "ok"),
        "resultados": resultados,
    }
