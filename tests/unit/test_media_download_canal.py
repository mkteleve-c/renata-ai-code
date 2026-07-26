"""download_media decide o caminho pelo canal de origem."""

import pytest

from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.worker import media


async def test_evolution_usa_base64_e_ignora_url(monkeypatch):
    chamado = {}

    async def fake_baixar(self, message_key):
        chamado["key"] = message_key
        return b"conteudo-decifrado"

    monkeypatch.setattr(
        "whatsapp_langchain.worker.evolution_client.EvolutionClient.baixar_midia",
        fake_baixar,
    )
    monkeypatch.setattr(media.settings, "evolution_base_url", "https://e.host")
    monkeypatch.setattr(media.settings, "evolution_api_key", "chave")
    monkeypatch.setattr(media.settings, "evolution_instance", "inst")

    conteudo = await media.download_media(
        "https://mmg.whatsapp.net/algo.enc",
        canal=MessagingChannel.EVOLUTION,
        message_key={"id": "MSG1"},
    )

    assert conteudo == b"conteudo-decifrado"
    assert chamado["key"] == {"id": "MSG1"}


async def test_evolution_sem_message_key_falha():
    with pytest.raises(ValueError, match="message_key"):
        await media.download_media(
            "https://mmg.whatsapp.net/algo.enc",
            canal=MessagingChannel.EVOLUTION,
            message_key=None,
        )
