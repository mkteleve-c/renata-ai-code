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
    ALFABETO_EVENT_ID,
    EXPIRACAO_PADRAO_SEGUNDOS,
    STATUS_SEM_RESPOSTA,
    GoogleCalendarClient,
    GoogleCalendarError,
    _segundos_de_expiracao,
    formatar_iso,
    gerar_event_id,
    validar_event_id,
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


async def test_401_concorrente_dispara_um_unico_refresh_extra(client):
    """O caminho que o retry existe para cobrir não pode estourar o endpoint.

    Cinco operações em voo levando 401 ao mesmo tempo — token revogado — é
    exatamente o cenário do retry. A versão anterior usava `forcar=True`, que
    pulava a dupla checagem: o lock apenas SERIALIZAVA as renovações em vez de
    deduplicá-las, e este teste media 6 refreshes (1 inicial + 5). O correto é
    2: o inicial e um único para substituir o token recusado.
    """
    tokens = []
    chamadas_api = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            tokens.append(request)
            await asyncio.sleep(0.01)
            return httpx.Response(
                200, json={"access_token": f"tok-{len(tokens)}", "expires_in": 3600}
            )

        chamadas_api.append(request.headers.get("authorization"))
        await asyncio.sleep(0.01)
        # Só o primeiro token é recusado; o segundo já vale.
        if request.headers.get("authorization") == "Bearer tok-1":
            return httpx.Response(401, json={"error": {"message": "revoked"}})
        return httpx.Response(200, json={"items": []})

    client._transport = httpx.MockTransport(handler)

    inicio = datetime(2026, 7, 28, 8, 0)
    fim = datetime(2026, 7, 28, 22, 0)
    resultados = await asyncio.gather(
        *(client.listar_eventos(inicio, fim) for _ in range(5))
    )

    assert resultados == [[]] * 5
    assert len(tokens) == 2, f"refreshes disparados: {len(tokens)}"
    # 5 chamadas com o token velho (401) + 5 repetidas com o novo.
    assert chamadas_api.count("Bearer tok-1") == 5
    assert chamadas_api.count("Bearer tok-2") == 5


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        (3600, 3600),
        (1800, 1800),
        ("1800", 1800),  # o Google já devolveu expires_in como string
        (3599.9, 3599),  # float trunca, não estoura
        (None, EXPIRACAO_PADRAO_SEGUNDOS),  # campo ausente
        ("", EXPIRACAO_PADRAO_SEGUNDOS),
        ("uma hora", EXPIRACAO_PADRAO_SEGUNDOS),  # não numérico
        ({"segundos": 3600}, EXPIRACAO_PADRAO_SEGUNDOS),  # shape errado
        (0, EXPIRACAO_PADRAO_SEGUNDOS),  # zero viraria renovação por chamada
        (-10, EXPIRACAO_PADRAO_SEGUNDOS),  # negativo idem
    ],
)
def test_segundos_de_expiracao_nunca_estoura(bruto, esperado):
    """A blindagem que o relatório chama de "a forma que um KeyError tomaria".

    `expires_in` ausente, não numérico ou <= 0 nunca era exercitado: os
    testes de token mandavam sempre inteiro válido.
    """
    assert _segundos_de_expiracao(bruto) == esperado


async def test_token_sem_expires_in_ainda_e_cacheado(client):
    """Comportamental: campo ausente não pode virar renovação por chamada.

    Sem o fallback, `_expira_em` ficaria no passado e cada operação pagaria
    um refresh — o desperdício que o cache existe para evitar.
    """
    tokens = []

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    def token(request: httpx.Request) -> httpx.Response:
        tokens.append(request)
        return httpx.Response(200, json={"access_token": "tok"})

    montar_transport(client, api, resposta_token=token)

    inicio = datetime(2026, 7, 28, 8, 0)
    fim = datetime(2026, 7, 28, 22, 0)
    await client.listar_eventos(inicio, fim)
    await client.listar_eventos(inicio, fim)

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
    # O id vai no corpo do insert — é o que torna o retry idempotente.
    assert set(corpo["id"]) <= set(ALFABETO_EVENT_ID)
    assert len(corpo["id"]) == 32


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


