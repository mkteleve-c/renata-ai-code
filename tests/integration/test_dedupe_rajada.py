"""Deduplicação de reentrega contra o banco real.

Rajada é o caso normal no WhatsApp — é a razão de o debounce existir. O
UPDATE do debounce não é INSERT, então nada do índice único da fila protege
a 2ª mensagem em diante: se o id absorvido não for gravado, a reentrega dela
volta a concatenar o mesmo texto na mesma linha.

Este arquivo também cobre o outro lado do mesmo predicado: linha de mídia da
Evolution tem `media_url` NULL e `provider_message_key` preenchida, e quando
volta para `queued` num retry ela não pode ser alvo do debounce de texto.
"""

import pytest
from psycopg.types.json import Jsonb

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.queue import enqueue_or_buffer

TELEFONE = "+551133332222"
OUTRO_TELEFONE = "+551133331111"
AGENTE = "illumi_assistant"


@pytest.fixture
async def limpar():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "delete from message_queue where phone_number in (%s, %s)",
            (TELEFONE, OUTRO_TELEFONE),
        )
    yield
    async with pool.connection() as conn:
        await conn.execute(
            "delete from message_queue where phone_number in (%s, %s)",
            (TELEFONE, OUTRO_TELEFONE),
        )


async def _enfileirar(body: str, message_id: str, phone: str = TELEFONE):
    pool = await get_pool()
    return await enqueue_or_buffer(
        pool,
        phone_number=phone,
        agent_id=AGENTE,
        body=body,
        channel=MessagingChannel.EVOLUTION,
        message_id=message_id,
        buffer_seconds=30.0,
    )


async def _texto_da_linha(queue_id: int) -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select incoming_message from message_queue where id = %s", (queue_id,)
        )
        (texto,) = await cur.fetchone()
    return texto


async def test_reentrega_de_mensagem_absorvida_nao_reconcatena(limpar):
    """Rajada de 3, a 2ª reentregue: o texto não pode aparecer duas vezes."""
    a = await _enfileirar("oi", "ID-A")
    b = await _enfileirar("tudo bem?", "ID-B")
    c = await _enfileirar("alô?", "ID-C")

    assert b.is_buffered is True
    assert b.message_id == a.message_id
    assert c.is_buffered is True

    reentrega = await _enfileirar("tudo bem?", "ID-B")

    assert reentrega.is_duplicate is True
    assert reentrega.message_id == a.message_id
    assert await _texto_da_linha(a.message_id) == "oi\ntudo bem?\nalô?"


async def test_reentrega_da_terceira_tambem_e_barrada(limpar):
    a = await _enfileirar("oi", "ID-A")
    await _enfileirar("tudo bem?", "ID-B")
    await _enfileirar("alô?", "ID-C")

    reentrega = await _enfileirar("alô?", "ID-C")

    assert reentrega.is_duplicate is True
    assert await _texto_da_linha(a.message_id) == "oi\ntudo bem?\nalô?"


async def test_mesmo_id_de_outro_lead_nao_e_duplicata(limpar):
    """Id do WhatsApp só é único por chat: dois leads podem repetir o id."""
    primeiro = await _enfileirar("oi", "ID-COMPARTILHADO", phone=TELEFONE)
    segundo = await _enfileirar("oi", "ID-COMPARTILHADO", phone=OUTRO_TELEFONE)

    assert segundo.is_duplicate is False
    assert segundo.message_id != primeiro.message_id


async def test_midia_em_retry_nao_recebe_debounce_de_texto(limpar):
    """Mídia da Evolution em retry tem media_url NULL e process_after futuro.

    Se o SELECT do debounce a encontrar, a legenda vira "legenda\\ntexto do
    lead" e a mensagem nova some dentro dela.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO message_queue
                (message_id, phone_number, agent_id, thread_id, incoming_message,
                 media_url, media_type, channel, provider_message_key,
                 status, process_after, attempts)
            VALUES (%s, %s, %s, %s, %s, NULL, %s, 'evolution', %s,
                    'queued', NOW() + interval '30 seconds', 1)
            RETURNING id
            """,
            (
                "ID-MIDIA",
                TELEFONE,
                AGENTE,
                f"{TELEFONE}:{AGENTE}",
                "legenda do audio",
                "audio/ogg",
                Jsonb({"id": "ID-MIDIA", "remoteJid": "5511@s.whatsapp.net"}),
            ),
        )
        (id_midia,) = await cur.fetchone()

    novo = await _enfileirar("texto novo do lead", "ID-TEXTO")

    assert novo.is_buffered is False, "texto novo não pode entrar na linha de mídia"
    assert novo.message_id != id_midia
    assert await _texto_da_linha(id_midia) == "legenda do audio"
