"""O processor envia a resposta da Renata em múltiplos balões.

Só `elevec_sdr` devolve JSON estruturado (`{"messages": [...]}`) no texto
final — os demais agentes do catálogo (illumi_assistant, rhawk_assistant)
respondem texto puro e não podem passar pelo parser `extrair_baloes`.

Cobre:
- N balões viram N chamadas de send_message, na ordem, espaçadas por sleep
- mark_done só acontece depois do ÚLTIMO balão confirmado
- agente que não é elevec_sdr nunca aciona o parser, mesmo se o texto
  parecer JSON de balões (comportamento existente preservado)
- falha no meio da sequência: os balões já enviados não são reenviados
  aqui — a exceção sobe para mark_failed (retry existente cobre o resto)
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from whatsapp_langchain.shared.models import MessageQueue, MessagingChannel
from whatsapp_langchain.worker.media import MediaPreprocessResult
from whatsapp_langchain.worker.twilio_client import TwilioSendError

TEXT_PREPROCESS = MediaPreprocessResult(
    should_invoke_agent=True,
    normalized_text="Olá!",
    media_processing_status="none",
)


def mensagem(agent_id: str) -> MessageQueue:
    return MessageQueue(
        id=1,
        message_id="SM123",
        phone_number="+5511999999999",
        agent_id=agent_id,
        thread_id=f"+5511999999999:{agent_id}",
        incoming_message="Olá!",
        channel=MessagingChannel.TWILIO,
        status="queued",
        attempts=0,
        created_at=datetime.now(UTC),
    )


def _patches():
    return (
        patch(
            "whatsapp_langchain.worker.processor.preprocess_incoming_message",
            new_callable=AsyncMock,
            return_value=TEXT_PREPROCESS,
        ),
        patch("whatsapp_langchain.worker.processor.load_graph"),
        patch("whatsapp_langchain.worker.processor.mark_done", new_callable=AsyncMock),
        patch(
            "whatsapp_langchain.worker.processor.mark_failed", new_callable=AsyncMock
        ),
        patch(
            "whatsapp_langchain.worker.processor.upsert_conversation",
            new_callable=AsyncMock,
        ),
        patch(
            "whatsapp_langchain.worker.processor.asyncio.sleep", new_callable=AsyncMock
        ),
    )


async def test_elevec_sdr_envia_um_balao_por_item():
    msg = mensagem("elevec_sdr")
    twilio = AsyncMock()
    twilio.send_typing = AsyncMock(return_value=True)
    twilio.send_message = AsyncMock(return_value="SM_OK")

    pre, load, done, failed, conv, sleep = _patches()
    with (
        pre,
        load as mock_load,
        done as mock_done,
        failed as mock_failed,
        conv,
        sleep as mock_sleep,
    ):
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {
            "messages": [
                MagicMock(content='{"messages": ["oi", "tudo bem?", "vamos marcar?"]}')
            ]
        }
        mock_load.return_value = mock_graph

        from whatsapp_langchain.worker.processor import process_message

        await process_message(msg, AsyncMock(), checkpointer=AsyncMock(), twilio=twilio)

        assert twilio.send_message.await_count == 3
        chamadas = [c.args for c in twilio.send_message.await_args_list]
        assert chamadas == [
            ("+5511999999999", "oi"),
            ("+5511999999999", "tudo bem?"),
            ("+5511999999999", "vamos marcar?"),
        ]
        # sleep entre balões, não depois do último: 2 gaps para 3 balões
        assert mock_sleep.await_count == 2
        assert mock_done.await_count == 1
        mock_failed.assert_not_awaited()


async def test_agente_sem_json_nao_e_afetado():
    """rhawk_assistant não aciona extrair_baloes mesmo com texto parecido com JSON."""
    msg = mensagem("rhawk_assistant")
    twilio = AsyncMock()
    twilio.send_typing = AsyncMock(return_value=True)
    twilio.send_message = AsyncMock(return_value="SM_OK")

    texto_parecido_com_json = '{"messages": ["oi", "tchau"]}'

    pre, load, done, failed, conv, sleep = _patches()
    with pre, load as mock_load, done as mock_done, failed as mock_failed, conv, sleep:
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {
            "messages": [MagicMock(content=texto_parecido_com_json)]
        }
        mock_load.return_value = mock_graph

        from whatsapp_langchain.worker.processor import process_message

        await process_message(msg, AsyncMock(), checkpointer=AsyncMock(), twilio=twilio)

        # Um único send_message, com o texto integral (cru) — comportamento
        # existente preservado.
        twilio.send_message.assert_awaited_once_with(
            "+5511999999999", texto_parecido_com_json
        )
        assert mock_done.await_count == 1
        mock_failed.assert_not_awaited()


async def test_falha_no_meio_nao_reenvia_os_ja_entregues():
    """2º de 3 balões falha: 1º não é reenviado, mark_done não roda, mark_failed sim."""
    msg = mensagem("elevec_sdr")
    twilio = AsyncMock()
    twilio.send_typing = AsyncMock(return_value=True)
    twilio.send_message = AsyncMock(
        side_effect=[
            "SM1",
            TwilioSendError(500, "Internal Server Error"),
            "SM3",
        ]
    )

    pre, load, done, failed, conv, sleep = _patches()
    with pre, load as mock_load, done as mock_done, failed as mock_failed, conv, sleep:
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {
            "messages": [
                MagicMock(content='{"messages": ["oi", "tudo bem?", "vamos marcar?"]}')
            ]
        }
        mock_load.return_value = mock_graph

        from whatsapp_langchain.worker.processor import process_message

        await process_message(msg, AsyncMock(), checkpointer=AsyncMock(), twilio=twilio)

        # Só os 2 primeiros balões foram tentados — o 3º nunca é alcançado
        # porque a exceção do 2º sobe e interrompe o loop.
        assert twilio.send_message.await_count == 2
        mock_done.assert_not_awaited()
        mock_failed.assert_awaited_once()
        assert "500" in mock_failed.call_args[0][2]


async def test_json_de_um_item_so_gera_um_send_e_nenhum_sleep():
    msg = mensagem("elevec_sdr")
    twilio = AsyncMock()
    twilio.send_typing = AsyncMock(return_value=True)
    twilio.send_message = AsyncMock(return_value="SM_OK")

    pre, load, done, failed, conv, sleep = _patches()
    with pre, load as mock_load, done as mock_done, failed, conv, sleep as mock_sleep:
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {
            "messages": [MagicMock(content='{"messages": ["oi"]}')]
        }
        mock_load.return_value = mock_graph

        from whatsapp_langchain.worker.processor import process_message

        await process_message(msg, AsyncMock(), checkpointer=AsyncMock(), twilio=twilio)

        twilio.send_message.assert_awaited_once_with("+5511999999999", "oi")
        mock_sleep.assert_not_awaited()
        assert mock_done.await_count == 1


async def test_auditoria_grava_cru_ui_grava_baloes_legiveis():
    """Fix round 1 (Importante 4): mark_done (message_queue.response, auditoria)
    grava o JSON cru; upsert_conversation (conversations.last_message, usado
    no preview truncado de /chats no admin panel) grava os balões unidos por
    "\\n" — não o JSON, que apareceria cru e truncado na lista de conversas.
    """
    msg = mensagem("elevec_sdr")
    twilio = AsyncMock()
    twilio.send_typing = AsyncMock(return_value=True)
    twilio.send_message = AsyncMock(return_value="SM_OK")

    bruto = '{"messages": ["oi", "tudo bem?"]}'

    pre, load, done, failed, conv, sleep = _patches()
    with pre, load as mock_load, done as mock_done, failed, conv as mock_conv, sleep:
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [MagicMock(content=bruto)]}
        mock_load.return_value = mock_graph

        from whatsapp_langchain.worker.processor import process_message

        await process_message(msg, AsyncMock(), checkpointer=AsyncMock(), twilio=twilio)

        assert mock_done.await_args.args[2] == bruto
        assert mock_conv.await_args.kwargs["last_message"] == "oi\ntudo bem?"