async def test_criar_evento_com_2xx_e_corpo_estranho_devolve_o_id_conhecido(client):
    """O evento já está na agenda — levantar aqui criaria um segundo no retry.

    E devolver `{}` deixaria a Renata sem id para remarcar ou cancelar. O id
    foi gerado ANTES do POST, então a identidade nunca se perde.
    """
    chamadas = []

    async def api(request: httpx.Request) -> httpx.Response:
        chamadas.append(json.loads(request.content)["id"])
        return httpx.Response(
            200, content=b"nao e json", headers={"content-type": "text/plain"}
        )

    montar_transport(client, api)

    evento = await client.criar_evento(
        summary="x",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
    )

    assert len(chamadas) == 1
    assert evento == {"id": chamadas[0]}


async def test_criar_evento_com_erro_http_vira_google_calendar_error(client):
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "sem permissao"}})

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.criar_evento(
            summary="x",
            inicio=datetime(2026, 7, 30, 14, 0),
            fim=datetime(2026, 7, 30, 15, 0),
        )

    assert exc.value.status_code == 403
    assert "sem permissao" in exc.value.detail


# --- Idempotência da criação --------------------------------------------


async def test_timeout_no_post_repete_com_o_mesmo_id_e_nao_duplica(client):
    """O caso que motivou a idempotência.

    Timeout num POST que o Google JÁ processou. Sem id fornecido pelo
    cliente, a repetição criaria um segundo evento na agenda de trabalho do
    Silvio e o lead veria o horário duplicado. Com id, a repetição colide em
    409 e o cliente lê o evento que já existe.
    """
    ids_tentados = []
    gets = []

    async def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            ids_tentados.append(json.loads(request.content)["id"])
            if len(ids_tentados) == 1:
                raise httpx.ConnectTimeout("timeout simulado")
            return httpx.Response(
                409,
                json={"error": {"message": "The requested identifier already exists"}},
            )
        gets.append(request.url.path)
        return httpx.Response(200, json={"id": ids_tentados[0], "status": "confirmed"})

    montar_transport(client, api)

    evento = await client.criar_evento(
        summary="Consultoria",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
    )

    assert len(ids_tentados) == 2
    assert ids_tentados[0] == ids_tentados[1], "o retry precisa reusar o MESMO id"
    assert evento == {"id": ids_tentados[0], "status": "confirmed"}
    assert len(gets) == 1, "o 409 é resolvido lendo o evento existente"


async def test_timeout_no_post_que_nao_chegou_cria_na_segunda_tentativa(client):
    """A outra metade: se o POST realmente não chegou, repetir cria mesmo."""
    posts = []

    async def api(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["id"])
        if len(posts) == 1:
            raise httpx.ReadTimeout("timeout simulado")
        return httpx.Response(200, json={"id": posts[-1], "status": "confirmed"})

    montar_transport(client, api)

    evento = await client.criar_evento(
        summary="Consultoria",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
    )

    assert posts[0] == posts[1]
    assert evento["status"] == "confirmed"


async def test_timeout_nas_duas_tentativas_vira_erro_sem_resposta(client):
    """Duas tentativas e para — não é laço infinito.

    `STATUS_SEM_RESPOSTA` diz a quem chama que NÃO SE SABE se o Google
    processou, que é diferente de "não processou".
    """
    posts = 0

    async def api(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        raise httpx.ConnectTimeout("timeout simulado")

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.criar_evento(
            summary="x",
            inicio=datetime(2026, 7, 30, 14, 0),
            fim=datetime(2026, 7, 30, 15, 0),
        )

    assert exc.value.status_code == STATUS_SEM_RESPOSTA
    assert posts == 2
    assert "não respondeu" in str(exc.value)


async def test_409_direto_devolve_o_evento_existente(client):
    """Id determinístico já usado num turno anterior: lê, não duplica."""

    async def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"error": {"reason": "duplicate"}})
        return httpx.Response(200, json={"id": "aaaaa11111", "status": "confirmed"})

    montar_transport(client, api)

    evento = await client.criar_evento(
        summary="x",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
        event_id="aaaaa11111",
    )

    assert evento == {"id": "aaaaa11111", "status": "confirmed"}


