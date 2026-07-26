"""Webhook da Evolution: do payload até a fila."""

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from whatsapp_langchain.server.dependencies import request_history
from whatsapp_langchain.server.main import app
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "551166665555"
AGENTE = "illumi_assistant"
JID = "5511966665555@s.whatsapp.net"


def payload(
    texto="Olá",
    from_me=False,
    remote_jid=JID,
    message_id="MSG1",
):
    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": message_id},
            "pushName": "Fulano",
            "messageType": "conversation",
            "message": {"conversation": texto},
        },
    }


def payload_midia(remote_jid=JID, message_id="MSG-MEDIA", com_url=True):
    imagem: dict[str, Any] = {"caption": "Olha essa foto"}
    if com_url:
        imagem["url"] = "https://mmg.whatsapp.net/criptografado"

    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": False, "id": message_id},
            "pushName": "Fulano",
            "messageType": "imageMessage",
            "message": {"imageMessage": imagem},
        },
    }


def payload_tipo(message_type: str, message: dict[str, Any], message_id="MSG-TIPO"):
    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": JID, "fromMe": False, "id": message_id},
            "pushName": "Fulano",
            "messageType": message_type,
            "message": message,
        },
    }


async def _apagar_rastro() -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute(
            "delete from message_queue where phone_number = %s", (f"+{TELEFONE}",)
        )
    # request_history é módulo-level e sobrevive entre testes — sem zerar,
    # a soma dos POSTs deste arquivo estoura RATE_LIMIT_PER_HOUR (30).
    request_history.pop(f"+{TELEFONE}", None)


@pytest.fixture
async def limpar():
    """Zera lead, fila e rate limit do telefone de teste, antes e depois.

    O teardown importa: sem ele uma linha com message_id repetido sobrevive
    para o teste seguinte e a deduplicação por (channel, message_id) faz o
    próximo enqueue devolver `duplicata` em vez de enfileirar.
    """
    await _apagar_rastro()
    yield
    await _apagar_rastro()


async def cliente():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://teste")


async def linhas_na_fila(colunas: str = "*") -> list[Any]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"select {colunas} from message_queue where phone_number = %s "
            "order by id asc",
            (f"+{TELEFONE}",),
        )
        return await cur.fetchall()


async def unica_linha(colunas: str = "*") -> Any:
    linhas = await linhas_na_fila(colunas)
    assert len(linhas) == 1, f"esperava 1 linha na fila, veio {len(linhas)}"
    return linhas[0]


async def test_mensagem_valida_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("channel, incoming_message")
    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


async def test_from_me_nao_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload(from_me=True)
        )

    assert r.json()["status"] == "ignorado"
    assert r.json()["motivo"] == "from_me"
    assert await linhas_na_fila("id") == []


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

    linha = await unica_linha("channel, incoming_message")
    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


@pytest.mark.parametrize(
    "nome_do_evento", ["MESSAGES_UPSERT", "messages-upsert", "Messages.Upsert"]
)
async def test_variacao_do_nome_do_evento_e_aceita(limpar, nome_do_evento):
    body = payload()
    body["event"] = nome_do_evento

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert len(await linhas_na_fila("id")) == 1


async def test_mensagem_com_midia_grava_provider_message_key(limpar):
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload_midia())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("provider_message_key, media_url, media_type")
    assert linha[0] == {
        "remoteJid": JID,
        "fromMe": False,
        "id": "MSG-MEDIA",
    }
    assert linha[1] == "https://mmg.whatsapp.net/criptografado"
    assert linha[2] == "image/jpeg"


