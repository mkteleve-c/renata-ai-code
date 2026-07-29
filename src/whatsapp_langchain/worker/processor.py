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

import asyncio

import structlog
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.agents.loader import load_graph
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.models import MessageQueue, MessagingChannel
from whatsapp_langchain.shared.queue import (
    mark_done,
    mark_failed,
    registrar_balao_enviado,
    upsert_conversation,
)
from whatsapp_langchain.worker.evolution_client import EvolutionClient
from whatsapp_langchain.worker.media import (
    AUTO_RESPONSE_MEDIA_FAILURE,
    preprocess_incoming_message,
)
from whatsapp_langchain.worker.meta_client import MetaClient
from whatsapp_langchain.worker.twilio_client import TwilioClient
from whatsapp_langchain.worker.uazapi_client import UazapiClient

logger = structlog.get_logger()

OutboundClient = TwilioClient | MetaClient | UazapiClient | EvolutionClient

# Único agente do catálogo que devolve JSON estruturado (`{"messages": [...]}`)
# no texto final — os demais (illumi_assistant, rhawk_assistant) respondem
# texto puro e não podem passar por extrair_baloes. `extrair_baloes` é
# importado sob demanda (lazy) dentro do branch que usa BALOES_AGENT_ID, não
# aqui no topo: `load_graph(message.agent_id, ...)` já importa dinamicamente
# o pacote `catalog.elevec_sdr` quando (e só quando) agent_id == "elevec_sdr"
# — importar aqui em cima faria o worker carregar o catálogo da Renata
# (agent.py, e nas próximas tasks os clientes de Calendar/CRM) no boot,
# mesmo em deploys que nunca usam esse agente, e quebraria o modelo de
# template do repositório (um fork que apaga catalog/elevec_sdr/ passaria a
# tomar ModuleNotFoundError no boot do worker, não só ao rotear pra ela).
BALOES_AGENT_ID = "elevec_sdr"


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
        if isinstance(outbound, EvolutionClient):
            return {MessagingChannel.EVOLUTION: outbound}
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
) -> str | None:
    """Wrapper que injeta o token outbound dinâmico para UazapiClient.

    Twilio e Meta têm credenciais estáticas no construtor — chamada padrão.
    Uazapi recebe o token da instância via webhook e armazenado em
    message.outbound_token; passamos como kwarg para o cliente usar.
    A Evolution autentica pela apikey e devolve None em modo mock — daí o
    retorno opcional.
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


async def _send_baloes(
    pool: AsyncConnectionPool,
    outbound: OutboundClient,
    message: MessageQueue,
    baloes: list[str],
) -> None:
    """Envia os balões em sequência, espaçados por `settings.balao_delay_ms`.

    Cada balão é um `_send_message` independente. Se um deles falhar no
    meio da sequência, os anteriores já foram entregues e **não são
    reenviados** — nem aqui, nem no retry.

    `message_queue.baloes_enviados` (migração 017) guarda o progresso, e o
    loop abaixo pula os índices já entregues. Sem isso, o retry reinvocava
    o agente do zero e recomeçava o envio no índice 0: o lead relia os
    balões que já tinha recebido, e com `max_attempts = 3` o mesmo balão
    podia chegar três vezes. Uma resposta em balões torna esse modo de
    falha visível de um jeito que a resposta única não tinha — ali, falha
    significava que o lead não recebeu nada e o retry era simplesmente
    correto.

    O contador é gravado DEPOIS de cada envio confirmado, em transação
    própria: se o processo morrer entre o envio e o `UPDATE`, o retry
    reenvia UM balão. Errar por um a mais é recuperável; pela sequência
    inteira, não era.

    `extrair_baloes` já aplica um teto (`settings.balao_max_count`) que
    concatena o excedente no último item, então `baloes` aqui nunca é maior
    que o teto — importante porque o `sleep` entre balões roda dentro do
    lease da mensagem (`settings.lease_seconds`); sem teto, uma resposta com
    dezenas de itens somaria mais tempo de sleep que o lease, parando o
    worker (hoje sem duplicar, porque o loop é serial e ninguém rouba o
    lease — mas duplicaria de verdade com mais de um worker).
    """
    total = len(baloes)
    delay_s = settings.balao_delay_ms / 1000
    ja_entregues = message.baloes_enviados
    if ja_entregues:
        logger.info(
            "baloes_retomados",
            message_id=message.id,
            ja_entregues=ja_entregues,
            balao_total=total,
        )
    for idx, balao in enumerate(baloes):
        if idx < ja_entregues:
            continue
        try:
            await _send_message(outbound, message.phone_number, balao, message)
        except Exception:
            logger.error(
                "balao_send_failed",
                message_id=message.id,
                phone=message.phone_number,
                channel=message.channel.value,
                balao_index=idx,
                balao_total=total,
                ja_entregues=ja_entregues,
            )
            raise
        # Best-effort, e de propósito: o balão JÁ chegou ao lead. Deixar
        # uma falha de contabilidade abortar a sequência entregaria uma
        # resposta pela metade para consertar um contador — troca ruim. O
        # custo de não gravar é o retry reenviar este balão, que é
        # exatamente o comportamento que existia antes da migração 017.
        try:
            await registrar_balao_enviado(pool, message.id, idx + 1)
        except Exception as reg_err:
            logger.warning(
                "registrar_balao_enviado_falhou",
                message_id=message.id,
                balao_index=idx,
                error=str(reg_err),
            )
        if idx < total - 1:
            await asyncio.sleep(delay_s)


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
            canal=message.channel,
            message_key=message.provider_message_key,
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
        result = await asyncio.wait_for(
            graph.ainvoke(
                {"messages": [human_message]},
                config=invoke_config,
            ),
            timeout=settings.agent_timeout_seconds,
        )

        # 4. Extrair resposta
        response_text = result["messages"][-1].content

        # 5. Enviar resposta outbound antes de mark_done. Só a Renata
        # (elevec_sdr) devolve JSON estruturado em balões — mesmo mecanismo
        # do outputParserStructured do n8n: parse do TEXTO FINAL, depois que
        # o ciclo de tools terminou, não `response_format` nativo (que
        # quebra o schema quando há tool call pendente no mesmo turno). Os
        # demais agentes do catálogo respondem texto puro; extrair_baloes
        # não é acionado para eles, e o comportamento existente (um único
        # send_message com o texto integral) fica idêntico.
        if message.agent_id == BALOES_AGENT_ID:
            # Import lazy e local ao branch: load_graph(message.agent_id, ...)
            # acima já importou dinamicamente o pacote catalog.elevec_sdr
            # para chegar até aqui (agent_id só é "elevec_sdr" se o grafo da
            # Renata acabou de ser carregado), então este import não soma
            # custo novo — só evita carregar o catálogo dela em deploys que
            # nunca usam esse agent_id.
            from whatsapp_langchain.agents.catalog.elevec_sdr.saida import (
                extrair_baloes,
            )

            baloes = extrair_baloes(response_text)
        else:
            # response_text normalmente é str, mas `BaseMessage.content` no
            # langchain_core é tipado `str | list[str | dict]` — um agente
            # fora da Renata que algum dia devolver content blocks não pode
            # quebrar o "\n".join(baloes) do upsert_conversation abaixo com
            # TypeError (join exige que todo item da lista seja string).
            baloes = [
                response_text if isinstance(response_text, str) else str(response_text)
            ]

        await _send_baloes(pool, client, message, baloes)

        # 6. mark_done somente após envio confirmado. Grava o response_text
        # CRU (o JSON completo, se for a Renata) — é o registro de auditoria
        # do output exato do modelo, útil para diagnosticar problema de
        # parsing depois. upsert_conversation, por outro lado, alimenta
        # conversations.last_message, que o admin panel trunca para preview
        # em /chats — gravar o JSON cru ali faria toda conversa da Renata
        # aparecer como '{"messages": ["Oi! Tudo bem? Aqui é a Rena' na
        # lista. Os balões unidos por "\n" são o texto que o lead de fato
        # recebeu; para os demais agentes (baloes = [response_text]) o join
        # é idêntico ao texto puro de sempre.
        await mark_done(
            pool,
            message.id,
            response_text,
            normalized_input=pre.normalized_text,
            media_processing_status=pre.media_processing_status,
            media_processing_error=pre.media_processing_error,
        )
        # `upsert_conversation` alimenta só o preview do admin panel — não é
        # entrega nem auditoria. Depois do `mark_done` acima, a mensagem já
        # saiu e já está contabilizada; deixar uma falha aqui subir para o
        # `except` faria `mark_failed` reprocessar uma linha `done` e o lead
        # receber tudo de novo. A guarda `status <> 'done'` de `mark_failed`
        # fecha esse caminho no banco, e este `try` o fecha aqui: um preview
        # perdido não vale um WhatsApp duplicado.
        try:
            await upsert_conversation(
                pool,
                phone_number=message.phone_number,
                agent_id=message.agent_id,
                last_message="\n".join(baloes),
            )
        except Exception as conv_err:
            logger.warning(
                "upsert_conversation_falhou",
                message_id=message.id,
                phone=message.phone_number,
                error=str(conv_err),
            )

        logger.info(
            "message_processed",
            message_id=message.id,
            phone=message.phone_number,
            agent_id=message.agent_id,
            channel=message.channel.value,
            response_length=len(response_text),
            balao_count=len(baloes),
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
