import httpx
import pytest

from whatsapp_langchain.worker.evolution_client import (
    EvolutionClient,
    EvolutionSendError,
)


def _cliente(handler) -> EvolutionClient:
    cliente = EvolutionClient(
        base_url="https://evo.exemplo",
        api_key="chave",
        instance="inst",
        delivery_mode="real",
    )
    cliente._transport = httpx.MockTransport(handler)
    return cliente


async def test_payload_do_template_com_parametro():
    capturado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["url"] = str(request.url)
        capturado["json"] = __import__("json").loads(request.content)
        capturado["apikey"] = request.headers.get("apikey")
        return httpx.Response(200, json={"key": {"id": "wamid.ABC"}})

    cliente = _cliente(handler)
    msg_id = await cliente.send_template(
        to="5511987654321",
        template="boas_vindas_renata_respondiapp_03",
        parametro_header="Fulano",
    )

    assert msg_id == "wamid.ABC"
    assert capturado["url"] == "https://evo.exemplo/message/sendTemplate/inst"
    assert capturado["apikey"] == "chave"
    assert capturado["json"] == {
        "number": "5511987654321",
        "language": "pt_BR",
        "name": "boas_vindas_renata_respondiapp_03",
        "components": [
            {"type": "header", "parameters": [{"type": "text", "text": "Fulano"}]}
        ],
    }


async def test_template_sem_parametro_nao_inventa_placeholder():
    capturado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["json"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"key": {"id": "wamid.X"}})

    cliente = _cliente(handler)
    await cliente.send_template(to="5511987654321", template="t")
    assert capturado["json"]["components"] == []


async def test_template_em_modo_mock_nao_chama_a_rede():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("modo mock não pode tocar a rede")

    cliente = _cliente(handler)
    cliente.delivery_mode = "mock"
    assert await cliente.send_template(to="551199", template="t") is None


async def test_erro_da_meta_vira_excecao():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "template not found"})

    cliente = _cliente(handler)
    with pytest.raises(EvolutionSendError) as exc:
        await cliente.send_template(to="551199", template="inexistente")
    assert exc.value.status_code == 400