async def test_reacao_nao_invoca_o_agente(limpar):
    corpo = payload_tipo(
        "reactionMessage",
        {"reactionMessage": {"text": "👍", "key": {"id": "MSG1"}}},
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json() == {"status": "ignorado", "motivo": "conteudo_nao_suportado"}
    assert await linhas_na_fila("id") == []


async def test_mensagem_apagada_nao_invoca_o_agente(limpar):
    corpo = payload_tipo(
        "protocolMessage",
        {"protocolMessage": {"type": "REVOKE", "key": {"id": "MSG1"}}},
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.json()["motivo"] == "conteudo_nao_suportado"
    assert await linhas_na_fila("id") == []


async def test_sticker_entra_como_midia_com_url(limpar):
    corpo = payload_tipo(
        "stickerMessage",
        {"stickerMessage": {"url": "https://mmg.whatsapp.net/sticker"}},
        message_id="MSG-STICKER",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("media_url, media_type, incoming_message")
    assert linha[0] == "https://mmg.whatsapp.net/sticker"
    assert linha[1] == "image/webp"
    assert linha[2] == ""


async def test_midia_sem_url_e_sem_legenda_e_ignorada(limpar):
    corpo = payload_tipo(
        "audioMessage",
        {"audioMessage": {"seconds": 3}},
        message_id="MSG-AUDIO",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["motivo"] == "conteudo_nao_suportado"
    assert await linhas_na_fila("id") == []


async def test_mensagem_sem_campo_message_e_ignorada(limpar):
    corpo = payload()
    del corpo["data"]["message"]

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["motivo"] == "conteudo_nao_suportado"
    assert await linhas_na_fila("id") == []


async def test_midia_dentro_de_envelope_view_once(limpar):
    corpo = payload_tipo(
        "viewOnceMessageV2",
        {
            "viewOnceMessageV2": {
                "message": {
                    "imageMessage": {
                        "url": "https://mmg.whatsapp.net/vo",
                        "caption": "some depois",
                    }
                }
            }
        },
        message_id="MSG-VO",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("media_url, media_type, incoming_message")
    assert linha[0] == "https://mmg.whatsapp.net/vo"
    assert linha[1] == "image/jpeg"
    assert linha[2] == "some depois"


async def test_data_como_lista_e_processada(limpar):
    corpo = payload()
    corpo["data"] = [corpo["data"]]

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("channel, incoming_message")
    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


async def test_payload_que_nao_e_objeto_nao_da_500(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=["nao", "e", "isso"]
        )

    assert r.status_code == 200
    assert r.json()["motivo"] == "payload_invalido"


async def test_reentrega_de_texto_nao_duplica_a_fila(limpar):
    async with await cliente() as c:
        primeira = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())
        segunda = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert primeira.json()["status"] == "ok"
    assert segunda.status_code == 200
    assert segunda.json()["motivo"] == "duplicata"
    assert segunda.json()["queue_id"] == primeira.json()["queue_id"]

    # Sem dedupe, a reentrega dentro da janela de debounce concatenaria o
    # mesmo texto na mesma linha.
    linha = await unica_linha("incoming_message")
    assert linha[0] == "Olá"


async def test_reentrega_de_midia_nao_duplica_a_fila(limpar):
    async with await cliente() as c:
        primeira = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload_midia()
        )
        segunda = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload_midia()
        )

    assert primeira.json()["status"] == "ok"
    assert segunda.json()["motivo"] == "duplicata"
    assert len(await linhas_na_fila("id")) == 1


async def test_secret_configurado_rejeita_header_errado(limpar, monkeypatch):
    monkeypatch.setattr(settings, "evolution_webhook_secret", "segredo-forte")

    async with await cliente() as c:
        errado = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(),
            headers={"X-Evolution-Webhook-Secret": "chute"},
        )
        ausente = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())
        certo = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(),
            headers={"X-Evolution-Webhook-Secret": "segredo-forte"},
        )

    assert errado.status_code == 401
    assert ausente.status_code == 401
    assert certo.status_code == 200
    assert certo.json()["status"] == "ok"
    assert len(await linhas_na_fila("id")) == 1


async def test_rate_limit_nao_deixa_o_gate_escrever(limpar, monkeypatch):
    """Mensagem barrada não pode contar como engajamento do lead.

    O gate renova last_interaction_at e zera followup_count. Se rodasse
    antes do rate limit, o lead ficaria "engajado" por uma mensagem que
    nunca chegou ao agente.
    """
    monkeypatch.setattr(settings, "rate_limit_per_hour", 0)

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    # 200 de propósito: 429 faria a Evolution reentregar em loop.
    assert r.status_code == 200
    assert r.json() == {"status": "ignorado", "motivo": "rate_limit"}
    assert await linhas_na_fila("id") == []

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (TELEFONE,)
        )
        assert (await cur.fetchone())[0] == 0


async def test_rate_limit_nao_barra_handover_do_atendente(limpar, monkeypatch):
    """fromMe precisa chegar ao gate mesmo com o limite estourado."""
    async with await cliente() as c:
        await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

        monkeypatch.setattr(settings, "rate_limit_per_hour", 0)
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(from_me=True, message_id="MSG-HUMANO"),
        )

    assert r.json()["motivo"] == "from_me"

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active from leads_crm where phone = %s", (TELEFONE,)
        )
        linha = await cur.fetchone()

    assert linha is not None, "o lead deveria existir"
    assert linha[0] is False, "o agente deveria ter sido desligado pelo handover"


async def test_sem_secret_configurado_a_rota_fica_aberta(limpar):
    assert settings.evolution_webhook_secret == ""

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
