"""O processor escolhe o cliente Evolution e propaga canal/key para a mídia."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from whatsapp_langchain.shared.models import MessageQueue, MessagingChannel
from whatsapp_langchain.worker.evolution_client import EvolutionClient
from whatsapp_langchain.worker.media import MediaPreprocessResult
from whatsapp_langchain.worker.processor import (
    _normalize_outbounds,
    _select_client,
    process_message,
)


@pytest.fixture
def evolution():
    return EvolutionClient(
        base_url="https://e.host",
        api_key="chave",
        instance="inst",
        delivery_mode="mock",
    )


def mensagem(
    channel: MessagingChannel,
    *,
    media_url: str | None = None,
    media_type: str | None = None,
    provider_message_key: dict | None = None,
) -> MessageQueue:
    return MessageQueue(
        id=1,
        phone_number="+551187654321",
        agent_id="illumi_assistant",
        thread_id="+551187654321:illumi_assistant",
        incoming_message="oi",
        media_url=media_url,
        media_type=media_type,
        provider_message_key=provider_message_key,
        channel=channel,
        status="queued",
        attempts=0,
        created_at=datetime.now(UTC),
    )


def test_seleciona_cliente_evolution(evolution):
    clientes = _normalize_outbounds({MessagingChannel.EVOLUTION: evolution}, None, None)
    assert _select_client(clientes, mensagem(MessagingChannel.EVOLUTION)) is evolution


def test_canal_nao_habilitado_da_erro_claro(evolution):
    clientes = _normalize_outbounds({MessagingChannel.EVOLUTION: evolution}, None, None)
    with pytest.raises(ValueError, match="não está habilitado"):
        _select_client(clientes, mensagem(MessagingChannel.TWILIO))


def test_cliente_legado_evolution_e_reconhecido(evolution):
    """Path legado: cliente único sem dict não pode cair no default Twilio."""
    clientes = _normalize_outbounds(None, evolution, None)
    assert clientes == {MessagingChannel.EVOLUTION: evolution}


class TestPropagacaoCanalEKey:
    """O processor entrega canal e provider_message_key ao pré-processador.

    Sem isso, mídia da Evolution cai no default Twilio e um GET na URL
    cifrada do mmg.whatsapp.net manda lixo para o LLM.
    """

    def _patches(self, resultado):
        return (
            patch(
                "whatsapp_langchain.worker.processor.preprocess_incoming_message",
                new_callable=AsyncMock,
                return_value=resultado,
            ),
            patch(
                "whatsapp_langchain.worker.processor.mark_done",
                new_callable=AsyncMock,
            ),
            patch(
                "whatsapp_langchain.worker.processor.mark_failed",
                new_callable=AsyncMock,
            ),
            patch(
                "whatsapp_langchain.worker.processor.upsert_conversation",
                new_callable=AsyncMock,
            ),
        )

    async def test_propaga_canal_e_key_da_evolution(self, evolution):
        resultado = MediaPreprocessResult(
            should_invoke_agent=False,
            normalized_text=None,
            media_processing_status="disabled",
            auto_response="Imagens desabilitadas.",
        )
        msg = mensagem(
            MessagingChannel.EVOLUTION,
            media_url="https://mmg.whatsapp.net/algo.enc",
            media_type="image/jpeg",
            provider_message_key={"id": "MSG1", "remoteJid": "5511@s.whatsapp.net"},
        )

        pre, _done, _failed, _conv = self._patches(resultado)
        with pre as mock_pre, _done, _failed, _conv:
            await process_message(
                msg,
                AsyncMock(),
                checkpointer=AsyncMock(),
                outbounds={MessagingChannel.EVOLUTION: evolution},
            )

        kwargs = mock_pre.await_args.kwargs
        assert kwargs["canal"] == MessagingChannel.EVOLUTION
        assert kwargs["message_key"] == {
            "id": "MSG1",
            "remoteJid": "5511@s.whatsapp.net",
        }

    async def test_propaga_canal_twilio_por_padrao(self):
        resultado = MediaPreprocessResult(
            should_invoke_agent=False,
            normalized_text=None,
            media_processing_status="disabled",
            auto_response="Imagens desabilitadas.",
        )
        msg = mensagem(
            MessagingChannel.TWILIO,
            media_url="https://api.twilio.com/m.jpg",
            media_type="image/jpeg",
        )
        twilio = AsyncMock()
        twilio.send_message = AsyncMock(return_value="SM1")

        pre, _done, _failed, _conv = self._patches(resultado)
        with pre as mock_pre, _done, _failed, _conv:
            await process_message(
                msg,
                AsyncMock(),
                checkpointer=AsyncMock(),
                outbounds={MessagingChannel.TWILIO: twilio},
            )

        kwargs = mock_pre.await_args.kwargs
        assert kwargs["canal"] == MessagingChannel.TWILIO
        assert kwargs["message_key"] is None
