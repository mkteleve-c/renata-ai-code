"""O mapeamento coluna→campo do claim_next, exercitado contra o banco real.

`claim_next` monta o MessageQueue por índice posicional (`row[0]`, `row[1]`…)
sobre o RETURNING. Uma coluna adicionada, removida ou **reordenada** no SQL
sem o ajuste correspondente dos índices desloca todos os campos seguintes em
silêncio — foi exatamente assim que `provider_message_key` chegava None ao
worker.

Um teste com tupla montada à mão não pega isso: ele repete a suposição do
código em vez de verificá-la. Aqui a tupla vem do Postgres, e cada coluna
recebe um valor sentinela distinto, então qualquer troca de posição quebra
uma asserção.
"""

from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.models import MessageStatus, MessagingChannel
from whatsapp_langchain.shared.queue import claim_next, contar_pendentes_por_canal

KEY_SENTINELA = {"id": "KEY-SENTINELA", "remoteJid": "5511@s.whatsapp.net"}

# Valores únicos por coluna: se dois campos trocarem de lugar, o valor não bate.
SENTINELAS = {
    "message_id": "MSGID-SENTINELA",
    "phone_number": "+5511900000001",
    "to_number": "+5511900000002",
    "agent_id": "AGENTE-SENTINELA",
    "thread_id": "THREAD-SENTINELA",
    "incoming_message": "ENTRADA-SENTINELA",
    "media_url": "https://sentinela.exemplo/x.jpg",
    "media_type": "image/sentinela",
    "normalized_input": "NORMALIZADO-SENTINELA",
    "media_processing_status": "STATUS-MIDIA-SENTINELA",
    "media_processing_error": "ERRO-MIDIA-SENTINELA",
    "response": "RESPOSTA-SENTINELA",
    "error": "ERRO-SENTINELA",
    "outbound_token": "TOKEN-SENTINELA",
}

INSERT = """
INSERT INTO message_queue (
    message_id, phone_number, to_number, agent_id, thread_id,
    incoming_message, media_url, media_type,
    normalized_input, media_processing_status, media_processing_error,
    status, process_after, attempts, max_attempts, lease_until,
    response, error, outbound_token, channel, provider_message_key,
    created_at, updated_at, processed_at
) VALUES (
    %(message_id)s, %(phone_number)s, %(to_number)s, %(agent_id)s, %(thread_id)s,
    %(incoming_message)s, %(media_url)s, %(media_type)s,
    %(normalized_input)s, %(media_processing_status)s, %(media_processing_error)s,
    'queued', TIMESTAMPTZ '1999-01-01 00:00:00+00', 1, 7, NULL,
    %(response)s, %(error)s, %(outbound_token)s, 'evolution', %(key)s,
    TIMESTAMPTZ '1999-01-01 00:00:00+00',
    TIMESTAMPTZ '1999-01-01 00:00:00+00',
    TIMESTAMPTZ '2001-02-03 04:05:06+00'
)
RETURNING id
"""


async def _inserir_sentinela(pool) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute(INSERT, {**SENTINELAS, "key": Jsonb(KEY_SENTINELA)})
        linha = await cursor.fetchone()
        await conn.commit()
    return int(linha[0])


async def _remover(pool, message_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM message_queue WHERE id = %s", (message_id,))
        await conn.commit()


@pytest.mark.asyncio
async def test_claim_next_mapeia_todas_as_colunas():
    """Cada campo do MessageQueue bate com a coluna correspondente do SQL."""
    pool = await get_pool()
    inserido = await _inserir_sentinela(pool)

    try:
        message = await claim_next(pool, lease_seconds=60)

        assert message is not None, "nenhuma mensagem reclamada"
        # created_at em 1999 põe a linha no topo do ORDER BY created_at ASC.
        assert message.id == inserido, (
            "outra linha foi reclamada antes da sentinela — há mensagem "
            "pendente mais antiga no banco, rode com a fila limpa"
        )

        for campo, esperado in SENTINELAS.items():
            assert getattr(message, campo) == esperado, f"campo {campo} deslocado"

        assert message.channel is MessagingChannel.EVOLUTION
        assert message.provider_message_key == KEY_SENTINELA

        # Colunas escritas pelo próprio claim.
        assert message.status is MessageStatus.PROCESSING
        assert message.attempts == 2, "attempts deve vir incrementado"
        assert message.max_attempts == 7
        assert message.lease_until is not None
        assert message.lease_until > datetime.now(UTC)

        # Timestamps com anos distintos: um swap entre eles quebra aqui.
        assert message.created_at.year == 1999
        assert message.processed_at is not None
        assert message.processed_at.year == 2001
        assert message.updated_at.year != 1999, (
            "updated_at deve ser reescrito por NOW()"
        )
    finally:
        await _remover(pool, inserido)


@pytest.mark.asyncio
async def test_contar_pendentes_por_canal_ve_a_linha():
    """Base do aviso de boot: fila por canal sem cliente outbound."""
    pool = await get_pool()
    antes = await contar_pendentes_por_canal(pool)
    inserido = await _inserir_sentinela(pool)

    try:
        depois = await contar_pendentes_por_canal(pool)
        assert depois.get("evolution", 0) == antes.get("evolution", 0) + 1
    finally:
        await _remover(pool, inserido)
