"""Testes do PipedriveClient com httpx mockado.

**Nenhum teste move card contra a API real.** O Pipedrive é o CRM de
produção da empresa; a verificação contra ele foi feita à parte e só em
leitura (`GET /users/me`, `GET /stages`), o suficiente para confirmar que os
`stage_id` 12 e 13 existem e que o header `x-api-token` autentica. Escrita
fica coberta só por mock.
"""

import json

import httpx
import pytest

from whatsapp_langchain.shared.pipedrive import (
    STATUS_SEM_RESPOSTA,
    PipedriveClient,
    PipedriveError,
    validar_deal_id,
)


@pytest.fixture
def client():
    return PipedriveClient(api_token="tok-secreto", base_url="https://pd.exemplo/v1")


def montar(client, handler):
    client._transport = httpx.MockTransport(handler)
    return client


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": {"id": 42}})


# --- Construção -------------------------------------------------------------


def test_exige_api_token():
    with pytest.raises(ValueError):
        PipedriveClient(api_token="")


# --- Requisição -------------------------------------------------------------


async def test_move_card_com_put_e_stage_id_no_corpo(client):
    vistas = []

    async def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        return ok(request)

    await montar(client, handler).mover_card("42", 12)

    assert vistas[0].method == "PUT"
    assert str(vistas[0].url) == "https://pd.exemplo/v1/deals/42"
    assert json.loads(vistas[0].content) == {"stage_id": 12}


async def test_token_vai_no_header_e_nunca_na_url(client):
    vistas = []

    async def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        return ok(request)

    await montar(client, handler).mover_card("42", 13)

    assert vistas[0].headers["x-api-token"] == "tok-secreto"
    # O `?api_token=` também funciona na API real, mas põe o segredo dentro
    # de `str(url)` — que é o que vai para o log e para o traceback.
    assert "tok-secreto" not in str(vistas[0].url)


# --- Falhas -----------------------------------------------------------------


async def test_status_de_erro_levanta_com_o_corpo_no_detail(client):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='{"error":"Deal not found"}')

    with pytest.raises(PipedriveError) as exc:
        await montar(client, handler).mover_card("999", 12)

    assert exc.value.status_code == 404
    assert "Deal not found" in exc.value.detail


async def test_200_com_success_false_e_falha(client):
    # O Pipedrive responde 200 com `success: false` em parte dos erros de
    # validação. Ler só o status faria a tool dizer que moveu o card.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "error": "stage_id inválido"}
        )

    with pytest.raises(PipedriveError) as exc:
        await montar(client, handler).mover_card("42", 99)

    assert "stage_id inválido" in exc.value.detail


async def test_falha_de_transporte_vira_status_sem_resposta(client):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    with pytest.raises(PipedriveError) as exc:
        await montar(client, handler).mover_card("42", 12)

    assert exc.value.status_code == STATUS_SEM_RESPOSTA


async def test_2xx_com_corpo_irreconhecivel_nao_levanta(client):
    # O PUT já foi aplicado; levantar aqui levaria o processor ao retry de
    # uma escrita que já valeu.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>manutenção</html>")

    await montar(client, handler).mover_card("42", 12)


# --- deal_id ----------------------------------------------------------------


@pytest.mark.parametrize(
    "bruto",
    ["42", " 42 ", "1000000"],
)
def test_deal_id_numerico_e_aceito(bruto):
    assert validar_deal_id(bruto) == bruto.strip()


@pytest.mark.parametrize(
    "bruto",
    ["", "  ", "abc", "42/../../users/me", "42?x=1", "4 2", "-1"],
)
def test_deal_id_nao_numerico_e_recusado(bruto):
    # `pipedriveid` é TEXT populado por importação do banco legado. Sem esta
    # checagem, um id com `/` viraria outra rota da API com o token válido.
    with pytest.raises(ValueError):
        validar_deal_id(bruto)


async def test_deal_id_invalido_nao_chega_a_sair_pela_rede(client):
    saiu = []

    async def handler(request: httpx.Request) -> httpx.Response:
        saiu.append(request)
        return ok(request)

    with pytest.raises(ValueError):
        await montar(client, handler).mover_card("42/../../users/me", 12)

    assert saiu == []
