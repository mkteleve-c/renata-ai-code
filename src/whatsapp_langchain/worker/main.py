"""Entry point do Worker — loop de processamento de mensagens.

Inicia o Worker que consome mensagens da fila PostgreSQL em loop.
Cada mensagem é processada pelo agente configurado, e a resposta é
enviada pelo cliente outbound correspondente ao canal de origem
(`message.channel`: twilio, meta ou uazapi).

Uso:
    python -m whatsapp_langchain.worker.main
"""

import asyncio

import structlog

from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import (
    bootstrap_langgraph_schema,
    close_pool,
    get_pool,
    open_checkpointer,
    open_store,
    run_migrations,
)
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.observability import setup_logging
from whatsapp_langchain.worker.consumer import claim_next_message
from whatsapp_langchain.worker.meta_client import MetaClient
from whatsapp_langchain.worker.processor import OutboundClient, process_message
from whatsapp_langchain.worker.twilio_client import TwilioClient
from whatsapp_langchain.worker.uazapi_client import UazapiClient

logger = structlog.get_logger()


def _build_outbound_clients(
    outbound_mode: str,
) -> dict[MessagingChannel, OutboundClient]:
    """Instancia clientes outbound de todos os canais habilitados.

    Um canal é considerado habilitado quando suas credenciais estão
    completas (`settings.channel_status()`). Canais "tocados" mas
    incompletos já teriam disparado erro em `validate_runtime_settings`
    chamado pela API no startup; mesmo assim revalidamos aqui para o caso
    do worker subir antes da API.

    Em modo mock, todos os 3 canais são instanciados (cliente simula envio).
    """
    status = settings.channel_status()
    clients: dict[MessagingChannel, OutboundClient] = {}

    if status["twilio"]["complete"] and (
        status["twilio"]["touched"] or outbound_mode == "mock"
    ):
        twilio = TwilioClient(
            account_sid=settings.twilio_account_sid,
            api_key_sid=settings.twilio_api_key_sid,
            api_key_secret=settings.twilio_api_key_secret,
            from_number=settings.twilio_from_number,
            delivery_mode=outbound_mode,
        )
        clients[MessagingChannel.TWILIO] = twilio
        logger.info(
            "twilio_client_ready",
            outbound_mode=outbound_mode,
            from_number=settings.twilio_from_number or None,
        )

    if status["meta"]["complete"] and (
        status["meta"]["touched"] or outbound_mode == "mock"
    ):
        meta = MetaClient(
            phone_number_id=settings.meta_phone_number_id,
            access_token=settings.meta_access_token,
            graph_api_version=settings.meta_graph_api_version,
            delivery_mode=outbound_mode,
        )
        clients[MessagingChannel.META] = meta
        logger.info(
            "meta_client_ready",
            outbound_mode=outbound_mode,
            phone_number_id=settings.meta_phone_number_id or None,
            graph_api_version=settings.meta_graph_api_version,
        )

    if status["uazapi"]["complete"] and (
        status["uazapi"]["touched"] or outbound_mode == "mock"
    ):
        uazapi = UazapiClient(
            base_url=settings.uazapi_base_url,
            instance_token=settings.uazapi_instance_token,
            delivery_mode=outbound_mode,
        )
        clients[MessagingChannel.UAZAPI] = uazapi
        logger.info(
            "uazapi_client_ready",
            outbound_mode=outbound_mode,
            base_url=settings.uazapi_base_url or None,
            has_static_token=bool(settings.uazapi_instance_token),
        )

    if not clients:
        logger.error("no_outbound_channel_enabled", channel_status=status)
        raise SystemExit(
            "Nenhum canal de mensageria está habilitado. Preencha credenciais "
            "de pelo menos um (Twilio, Meta ou uazapi) ou rode em "
            "OUTBOUND_MODE=mock para desenvolvimento local."
        )

    return clients


async def main() -> None:
    """Loop principal do Worker.

    1. Configura logging e banco de dados
    2. Aplica migrações pendentes
    3. Valida config e instancia clientes outbound dos canais habilitados
    4. Entra em loop infinito buscando mensagens na fila
    5. Processa cada mensagem com o agente apropriado, usando o cliente
       outbound correspondente ao canal de origem da mensagem.
    """
    setup_logging(log_level=settings.log_level, json_output=settings.log_json)

    # Em prod a API roda esta validação no lifespan; mas o worker pode
    # iniciar primeiro num scale-up — revalidar aqui é barato e dá fail-fast
    # claro caso credenciais estejam parcialmente configuradas.
    settings.validate_runtime_settings()

    enabled = [
        ch
        for ch, st in settings.channel_status().items()
        if st["complete"] and st["touched"]
    ]
    logger.info("worker_starting", enabled_channels=enabled or ["mock"])

    pool = await get_pool()
    await run_migrations(pool)
    await bootstrap_langgraph_schema()

    checkpointer_stack, checkpointer = await open_checkpointer()
    store_stack, store = await open_store()

    outbound_mode = settings.resolved_outbound_mode
    outbounds = _build_outbound_clients(outbound_mode)

    logger.info(
        "worker_ready",
        poll_interval=settings.poll_interval_seconds,
        memory_enabled=store is not None,
        outbound_mode=outbound_mode,
        enabled_channels=[ch.value for ch in outbounds],
    )

    try:
        while True:
            message = await claim_next_message(pool, settings.lease_seconds)

            if message is None:
                await asyncio.sleep(settings.poll_interval_seconds)
                continue

            await process_message(
                message,
                pool,
                checkpointer=checkpointer,
                store=store,
                outbounds=outbounds,
            )

    except KeyboardInterrupt:
        logger.info("worker_interrupted")
    finally:
        if store_stack is not None:
            await store_stack.aclose()
        await checkpointer_stack.aclose()
        await close_pool()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
