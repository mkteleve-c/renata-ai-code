"""Processador de mensagens — orquestra agente, typing e envio outbound.

Responsável por:
1. Pré-processar entrada (mídia -> texto)
2. Enviar typing indicator (best-effort, no-op em alguns canais)
3. Carregar o agente via loader (com checkpointer PostgreSQL)
4. Executar o agente
5. Enviar resposta ao usuário via cliente outbound do canal de origem
   (`message.channel`)
6. Salvar no banco (mark_done somente após envio confirmado)

Decisões arquiteturais:
- O worker mantém um dict `outbounds: {MessagingChannel: client}` com os
  canais habilitados; o processor seleciona o cliente pelo canal da mensagem.
- Em production, o envio outbound usa o canal real.
- Em desenvolvimento, o worker pode operar em modo mock para validar
  a via assincrona sem consumir cota externa.
- mark_done ocorre somente após envio outbound bem-sucedido
  (real ou simulado, dependendo do modo do worker).
- Falha de envio entra no fluxo de retry (mark_failed).
- Mensagem cujo `channel` não tem cliente disponível é marcada como
  failed com erro claro (operador deve habilitar o canal correspondente).

Uso:
    from whatsapp_langchain.worker.processor import process_message

    await process_message(
        message, pool,
        checkpointer=checkpointer,
        store=store,
        outbounds={MessagingChannel.TWILIO: TwilioClient(...), ...},
    )
"""

import structlog
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.agents.loader import load_graph
from whatsapp_langchain.shared.models import MessageQueue, MessagingChannel
from whatsapp_langchain.shared.queue import (
    mark_done,
    mark_failed,
    upsert_conversation,
)
from whatsapp_langchain.worker.media import (
    AUTO_RESPONSE_MEDIA_FAILURE,
    preprocess_incoming_message,
)
from whatsapp_langchain.worker.meta_client import MetaClient
from whatsapp_langchain.worker.twilio_client import TwilioClient
from whatsapp_langchain.worker.uazapi_client import UazapiClient

logger = structlog.get_logger()

OutboundClient = TwilioClient | MetaClient | UazapiClient


def _normalize_outbounds(
    outbounds: dict[MessagingChannel, OutboundClient] | None,
    outbound: OutboundClient | None,
    twilio: OutboundClient | None,
) -> dict[MessagingChannel, OutboundClient]:
    """Resolve o mapa de clientes outbound, aceitando assinaturas legadas.

    Prioridade: outbounds (novo) > twilio (legacy explícito) > outbound (legacy).
    Os kwargs legados são mantidos para não quebrar testes/scripts externos
    que ainda passam um cliente único; no path novo o worker sempre passa o dict.
    """
    if outbounds:
        return outbounds
    # Kwarg `twilio=` é explicitamente Twilio (semântica histórica).
    if twilio is not None:
        return {MessagingChannel.TWILIO: twilio}
    if outbound is not None:
        if isinstance(outbound, MetaClient):
            return {MessagingChannel.META: outbound}
        if isinstance(outbound, UazapiClient):
            return {MessagingChannel.UAZAPI: outbound}
        # TwilioClient ou mock genérico — assume Twilio (default histórico).
        return {MessagingChannel.TWILIO: outbound}
    raise ValueError(
        "process_message exige 'outbounds' (dict por canal) ou um cliente "
        "outbound legado em 'outbound'/'twilio'."
    )


def _select_client(
    outbounds: dict[MessagingChannel, OutboundClient],
    message: MessageQueue,
) -> OutboundClient:
    """Seleciona o cliente outbound pelo canal da mensagem.

    Levanta ValueError se o canal não está habilitado neste worker. O caller
    deve capturar e direcionar para mark_failed com erro claro.
    """
    client = outbounds.get(message.channel)
    if client is None:
        available = sorted(ch.value for ch in outbounds)
        raise ValueError(
            f"Canal '{message.channel.value}' não está habilitado neste worker. "
            f"Canais disponíveis: {available or 'nenhum'}. Verifique as "
            f"credenciais correspondentes no .env."
        )
    return client


async def _send_message(
    outbound: OutboundClient,
    to: str,
    body: str,
    message: MessageQueue,
) -> str:
    """Wrapper que injeta o token outbound dinâmico para UazapiClient.

    Twilio e Meta têm credenciais estáticas no construtor — chamada padrão.
    Uazapi recebe o token da instância via webhook e armazenado em
    message.outbound_token; passamos como kwarg para o cliente usar.
    """
    if isinstance(outbound, UazapiClient):
        return await outbound.send_message(to, body, token=message.outbound_token)
    return await outbound.send_message(to, body)


async def _send_typing(
    outbound: OutboundClient,
    to: str,
    message: MessageQueue,
) -> bool:
    """Wrapper de typing indicator com token dinâmico para UazapiClient."""
    if isinstance(outbound, UazapiClient):
        return await outbound.send_typing(
            to, message.message_id, token=message.outbound_token
        )
    return await outbound.send_typing(to, message.message_id)


