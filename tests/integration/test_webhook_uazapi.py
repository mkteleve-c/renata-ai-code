"""Webhook da uazapi: garantias que a Task 8 não pode ter mudado.

A Task 8 mexeu em `enqueue_or_buffer`, que é compartilhada pelos quatro
canais. A uazapi está em produção e não tinha nenhum teste no repositório,
então a suíte verde não dizia nada sobre ela. Este arquivo cobre
especificamente o que a mudança de `has_media` poderia ter quebrado.
"""

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from whatsapp_langchain.server.dependencies import request_history
from whatsapp_langchain.server.main import app
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "+5511955554444"
AGENTE = "illumi_assistant"
CHATID = "5511955554444@s.whatsapp.net"


def payload_uazapi(
    message_type: str,
    *,
    text: str = "",
    file_url: str | None = None,
    messageid: str = "UAZ-1",
) -> dict[str, Any]:
    mensagem: dict[str, Any] = {
        "messageid": messageid,
        "chatid": CHATID,
        "fromMe": False,
        "isGroup": False,
        "messageType": message_type,
        "text": text,
    }
    if file_url is not None:
        mensagem["fileURL"] = file_url

    return {
        "EventType": "messages",
        "instanceName": "instancia-teste",
        "token": "token-da-instancia",
        "message": mensagem,
    }


async def _apagar_rastro() -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "delete from message_queue where phone_number = %s", (TELEFONE,)
        )
    request_history.pop(TELEFONE, None)


@pytest.fixture
async def limpar():
    await _apagar_rastro()
    yield
    await _apagar_rastro()


async def cliente():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://teste")


async def unica_linha(colunas: str) -> Any:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"select {colunas} from message_queue where phone_number = %s "
            "order by id asc",
            (TELEFONE,),
        )
        linhas = await cur.fetchall()
    assert len(linhas) == 1, f"esperava 1 linha na fila, veio {len(linhas)}"
    return linhas[0]


async def test_texto_entra_na_fila_com_canal_uazapi(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/uazapi?agent={AGENTE}",
            json=payload_uazapi("conversation", text="Olá"),
        )

    assert r.status_code == 200
    assert r.json()["processed"] == 1

    linha = await unica_linha("channel, incoming_message, media_url, media_type")
    assert linha[0] == "uazapi"
    assert linha[1] == "Olá"
    assert linha[2] is None
    assert linha[3] is None


async def test_midia_com_file_url_entra_como_midia(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/uazapi?agent={AGENTE}",
            json=payload_uazapi(
                "imageMessage",
                text="olha",
                file_url="https://cdn.uazapi.com/foto.jpg",
                messageid="UAZ-IMG",
            ),
        )

    assert r.status_code == 200

    linha = await unica_linha("media_url, media_type, provider_message_key")
    assert linha[0] == "https://cdn.uazapi.com/foto.jpg"
    assert linha[1] == "image/jpeg"
    assert linha[2] is None, "provider_message_key é exclusiva da Evolution"


async def test_midia_sem_file_url_continua_no_branch_de_texto(limpar):
    """Payload reduzido da uazapi: media_type sem fileURL.

    `_extract_message_payload` devolve (text, None, media_type) nesse caso.
    A Task 8 passou a tratar `media_type` como sinal de mídia em
    `enqueue_or_buffer`, e sem cuidado isso mudaria de branch mensagens de um
    canal em produção. O sinal real é ter VIA DE DOWNLOAD (url ou
    provider_message_key), e a uazapi não manda key — então nada muda aqui.
    """
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/uazapi?agent={AGENTE}",
            json=payload_uazapi(
                "imageMessage", text="sem url", messageid="UAZ-SEM-URL"
            ),
        )

    assert r.status_code == 200

    linha = await unica_linha("incoming_message, media_url, media_type")
    assert linha[0] == "sem url"
    assert linha[1] is None, "sem via de download, não pode virar linha de mídia"
    assert linha[2] is None


async def test_from_me_nao_entra_na_fila(limpar):
    corpo = payload_uazapi("conversation", text="eco")
    corpo["message"]["fromMe"] = True

    async with await cliente() as c:
        r = await c.post(f"/webhook/uazapi?agent={AGENTE}", json=corpo)

    assert r.json()["processed"] == 0

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from message_queue where phone_number = %s", (TELEFONE,)
        )
        assert (await cur.fetchone())[0] == 0
