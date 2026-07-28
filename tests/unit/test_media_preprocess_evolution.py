"""Pré-processamento de mídia no canal Evolution.

Na integração WHATSAPP-BUSINESS a URL do payload é aberta e é a via de
download — a `provider_message_key` não baixa nada. Mídia sem URL não tem
como ser resolvida e vira auto-resposta, não silêncio.
"""

from unittest.mock import AsyncMock, patch

from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.worker.media import (
    AUTO_RESPONSE_UNSUPPORTED_MEDIA,
    preprocess_incoming_message,
)

KEY = {"id": "MSG1", "remoteJid": "5511987654321@s.whatsapp.net"}
URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"


class TestPreprocessEvolution:
    async def test_evolution_baixa_pela_url_do_payload(self):
        download = AsyncMock(return_value=b"img-bytes")
        with (
            patch.object(settings, "media_image_enabled", True),
            patch("whatsapp_langchain.worker.media.download_media", new=download),
            patch(
                "whatsapp_langchain.worker.media._describe_image",
                new=AsyncMock(return_value="uma planta baixa"),
            ),
        ):
            result = await preprocess_incoming_message(
                body="olha",
                media_url=URL,
                media_type="image/jpeg",
                canal=MessagingChannel.EVOLUTION,
                message_key=KEY,
            )

        assert result.should_invoke_agent is True
        assert result.media_processing_status == "processed"
        assert "[Descrição de imagem]: uma planta baixa" in (
            result.normalized_text or ""
        )
        assert download.await_args.args[0] == URL

    async def test_evolution_passa_canal_e_key_ao_download(self):
        """O canal decide a autenticação; a key vai junto para diagnóstico."""
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
                media_url=URL,
                media_type="audio/ogg; codecs=opus",
                canal=MessagingChannel.EVOLUTION,
                message_key=KEY,
            )

        assert result.should_invoke_agent is True
        kwargs = download.await_args.kwargs
        assert kwargs["canal"] == MessagingChannel.EVOLUTION
        assert kwargs["message_key"] == KEY

    async def test_evolution_sem_url_vira_unsupported_mesmo_com_key(self):
        """A key deixou de ser via de download: sem URL não há o que baixar."""
        result = await preprocess_incoming_message(
            body="olha",
            media_url=None,
            media_type="image/jpeg",
            canal=MessagingChannel.EVOLUTION,
            message_key=KEY,
        )
        assert result.should_invoke_agent is False
        assert result.auto_response == AUTO_RESPONSE_UNSUPPORTED_MEDIA

    async def test_outros_canais_sem_url_continuam_unsupported(self):
        result = await preprocess_incoming_message(
            body="olha",
            media_url=None,
            media_type="image/jpeg",
            canal=MessagingChannel.TWILIO,
            message_key=KEY,
        )
        assert result.should_invoke_agent is False
        assert result.auto_response == AUTO_RESPONSE_UNSUPPORTED_MEDIA