async def process_message(
    message: MessageQueue,
    pool: AsyncConnectionPool,
    *,
    checkpointer: BaseCheckpointSaver,
    store: BaseStore | None = None,
    outbounds: dict[MessagingChannel, OutboundClient] | None = None,
    outbound: OutboundClient | None = None,
    twilio: OutboundClient | None = None,
) -> None:
    """Processa uma mensagem da fila com o agente apropriado.

    Faz download de mídia se presente, envia typing, carrega o grafo
    do agente com checkpointer PostgreSQL, executa, envia a resposta
    via cliente outbound do canal de origem (`message.channel`) e salva
    no banco.

    Nenhum mark_done ocorre sem envio outbound confirmado.

    Args:
        message: Mensagem a processar (já reservada via claim).
        pool: Pool de conexões do psycopg.
        checkpointer: Checkpointer LangGraph já inicializado no boot.
        store: Store LangGraph compartilhado (None se memória desabilitada).
        outbounds: Dict {MessagingChannel: client} com canais habilitados.
        outbound: Cliente outbound único (legacy — preferir `outbounds`).
        twilio: Alias retrocompatível para `outbound` (deprecated).
    """
    try:
        client_map = _normalize_outbounds(outbounds, outbound, twilio)
    except ValueError:
        raise

    logger.info(
        "processing_message",
        message_id=message.id,
        phone=message.phone_number,
        agent_id=message.agent_id,
        channel=message.channel.value,
        attempt=message.attempts,
    )

    # Selecionar cliente do canal correto. Falha aqui = canal não habilitado
    # no worker; vai para mark_failed antes de tocar o agente.
    try:
        client = _select_client(client_map, message)
    except ValueError as ch_err:
        logger.error(
            "channel_not_enabled",
            message_id=message.id,
            phone=message.phone_number,
            channel=message.channel.value,
            error=str(ch_err),
        )
        await mark_failed(pool, message.id, str(ch_err))
        return

    try:
        # 1. Pré-processar entrada (mídia -> texto) antes do agente
        pre = await preprocess_incoming_message(
            body=message.incoming_message,
            media_url=message.media_url,
            media_type=message.media_type,
        )

        # Se mídia está desabilitada ou falhou, não chama o agente
        if not pre.should_invoke_agent:
            auto_response = pre.auto_response or AUTO_RESPONSE_MEDIA_FAILURE

            # Enviar auto-response no canal outbound antes de marcar como done
            await _send_message(client, message.phone_number, auto_response, message)

            await mark_done(
                pool,
                message.id,
                auto_response,
                normalized_input=None,
                media_processing_status=pre.media_processing_status,
                media_processing_error=pre.media_processing_error,
            )
            await upsert_conversation(
                pool,
                phone_number=message.phone_number,
                agent_id=message.agent_id,
                last_message=auto_response,
            )
            logger.info(
                "message_auto_responded",
                message_id=message.id,
                phone=message.phone_number,
                agent_id=message.agent_id,
                channel=message.channel.value,
                media_status=pre.media_processing_status,
            )
            return

        # 2. Typing indicator (best-effort, falha não interrompe processamento)
        try:
            await _send_typing(client, message.phone_number, message)
        except Exception as typing_err:
            logger.warning(
                "typing_failed",
                message_id=message.id,
                phone=message.phone_number,
                channel=message.channel.value,
                error=str(typing_err),
            )

        # 3. Carregar agente com checkpointer + store (se memória habilitada)
        normalized_text = pre.normalized_text or message.incoming_message
        human_message = HumanMessage(content=normalized_text)

        invoke_config = {
            "configurable": {
                "thread_id": message.thread_id,
                "user_id": message.phone_number,
            }
        }

        graph = load_graph(
            message.agent_id,
            checkpointer=checkpointer,
            store=store,
        )
        result = await graph.ainvoke(
            {"messages": [human_message]},
            config=invoke_config,
        )

        # 4. Extrair resposta
        response_text = result["messages"][-1].content

        # 5. Enviar resposta outbound antes de mark_done
        await _send_message(client, message.phone_number, response_text, message)

        # 6. mark_done somente após envio confirmado
        await mark_done(
            pool,
            message.id,
            response_text,
            normalized_input=pre.normalized_text,
            media_processing_status=pre.media_processing_status,
            media_processing_error=pre.media_processing_error,
        )
        await upsert_conversation(
            pool,
            phone_number=message.phone_number,
            agent_id=message.agent_id,
            last_message=response_text,
        )

        logger.info(
            "message_processed",
            message_id=message.id,
            phone=message.phone_number,
            agent_id=message.agent_id,
            channel=message.channel.value,
            response_length=len(response_text),
        )

    except Exception as e:
        logger.error(
            "message_processing_error",
            message_id=message.id,
            phone=message.phone_number,
            agent_id=message.agent_id,
            channel=message.channel.value,
            error=str(e),
        )
        await mark_failed(pool, message.id, str(e))
