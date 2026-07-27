"""Testes do GoogleCalendarClient com httpx mockado.

Nenhum teste toca a API real. A verificação contra o Google foi feita à
parte, em leitura pura (`listar_eventos` numa janela curta): a agenda é de
trabalho de uma pessoa e escrita fica coberta só por mock.
"""

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from whatsapp_langchain.shared.google_calendar import (
    GoogleCalendarClient,
    GoogleCalendarError,
    formatar_iso,
)

CALENDAR_ID = "silvio@exemplo.com"
CALENDAR_ID_URL = "silvio%40exemplo.com"


@pytest.fixture
def client():
    return GoogleCalendarClient(
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken",
        calendar_id=CALENDAR_ID,
    )


def montar_transport(client, handler_api, resposta_token=None, registro=None):
    """MockTransport que separa o endpoint de token das chamadas de calendário.

    `registro` é uma lista onde cada requisição ao token é anotada — é como
    os testes de renovação contam quantas vezes o refresh saiu.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            if registro is not None:
                registro.append(request)
            if resposta_token is not None:
                return resposta_token(request)
            return httpx.Response(
                200, json={"access_token": "tok-1", "expires_in": 3600}
            )
        return await handler_api(request)

    client._transport = httpx.MockTransport(handler)
    return client


# --- Renovação de token -------------------------------------------------


async def test_renova_token_a_partir_do_refresh_token(client):
    corpos = []

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    def token(request: httpx.Request) -> httpx.Response:
        corpos.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    montar_transport(client, api, resposta_token=token)

    assert await client.obter_access_token() == "tok-1"
    assert corpos[0]["grant_type"] == ["refresh_token"]
    assert corpos[0]["client_id"] == ["cid"]
    assert corpos[0]["client_secret"] == ["csecret"]
    assert corpos[0]["refresh_token"] == ["rtoken"]


async def test_reusa_access_token_enquanto_valido(client):
    tokens = []

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    montar_transport(client, api, registro=tokens)

    inicio = datetime(2026, 7, 28, 8, 0)
    fim = datetime(2026, 7, 28, 22, 0)
    await client.listar_eventos(inicio, fim)
    await client.listar_eventos(inicio, fim)
    await client.listar_eventos(inicio, fim)

    assert len(tokens) == 1


async def test_renova_de_novo_quando_expiracao_cai_dentro_da_margem(client):
    """`expires_in` menor que a margem significa token já nascido vencido.

    Guardar com margem é o que impede um token que vence no meio do voo; o
    outro lado da moeda é que um `expires_in` curto precisa renovar sempre,
    e não ficar servindo um token que o Google já vai recusar.
    """
    tokens = []

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    def token(request: httpx.Request) -> httpx.Response:
        tokens.append(request)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 30})

    montar_transport(client, api, resposta_token=token)

    inicio = datetime(2026, 7, 28, 8, 0)
    fim = datetime(2026, 7, 28, 22, 0)
    await client.listar_eventos(inicio, fim)
    await client.listar_eventos(inicio, fim)

    assert len(tokens) == 2


async def test_renovacao_concorrente_dispara_um_unico_refresh(client):
    """Várias tools no mesmo turno não podem virar N idas ao endpoint.

    O `await asyncio.sleep` no handler é o que dá sentido ao teste: sem uma
    suspensão real, o `gather` roda a primeira corrotina até o fim antes de
    começar a segunda e a corrida nunca acontece — a versão sem lock
    passaria igual.
    """
    tokens = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "oauth2.googleapis.com"
        tokens.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    client._transport = httpx.MockTransport(handler)

    await asyncio.gather(*(client.obter_access_token() for _ in range(5)))

    assert len(tokens) == 1


async def test_token_com_erro_http_vira_google_calendar_error(client):
    async def api(request: httpx.Request) -> httpx.Response:
        raise AssertionError("não deveria chegar ao calendário sem token")

    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    montar_transport(client, api, resposta_token=token)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.obter_access_token()

    assert exc.value.status_code == 400
    assert "invalid_grant" in exc.value.detail


async def test_token_200_sem_access_token_nao_levanta_keyerror(client):
    """A lição da Fase 1: corpo inesperado com 200 não pode virar KeyError."""

    async def api(request: httpx.Request) -> httpx.Response:
        raise AssertionError("não deveria chegar ao calendário")

    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    montar_transport(client, api, resposta_token=token)

    with pytest.raises(GoogleCalendarError):
        await client.obter_access_token()


async def test_token_200_com_corpo_nao_json_nao_levanta_cru(client):
    async def api(request: httpx.Request) -> httpx.Response:
        raise AssertionError("não deveria chegar ao calendário")

    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>oops</html>", headers={"content-type": "text/html"}
        )

    montar_transport(client, api, resposta_token=token)

    with pytest.raises(GoogleCalendarError):
        await client.obter_access_token()


async def test_401_renova_token_e_repete_uma_vez(client):
    chamadas = []
    tokens = []

    async def api(request: httpx.Request) -> httpx.Response:
        chamadas.append(request.headers.get("authorization"))
        if len(chamadas) == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"items": []})

    def token(request: httpx.Request) -> httpx.Response:
        tokens.append(request)
        return httpx.Response(
            200, json={"access_token": f"tok-{len(tokens)}", "expires_in": 3600}
        )

    montar_transport(client, api, resposta_token=token)

    eventos = await client.listar_eventos(
        datetime(2026, 7, 28, 8, 0), datetime(2026, 7, 28, 22, 0)
    )

    assert eventos == []
    assert chamadas == ["Bearer tok-1", "Bearer tok-2"]
    assert len(tokens) == 2


async def test_401_persistente_sobe_como_erro(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "revoked"}})

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.listar_eventos(
            datetime(2026, 7, 28, 8, 0), datetime(2026, 7, 28, 22, 0)
        )

    assert exc.value.status_code == 401


# --- Construção ---------------------------------------------------------


@pytest.mark.parametrize(
    "campo", ["client_id", "client_secret", "refresh_token", "calendar_id"]
)
def test_credencial_vazia_recusa_a_construcao(campo):
    kwargs = {
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rtoken",
        "calendar_id": CALENDAR_ID,
    }
    kwargs[campo] = ""

    with pytest.raises(ValueError, match=campo):
        GoogleCalendarClient(**kwargs)


# --- listar_eventos -----------------------------------------------------


async def test_listar_eventos_monta_janela_em_iso_com_offset_de_sao_paulo(client):
    capturado = {}

    async def api(request: httpx.Request) -> httpx.Response:
        capturado["url"] = request.url
        capturado["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"items": [{"id": "ev1"}]})

    montar_transport(client, api)

    eventos = await client.listar_eventos(
        datetime(2026, 7, 28, 8, 0, 0), datetime(2026, 7, 28, 22, 0, 0)
    )

    url = capturado["url"]
    assert str(url).startswith(
        f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID_URL}/events"
    )
    assert url.params["timeMin"] == "2026-07-28T08:00:00-03:00"
    assert url.params["timeMax"] == "2026-07-28T22:00:00-03:00"
    assert url.params["singleEvents"] == "true"
    assert url.params["orderBy"] == "startTime"
    assert url.params["timeZone"] == "America/Sao_Paulo"
    assert capturado["auth"] == "Bearer tok-1"
    assert eventos == [{"id": "ev1"}]


async def test_listar_eventos_com_erro_http_vira_google_calendar_error(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "forbidden"}})

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.listar_eventos(
            datetime(2026, 7, 28, 8, 0), datetime(2026, 7, 28, 22, 0)
        )

    assert exc.value.status_code == 403
    assert "forbidden" in exc.value.detail


async def test_listar_eventos_com_corpo_inesperado_levanta_tipado_e_nao_lista_vazia(
    client,
):
    """Lista vazia diante de corpo estranho faria a Renata oferecer horário ocupado.

    É o oposto da escrita: aqui errar calado é pior que falhar. O que não
    pode acontecer, nos dois lados, é `KeyError` cru sem log.
    """

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"kind": "calendar#events"})

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError):
        await client.listar_eventos(
            datetime(2026, 7, 28, 8, 0), datetime(2026, 7, 28, 22, 0)
        )


async def test_listar_eventos_com_corpo_nao_json_levanta_tipado(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"nao e json", headers={"content-type": "text/plain"}
        )

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError):
        await client.listar_eventos(
            datetime(2026, 7, 28, 8, 0), datetime(2026, 7, 28, 22, 0)
        )


async def test_listar_eventos_descarta_itens_que_nao_sao_objeto(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "ev1"}, "lixo", None]})

    montar_transport(client, api)

    eventos = await client.listar_eventos(
        datetime(2026, 7, 28, 8, 0), datetime(2026, 7, 28, 22, 0)
    )

    assert eventos == [{"id": "ev1"}]


# --- criar_evento -------------------------------------------------------


async def test_criar_evento_envia_summary_attendees_e_janela(client):
    capturado = {}

    async def api(request: httpx.Request) -> httpx.Response:
        capturado["metodo"] = request.method
        capturado["url"] = request.url
        capturado["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ev-novo", "htmlLink": "https://x"})

    montar_transport(client, api)

    evento = await client.criar_evento(
        summary="Consultoria de Alavancagem de Carreira - Ana",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
        participantes=["ana@exemplo.com", "silvio@exemplo.com"],
        descricao="Lead veio do Instagram",
    )

    assert capturado["metodo"] == "POST"
    assert capturado["url"].params["sendUpdates"] == "all"
    corpo = capturado["body"]
    assert corpo["summary"] == "Consultoria de Alavancagem de Carreira - Ana"
    assert corpo["start"] == {
        "dateTime": "2026-07-30T14:00:00-03:00",
        "timeZone": "America/Sao_Paulo",
    }
    assert corpo["end"]["dateTime"] == "2026-07-30T15:00:00-03:00"
    assert corpo["attendees"] == [
        {"email": "ana@exemplo.com"},
        {"email": "silvio@exemplo.com"},
    ]
    assert corpo["description"] == "Lead veio do Instagram"
    assert evento["id"] == "ev-novo"


async def test_criar_evento_sem_notificar_nao_manda_convite(client):
    capturado = {}

    async def api(request: httpx.Request) -> httpx.Response:
        capturado["url"] = request.url
        return httpx.Response(200, json={"id": "ev"})

    montar_transport(client, api)

    await client.criar_evento(
        summary="x",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
        notificar=False,
    )

    assert capturado["url"].params["sendUpdates"] == "none"


async def test_criar_evento_com_2xx_e_corpo_estranho_nao_levanta(client):
    """O evento já está na agenda — levantar aqui criaria um segundo no retry."""
    chamadas = 0

    async def api(request: httpx.Request) -> httpx.Response:
        nonlocal chamadas
        chamadas += 1
        return httpx.Response(
            200, content=b"nao e json", headers={"content-type": "text/plain"}
        )

    montar_transport(client, api)

    evento = await client.criar_evento(
        summary="x",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
    )

    assert evento == {}
    assert chamadas == 1


async def test_criar_evento_com_erro_http_vira_google_calendar_error(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"message": "conflito"}})

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.criar_evento(
            summary="x",
            inicio=datetime(2026, 7, 30, 14, 0),
            fim=datetime(2026, 7, 30, 15, 0),
        )

    assert exc.value.status_code == 409
    assert "conflito" in exc.value.detail


# --- atualizar_evento ---------------------------------------------------


async def test_atualizar_evento_usa_patch_e_so_manda_o_que_mudou(client):
    capturado = {}

    async def api(request: httpx.Request) -> httpx.Response:
        capturado["metodo"] = request.method
        capturado["url"] = request.url
        capturado["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ev-1"})

    montar_transport(client, api)

    await client.atualizar_evento(
        "ev-1",
        inicio=datetime(2026, 7, 31, 9, 0),
        fim=datetime(2026, 7, 31, 10, 0),
    )

    assert capturado["metodo"] == "PATCH"
    assert str(capturado["url"]).startswith(
        f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID_URL}/events/ev-1"
    )
    assert set(capturado["body"]) == {"start", "end"}
    assert capturado["body"]["start"]["dateTime"] == "2026-07-31T09:00:00-03:00"


async def test_atualizar_evento_sem_nenhum_campo_recusa(client):
    async def api(request: httpx.Request) -> httpx.Response:
        raise AssertionError("não deveria sair pela rede")

    montar_transport(client, api)

    with pytest.raises(ValueError):
        await client.atualizar_evento("ev-1")


async def test_atualizar_evento_com_2xx_e_corpo_estranho_nao_levanta(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})

    montar_transport(client, api)

    assert await client.atualizar_evento("ev-1", summary="novo") == {}


# --- obter_evento -------------------------------------------------------


async def test_obter_evento_devolve_o_corpo(client):
    async def api(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/events/ev-1")
        return httpx.Response(200, json={"id": "ev-1", "status": "confirmed"})

    montar_transport(client, api)

    assert (await client.obter_evento("ev-1"))["status"] == "confirmed"


async def test_obter_evento_inexistente_vira_erro_tipado(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.obter_evento("ev-sumiu")

    assert exc.value.status_code == 404


# --- deletar_evento -----------------------------------------------------


async def test_deletar_evento_manda_delete_e_aceita_204(client):
    capturado = {}

    async def api(request: httpx.Request) -> httpx.Response:
        capturado["metodo"] = request.method
        capturado["url"] = request.url
        return httpx.Response(204)

    montar_transport(client, api)

    assert await client.deletar_evento("ev-1") is None
    assert capturado["metodo"] == "DELETE"
    assert capturado["url"].params["sendUpdates"] == "all"


@pytest.mark.parametrize("status", [404, 410])
async def test_deletar_evento_ja_removido_e_idempotente(client, status):
    """410/404 é o estado desejado já valendo — não é falha de cancelamento."""

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "deleted"}})

    montar_transport(client, api)

    assert await client.deletar_evento("ev-1") is None


async def test_deletar_evento_com_erro_real_levanta(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.deletar_evento("ev-1")

    assert exc.value.status_code == 500


# --- formatar_iso -------------------------------------------------------


def test_formatar_iso_trata_naive_como_relogio_de_sao_paulo():
    """Em produção o container roda em UTC — ler o naive no fuso do processo
    deslocaria toda sugestão de horário em três horas."""
    assert formatar_iso(datetime(2026, 7, 28, 9, 0)) == "2026-07-28T09:00:00-03:00"


def test_formatar_iso_converte_datetime_com_fuso():
    momento = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert formatar_iso(momento) == "2026-07-28T09:00:00-03:00"
