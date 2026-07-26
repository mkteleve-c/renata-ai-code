"""Pré-processamento de mídia no canal Evolution.

Na Evolution a URL do payload é cifrada e pode até faltar — a via de
download é a `provider_message_key`. O corte por "sem URL" não pode
descartar a mensagem antes de tentar o download por key.
"""

from unittest.mock import AsyncMock, patch

from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.worker.media import (
    AUTO_RESPONSE_UNSUPPORTED_MEDIA,
    preprocess_incoming_message,
)

KEY = {"id": "MSG1", "remoteJid": "5511987654321@s.whatsapp.net"}


class TestPreprocessEvolution:
    async def test_evolution_sem_url_baixa_por_key(self):
        """Sem media_url mas com key: existe via de download, não é 'unsupported'."""
        with (
            patch.object(settings, "media_image_enabled", True),
            patch(
                "whatsapp_langchain.worker.media.download_media",
                new=AsyncMock(return_value=b"img-bytes"),
            ),
            patch(
                "whatsapp_langchain.worker.media._describe_image",
                new=AsyncMock(return_value="uma planta baixa"),
            ),
        ):
            result = await preprocess_incoming_message(
                body="olha",
                media_url=None,
                media_type="image/jpeg",
                canal=MessagingChannel.EVOLUTION,
                message_key=KEY,
            )

        assert result.should_invoke_agent is True
        assert result.media_processing_status == "processed"
        assert "[Descrição de imagem]: uma planta baixa" in (
            result.normalized_text or ""
        )

    async def test_evolution_passa_canal_e_key_ao_download(self):
        download = AsyncMock(return_value=b"audio-bytes")
        with (
            patch.object(settings, "media_audio_enabled", True),
            patch("whatsapp_langchain.worker.media.download_media", new=download),
            patch(
                "whatsapp_langchain.worker.media._transcribe_audio",
                new=AsyncMock(return_value="bom dia"),
            ),
        ):
            result = await preprocess_incoming_message(
                body="",
                media_url="https://mmg.whatsapp.net/algo.enc",
                media_type="audio/ogg",
                canal=MessagingChannel.EVOLUTION,
                message_key=KEY,
            )

        assert result.should_invoke_agent is True
        kwargs = download.await_args.kwargs
        assert kwargs["canal"] == MessagingChannel.EVOLUTION
        assert kwargs["message_key"] == KEY

    async def test_evolution_sem_url_e_sem_key_continua_unsupported(self):
        result = await preprocess_incoming_message(
            body="olha",
            media_url=None,
            media_type="image/jpeg",
            canal=MessagingChannel.EVOLUTION,
            message_key=None,
        )
        assert result.should_invoke_agent is False
        assert result.auto_response == AUTO_RESPONSE_UNSUPPORTED_MEDIA

    async def test_outros_canais_sem_url_continuam_unsupported(self):
        """Twilio sem URL não ganha via de download por key."""
        result = await preprocess_incoming_message(
            body="olha",
            media_url=None,
            media_type="image/jpeg",
            canal=MessagingChannel.TWILIO,
            message_key=KEY,
        )
        assert result.should_invoke_agent is False
        assert result.auto_response == AUTO_RESPONSE_UNSUPPORTED_MEDIA
