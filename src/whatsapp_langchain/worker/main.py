"""Entry point do Worker — loop de processamento de mensagens.

Inicia o Worker que consome mensagens da fila PostgreSQL em loop.
Cada mensagem é processada pelo agente configurado, e a resposta é
enviada pelo cliente outbound correspondente ao canal de origem
(`message.channel`: twilio, meta, uazapi ou evolution).

Uso:
    python -m whatsapp_langchain.worker.main
"""

import asyncio
from collections.abc import Iterable, Mapping

import structlog
from psycopg_pool import AsyncConnectionPool

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
from whatsapp_langchain.shared.queue import contar_pendentes_por_canal
from whatsapp_langchain.worker.consumer import claim_next_message
from whatsapp_langchain.worker.evolution_client import EvolutionClient
from whatsapp_langchain.worker.followup import ClienteOutbound, rodada
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

    Em modo mock, todos os canais são instanciados (cliente simula envio).
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

    if status["evolution"]["complete"] and (
        status["evolution"]["touched"] or outbound_mode == "mock"
    ):
        evolution = EvolutionClient(
            base_url=settings.evolution_base_url,
            api_key=settings.evolution_api_key,
            instance=settings.evolution_instance,
            delivery_mode=outbound_mode,
        )
        clients[MessagingChannel.EVOLUTION] = evolution
        logger.info(
            "evolution_client_ready",
            outbound_mode=outbound_mode,
            base_url=settings.evolution_base_url or None,
            instance=settings.evolution_instance or None,
        )

    if not clients:
        logger.error("no_outbound_channel_enabled", channel_status=status)
        raise SystemExit(
            "Nenhum canal de mensageria está habilitado. Preencha credenciais "
            "de pelo menos um (Twilio, Meta, uazapi ou Evolution) ou rode em "
            "OUTBOUND_MODE=mock para desenvolvimento local."
        )

    return clients


def _canais_sem_cliente(
    pendentes: dict[str, int],
    habilitados: Iterable[MessagingChannel],
) -> dict[str, int]:
    """Canais com mensagens pendentes na fila e sem cliente outbound aqui.

    Um webhook inbound aceita mensagens mesmo sem credencial outbound
    configurada para o canal. Sem este aviso, a fila enche e cada mensagem
    morre em mark_failed com o worker aparentemente saudável.
    """
    ativos = {canal.value for canal in habilitados}
    return {
        canal: total
        for canal, total in pendentes.items()
        if canal not in ativos and total > 0
    }


async def _loop_followup(pool: AsyncConnectionPool, cliente: ClienteOutbound) -> None:
    """Chama `rodada` em loop, isolando exceção por rodada.

    Sem o `try/except` aqui, uma exceção (erro de banco, timeout do canal)
    mata a task inteira em silêncio até o próximo deploy — "régua parada" e
    "ninguém elegível" ficam indistinguíveis de fora, porque as duas dão zero
    envio. `followup_rodada_falhou` é o sinal que diferencia os dois casos.
    """
    while True:
        try:
            resultado = await rodada(pool, cliente)
            logger.info("followup_rodada", **resultado)
        except Exception as erro:
            logger.warning("followup_rodada_falhou", erro=str(erro))
        await asyncio.sleep(settings.followup_interval_seconds)


def iniciar_followup(
    pool: AsyncConnectionPool,
    outbounds: Mapping[MessagingChannel, ClienteOutbound],
) -> asyncio.Task[None] | None:
    """Sobe a régua de follow-up como task ao lado do loop de consumo.

    Três coisas não óbvias:

    1. `FOLLOWUP_ENABLED=false` é o padrão e é sagrado — subir o worker não
       pode, por si só, começar a mandar WhatsApp para lead nenhum. A flag
       só vira `true` no cutover, deliberadamente.
    2. Sem fail-fast: quando a flag está desligada ou não há cliente
       Evolution em `outbounds`, esta função devolve `None` e loga o
       motivo — nunca `SystemExit`. `validate_runtime_settings` roda também
       no lifespan da API, que não roda follow-up; derrubar o boot por uma
       flag do worker penalizaria a API à toa, e impediria `run_migrations`
       de rodar.
    3. Em `OUTBOUND_MODE=mock`, `channel_status()` marca todo canal como
       completo e `_build_outbound_clients` instancia todos — então "tem
       cliente Evolution" é vacuamente verdadeiro, e a régua SOBE E RODA DE
       VERDADE contra o cliente mock em dev com a flag ligada. Inofensivo
       (o mock não manda WhatsApp nenhum), mas é surpresa — ver
       `test_em_modo_mock_a_regua_sobe_e_roda`.
    """
    if not settings.followup_enabled:
        logger.info("followup_desligado")
        return None

    cliente = outbounds.get(MessagingChannel.EVOLUTION)
    if cliente is None:
        logger.warning(
            "followup_sem_canal",
            canais_disponiveis=[canal.value for canal in outbounds],
        )
        return None

    logger.info(
        "followup_iniciado", interval_seconds=settings.followup_interval_seconds
    )
    return asyncio.create_task(_loop_followup(pool, cliente))


async def _parar_followup(task: asyncio.Task[None] | None) -> None:
    """Cancela e aguarda a task de follow-up no shutdown do worker.

    Extraída à parte para ser testável direto: uma task de background
    esquecida no `finally` não morre sozinha e polui rodadas seguintes de
    testes (e, em produção, continua rodando após o processo achar que já
    encerrou o follow-up).
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


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

    orfaos = _canais_sem_cliente(await contar_pendentes_por_canal(pool), outbounds)
    if orfaos:
        logger.warning(
            "queued_messages_without_outbound_client",
            canais=orfaos,
            enabled_channels=[ch.value for ch in outbounds],
            hint=(
                "Há mensagens na fila de canais sem cliente outbound neste "
                "worker — o inbound aceita, mas cada uma vai falhar no envio. "
                "Preencha as credenciais do canal ou desabilite o webhook."
            ),
        )

    followup_task = iniciar_followup(pool, outbounds)

    logger.info(
        "worker_ready",
        poll_interval=settings.poll_interval_seconds,
        memory_enabled=store is not None,
        outbound_mode=outbound_mode,
        enabled_channels=[ch.value for ch in outbounds],
        followup_task=followup_task is not None,
        allowlist_ativa=settings.allowlist_ativa,
        allowlist_permitidos=len(settings.allowlist_phones),
        allowlist_descartadas=settings.allowlist_descartadas,
        horario_comercial=(
            f"{settings.horario_comercial_inicio}-"
            f"{settings.horario_comercial_fim} seg-sex"
            if settings.horario_comercial_ativo
            else "desligado"
        ),
        calada_agora=settings.em_horario_comercial(),
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
        await _parar_followup(followup_task)
        if store_stack is not None:
            await store_stack.aclose()
        await checkpointer_stack.aclose()
        await close_pool()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
