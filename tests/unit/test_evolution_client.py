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


async def test_send_typing_e_noop(client, monkeypatch):
    """False, não None: processor._send_typing é tipado como bool.

    E sem tocar a rede: `/chat/sendPresence` lança "Method not available on
    WhatsApp Business API" nessa integração, então uma chamada real seria
    HTTP 400 por mensagem enviada.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"send_typing não pode fazer requisição: {request.url}")

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    assert await client.send_typing("+551187654321") is False


async def test_resposta_nao_json_vira_excecao(client, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"nao e json", headers={"content-type": "text/plain"}
        )

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))

    with pytest.raises(EvolutionSendError):
        await client.send_message("+551187654321", "Olá")


async def test_resposta_sem_chave_key_vira_excecao(client, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "PENDING"})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))

    with pytest.raises(EvolutionSendError):
        await client.send_message("+551187654321", "Olá")


async def test_baixar_midia_sem_base64_vira_excecao(client, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"mimetype": "audio/ogg"})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))

    with pytest.raises(EvolutionSendError):
        await client.baixar_midia({"id": "MSG123"})


async def test_baixar_midia_base64_invalido_vira_excecao(client, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"base64": "###nao-e-base64###"})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))

    with pytest.raises(EvolutionSendError):
        await client.baixar_midia({"id": "MSG123"})


async def test_mensagem_longa_e_quebrada_em_partes_na_ordem_certa(client, monkeypatch):
    corpo = "a" * 5000
    capturados: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        capturados.append(payload["text"])
        return httpx.Response(201, json={"key": {"id": f"MSG-{len(capturados)}"}})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    msg_id = await client.send_message("+551187654321", corpo)

    assert len(capturados) > 1
    assert "".join(capturados) == corpo
    assert msg_id == f"MSG-{len(capturados)}"
