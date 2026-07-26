"""Testes do EvolutionClient com httpx mockado."""

import base64
import json

import httpx
import pytest

from whatsapp_langchain.worker.evolution_client import (
    EvolutionClient,
    EvolutionSendError,
)

BASE = "https://evolution.exemplo.host"
INSTANCIA = "instancia-teste"
CHAVE = "chave-secreta"


@pytest.fixture
def client():
    return EvolutionClient(
        base_url=BASE, api_key=CHAVE, instance=INSTANCIA, delivery_mode="real"
    )


async def test_envia_texto_com_numero_sem_mais(client, monkeypatch):
    capturado = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        capturado["url"] = str(request.url)
        capturado["apikey"] = request.headers.get("apikey")
        capturado["body"] = json.loads(request.content)
        return httpx.Response(201, json={"key": {"id": "MSG123"}})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    msg_id = await client.send_message("+551187654321", "Olá", delay_ms=1200)

    assert msg_id == "MSG123"
    assert capturado["url"] == f"{BASE}/message/sendText/{INSTANCIA}"
    assert capturado["apikey"] == CHAVE
    assert capturado["body"]["number"] == "551187654321"
    assert capturado["body"]["text"] == "Olá"
    assert capturado["body"]["delay"] == 1200


async def test_erro_http_vira_excecao(client, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))

    with pytest.raises(EvolutionSendError) as exc:
        await client.send_message("+551187654321", "Olá")

    assert exc.value.status_code == 401


async def test_modo_mock_nao_faz_requisicao():
    mock = EvolutionClient(
        base_url=BASE, api_key=CHAVE, instance=INSTANCIA, delivery_mode="mock"
    )
    assert await mock.send_message("+551187654321", "Olá") is None


async def test_baixa_midia_decifrada(client, monkeypatch):
    conteudo = b"\x00\x01audio"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith(f"/chat/getBase64FromMediaMessage/{INSTANCIA}")
        return httpx.Response(200, json={"base64": base64.b64encode(conteudo).decode()})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    assert await client.baixar_midia({"id": "MSG123"}) == conteudo


async def test_send_typing_e_noop(client):
    """False, não None: processor._send_typing é tipado como bool."""
    assert await client.send_typing("+551187654321") is False