async def test_criar_evento_usa_o_event_id_fornecido(client):
    capturado = {}

    async def api(request: httpx.Request) -> httpx.Response:
        capturado["id"] = json.loads(request.content)["id"]
        return httpx.Response(200, json={"id": capturado["id"]})

    montar_transport(client, api)

    # "lead" + telefone: nada de `x`, `w`, `y` ou `z` — não são base32hex.
    # (a primeira versão deste teste usava um `x` no fim e o validador pegou,
    # que é exatamente o erro que ele existe para evitar num id determinístico)
    await client.criar_evento(
        summary="x",
        inicio=datetime(2026, 7, 30, 14, 0),
        fim=datetime(2026, 7, 30, 15, 0),
        event_id="lead5511987654321a",
    )

    assert capturado["id"] == "lead5511987654321a"


async def test_criar_evento_recusa_event_id_invalido_sem_tocar_a_rede(client):
    """Um `@` ou `+` morreria num 400 no meio do agendamento."""

    async def api(request: httpx.Request) -> httpx.Response:
        raise AssertionError("não deveria sair pela rede")

    montar_transport(client, api)

    with pytest.raises(ValueError, match="base32hex"):
        await client.criar_evento(
            summary="x",
            inicio=datetime(2026, 7, 30, 14, 0),
            fim=datetime(2026, 7, 30, 15, 0),
            event_id="lead+5511987654321@wa",
        )


async def test_falha_de_rede_na_leitura_vira_erro_tipado(client):
    """Nenhum `httpx.TimeoutException` cru escapa do cliente."""

    async def api(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns falhou")

    montar_transport(client, api)

    with pytest.raises(GoogleCalendarError) as exc:
        await client.listar_eventos(
            datetime(2026, 7, 28, 8, 0), datetime(2026, 7, 28, 22, 0)
        )

    assert exc.value.status_code == STATUS_SEM_RESPOSTA


# --- Ids de evento ------------------------------------------------------


def test_gerar_event_id_respeita_o_formato_do_google():
    ids = {gerar_event_id() for _ in range(100)}

    assert len(ids) == 100, "colisão em 100 ids"
    for gerado in ids:
        assert 5 <= len(gerado) <= 1024
        assert set(gerado) <= set(ALFABETO_EVENT_ID)


@pytest.mark.parametrize(
    "invalido",
    [
        "abc",  # curto demais
        "a" * 1025,  # longo demais
        "lead@exemplo",  # arroba fora do base32hex
        "lead+5511",  # mais
        "ABCDEF",  # maiúsculas fora do base32hex
        "wxyz12",  # w-z fora do base32hex
        "abc def",  # espaço
    ],
)
def test_validar_event_id_recusa_fora_do_base32hex(invalido):
    with pytest.raises(ValueError):
        validar_event_id(invalido)


@pytest.mark.parametrize("valido", ["abcde", "0123456789", "a" * 1024, "v0v0v"])
def test_validar_event_id_aceita_base32hex(valido):
    assert validar_event_id(valido) == valido


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


async def test_atualizar_evento_com_2xx_e_corpo_estranho_devolve_o_id(client):
    """A identidade veio de quem chamou — não há como perdê-la aqui."""

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})

    montar_transport(client, api)

    assert await client.atualizar_evento("ev-1", summary="novo") == {"id": "ev-1"}


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
