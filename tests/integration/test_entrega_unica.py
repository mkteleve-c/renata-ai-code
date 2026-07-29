"""Uma mensagem entregue não pode voltar para a fila.

Todo o resto do worker é desenhado para não mandar WhatsApp indevido. Este
arquivo cobre o modo oposto e mais barato de errar: mandar de novo o que já
foi mandado, porque um passo de contabilidade DEPOIS da entrega falhou.

O lead não vê a diferença entre "o sistema falhou" e "o sistema me mandou
tudo duas vezes" — mas a segunda é pior, porque parece que a Renata está
travada.
"""

import pytest

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.queue import (
    claim_next,
    enqueue_or_buffer,
    mark_done,
    mark_failed,
)

TELEFONE = "+551199990000"


@pytest.fixture
async def limpar():
    pool = await get_pool()

    async def limpa():
        async with pool.connection() as conn:
            await conn.execute(
                "delete from message_queue where phone_number = %s", (TELEFONE,)
            )

    await limpa()
    yield
    await limpa()


async def test_mark_failed_nao_ressuscita_mensagem_ja_entregue(limpar):
    """`mark_done` fica dentro do mesmo `try` que `upsert_conversation`.

    Se a contabilidade de conversa falhar (deadlock, statement timeout,
    pool esgotado) DEPOIS de a resposta já ter sido enviada e gravada, o
    `except` chama `mark_failed` — e sem guarda de status ele devolve para
    `queued` uma linha `done`. O worker reivindica de novo e o lead recebe
    a resposta inteira pela segunda vez, sem que nada tenha falhado no
    envio.
    """
    pool = await get_pool()
    resultado = await enqueue_or_buffer(
        pool,
        phone_number=TELEFONE,
        agent_id="elevec_sdr",
        body="oi",
        channel=MessagingChannel.EVOLUTION,
        buffer_seconds=0,
    )
    msg_id = resultado.message_id
    await claim_next(pool, lease_seconds=60)
    await mark_done(pool, msg_id, "resposta ja entregue ao lead")

    # A falha acontece DEPOIS da entrega — é o cenário inteiro.
    await mark_failed(pool, msg_id, "deadlock detected")

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select status, response from message_queue where id = %s", (msg_id,)
        )
        status, response = await cur.fetchone()

    assert status == "done", "mensagem entregue voltou para a fila"
    assert response == "resposta ja entregue ao lead", "a resposta foi perdida"

    reivindicada = await claim_next(pool, lease_seconds=60)
    assert reivindicada is None or reivindicada.id != msg_id, (
        "mensagem já entregue foi reivindicada de novo — o lead recebe duas vezes"
    )


async def test_mark_failed_continua_funcionando_para_falha_de_verdade(limpar):
    """Contraprova: a guarda não pode engolir o retry legítimo, que é o
    caso em que o lead NÃO recebeu nada."""
    pool = await get_pool()
    resultado = await enqueue_or_buffer(
        pool,
        phone_number=TELEFONE,
        agent_id="elevec_sdr",
        body="oi",
        channel=MessagingChannel.EVOLUTION,
        buffer_seconds=0,
    )
    msg_id = resultado.message_id
    await claim_next(pool, lease_seconds=60)

    await mark_failed(pool, msg_id, "Evolution 500")

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select status, error from message_queue where id = %s", (msg_id,)
        )
        status, error = await cur.fetchone()

    assert status == "queued", "falha real precisa voltar para a fila"
    assert error == "Evolution 500"
