"""Webhook da Evolution: do payload até a fila."""

import pytest
from httpx import ASGITransport, AsyncClient

from whatsapp_langchain.server.main import app
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "551166665555"
AGENTE = "illumi_assistant"


def payload(texto="Olá", from_me=False, remote_jid="5511966665555@s.whatsapp.net"):
    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": "MSG1"},
            "pushName": "Fulano",
            "messageType": "conversation",
            "message": {"conversation": texto},
        },
    }


def payload_midia(remote_jid="5511966665555@s.whatsapp.net"):
    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": False, "id": "MSG-MEDIA"},
            "pushName": "Fulano",
            "messageType": "imageMessage",
            "message": {
                "imageMessage": {
                    "url": "https://mmg.whatsapp.net/criptografado",
                    "caption": "Olha essa foto",
                }
            },
        },
    }


@pytest.fixture
async def limpar():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute(
            "delete from message_queue where phone_number = %s", (f"+{TELEFONE}",)
        )
    yield


async def cliente():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://teste")


async def test_mensagem_valida_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select channel, incoming_message from message_queue "
            "where phone_number = %s",
            (f"+{TELEFONE}",),
        )
        linha = await cur.fetchone()

    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


async def test_from_me_nao_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload(from_me=True)
        )

    assert r.json()["status"] == "ignorado"
    assert r.json()["motivo"] == "from_me"

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from message_queue where phone_number = %s",
            (f"+{TELEFONE}",),
        )
        assert (await cur.fetchone())[0] == 0


async def test_grupo_e_ignorado(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(remote_jid="12345-67890@g.us"),
        )

    assert r.json()["status"] == "ignorado"
    assert r.json()["motivo"] == "telefone_invalido"


async def test_evento_nao_mensagem_e_ignorado(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json={"event": "connection.update", "instance": "x", "data": {}},
        )

    assert r.status_code == 200
    assert r.json()["status"] == "ignorado"


async def test_agente_inexistente_da_erro():
    async with await cliente() as c:
        r = await c.post("/webhook/evolution?agent=nao_existe", json=payload())

    assert r.status_code == 400


async def test_evento_messages_sem_sufixo_e_aceito(limpar):
    body = payload()
    body["event"] = "messages"

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=body)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_mensagem_com_midia_grava_provider_message_key(limpar):
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload_midia())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select provider_message_key, media_url, media_type "
            "from message_queue where phone_number = %s",
            (f"+{TELEFONE}",),
        )
        linha = await cur.fetchone()

    assert linha[0] == {
        "remoteJid": "5511966665555@s.whatsapp.net",
        "fromMe": False,
        "id": "MSG-MEDIA",
    }
    assert linha[1] == "https://mmg.whatsapp.net/criptografado"
    assert linha[2] == "image/jpeg"
