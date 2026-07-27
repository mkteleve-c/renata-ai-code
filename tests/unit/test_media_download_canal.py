"""download_media decide o esquema de autenticação pelo canal de origem.

Na integração WHATSAPP-BUSINESS a mídia NÃO é criptografada: a URL do
payload aponta para `lookaside.fbsbx.com` e responde a um GET com
`Authorization: Bearer <apikey da instância>` (medido contra a instância
real — áudio 200/audio/ogg, imagem 200/image/jpeg). O
`getBase64FromMediaMessage` é o caminho de instância Baileys e não participa
mais deste fluxo.
"""

import httpx
import pytest

from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.worker import media

URL_CLOUD_API = (
    "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123&ext=1"
)


def _mockar_transporte(monkeypatch, handler) -> None:
    """Faz o httpx.AsyncClient de media.py nascer com MockTransport."""
    real = httpx.AsyncClient

    def fabrica(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(media.httpx, "AsyncClient", fabrica)


@pytest.fixture
def credenciais(monkeypatch):
    monkeypatch.setattr(media.settings, "evolution_base_url", "https://e.host")
    monkeypatch.setattr(media.settings, "evolution_api_key", "chave-da-instancia")
    monkeypatch.setattr(media.settings, "evolution_instance", "inst")


async def test_evolution_baixa_por_get_com_bearer(monkeypatch, credenciais):
    capturado = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        capturado["method"] = request.method
        capturado["url"] = str(request.url)
        capturado["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"OggS\x00audio")

    _mockar_transporte(monkeypatch, handler)

    conteudo = await media.download_media(
        URL_CLOUD_API,
        canal=MessagingChannel.EVOLUTION,
        message_key={"id": "MSG1", "remoteJid": "5511@s.whatsapp.net"},
    )

    assert conteudo == b"OggS\x00audio"
    assert capturado["method"] == "GET"
    assert capturado["url"] == URL_CLOUD_API
    assert capturado["authorization"] == "Bearer chave-da-instancia"


async def test_evolution_nao_chama_getbase64(monkeypatch, credenciais):
    """A key deixou de ser via de download — nada de POST no endpoint base64."""

    async def proibido(*args, **kwargs):
        raise AssertionError("getBase64FromMediaMessage não participa do download")

    monkeypatch.setattr(
        "whatsapp_langchain.worker.evolution_client.EvolutionClient.baixar_midia",
        proibido,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"JFIF-bytes")

    _mockar_transporte(monkeypatch, handler)

    conteudo = await media.download_media(
        URL_CLOUD_API,
        canal=MessagingChannel.EVOLUTION,
        message_key={"id": "MSG1"},
    )

    assert conteudo == b"JFIF-bytes"


async def test_evolution_sem_url_falha_mesmo_com_key(monkeypatch, credenciais):
    """Sem URL não há download: a key sozinha não baixa nada nesta integração."""
    with pytest.raises(ValueError, match="URL"):
        await media.download_media(
            "",
            canal=MessagingChannel.EVOLUTION,
            message_key={"id": "MSG1", "remoteJid": "5511@s.whatsapp.net"},
        )


async def test_evolution_propaga_erro_http(monkeypatch, credenciais):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    _mockar_transporte(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await media.download_media(
            URL_CLOUD_API,
            canal=MessagingChannel.EVOLUTION,
            message_key={"id": "MSG1"},
        )


async def test_twilio_continua_com_basicauth(monkeypatch):
    monkeypatch.setattr(media.settings, "twilio_api_key_sid", "SK123")
    monkeypatch.setattr(media.settings, "twilio_api_key_secret", "segredo")
    capturado = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        capturado["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"twilio-bytes")

    _mockar_transporte(monkeypatch, handler)

    conteudo = await media.download_media(
        "https://api.twilio.com/midia.jpg",
        canal=MessagingChannel.TWILIO,
    )

    assert conteudo == b"twilio-bytes"
    assert capturado["authorization"].startswith("Basic ")
