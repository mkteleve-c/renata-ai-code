"""Operações de fila no PostgreSQL.

Módulo compartilhado entre API e Worker para manipular a tabela message_queue.
A API insere mensagens (enqueue), o Worker consome (claim) e
finaliza (mark_done/failed).

O debounce agrupa mensagens rápidas do mesmo remetente: se o usuário
envia 3 mensagens em 2 segundos, elas são concatenadas em uma única
entrada na fila.

Uso:
    from whatsapp_langchain.shared.queue import enqueue_or_buffer

    result = await enqueue_or_buffer(pool, phone="+55...", body="Olá")
    message = await claim_next(pool, lease_seconds=60)
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.models import (
    EnqueueResult,
    MessageQueue,
    MessagingChannel,
)

logger = structlog.get_logger()


# Alvo explícito do ON CONFLICT: sem ele, o DO NOTHING engoliria em silêncio
# qualquer unique constraint que a tabela venha a ganhar. Repete o predicado
# do índice parcial da migração 009 para que o Postgres consiga inferi-lo.
ALVO_DE_CONFLITO = (
    "ON CONFLICT (channel, agent_id, message_id) "
    "WHERE message_id IS NOT NULL DO NOTHING"
)


async def _buscar_por_message_id(
    conn: AsyncConnection[Any],
    channel_value: str,
    agent_id: str,
    message_id: str | None,
) -> int | None:
    """Id da linha já enfileirada para esse (canal, agente, message_id).

    `agent_id` faz parte da chave porque o mesmo payload entregue em
    `?agent=a` e `?agent=b` são duas mensagens legítimas — sem ele, a
    segunda seria tratada como reentrega da primeira.
    """
    if not message_id:
        return None

    cursor = await conn.execute(
        "SELECT id FROM message_queue "
        "WHERE channel = %s AND agent_id = %s AND message_id = %s LIMIT 1",
        (channel_value, agent_id, message_id),
    )
    row = await cursor.fetchone()
    return row[0] if row is not None else None


async def enqueue_or_buffer(
    pool: AsyncConnectionPool,
    phone_number: str,
    agent_id: str,
    body: str,
    channel: MessagingChannel | str = MessagingChannel.TWILIO,
    media_url: str | None = None,
    media_type: str | None = None,
    to_number: str | None = None,
    message_id: str | None = None,
    buffer_seconds: float = 2.0,
    outbound_token: str | None = None,
    provider_message_key: dict[str, Any] | None = None,
) -> EnqueueResult:
    """Insere mensagem na fila ou agrupa com mensagem pendente (debounce).

    Regras de debounce (Fase 3):
    - Debounce somente para texto (sem via de download de mídia).
    - Mensagem com mídia não faz debounce (entrada imediata). Conta como
      mídia quem tem `media_url` OU `media_type` + `provider_message_key` —
      a Evolution baixa pela key e não depende da URL.
    - Antes de inserir mídia, flush de texto pendente do mesmo phone+agent
      para que o worker processe o texto ANTES da mídia (ordenação por created_at).
    - Concorrência protegida por pg_advisory_xact_lock(hash(phone+agent)).

    Idempotência: quando `message_id` vem preenchido, uma reentrega do mesmo
    id no mesmo canal e agente não vira linha nova nem é concatenada por
    debounce — devolve o id da linha original com `is_duplicate=True`.
    Provedores reentregam o webhook em timeout ou resposta >= 400; sem isso o
    lead receberia a resposta duas vezes. O mesmo payload em `?agent=a` e
    `?agent=b` continua gerando duas linhas, que é o comportamento correto do
    template. O índice único parcial da migração 009 é a garantia final
    contra a corrida entre dois workers da API.

    Limitação conhecida: NumMedia > 1 no mesmo webhook fica fora do escopo.

    Args:
        pool: Pool de conexões do psycopg.
        phone_number: Telefone do remetente (E.164).
        agent_id: ID do agente que vai processar.
        body: Texto da mensagem.
        media_url: URL de mídia anexada (opcional).
        media_type: MIME type da mídia (opcional).
        to_number: Número destinatário (opcional).
        message_id: ID externo da mensagem, ex: Twilio MessageSid (opcional).
        buffer_seconds: Segundos de debounce. Default: 2.0.
        provider_message_key: Key completa da mensagem no provedor (ex: data.key
            da Evolution), necessária quando o download de mídia exige mais que
            o id. Vazia para os demais canais.

    Returns:
        EnqueueResult com message_id, se foi buffered e se era duplicata.
    """
    thread_id = f"{phone_number}:{agent_id}"
    # É mídia quando existe ALGUMA via de buscar os bytes: uma URL, ou uma
    # `provider_message_key` (a Evolution baixa por ela e a URL do payload
    # aponta para conteúdo cifrado, inútil para GET). Sem nenhuma das duas,
    # `media_type` sozinho é sucata — a uazapi manda isso no "payload
    # reduzido" e essas mensagens seguem no branch de texto, como sempre
    # seguiram. Mídia sem via de download que caísse no branch de mídia
    # gravaria uma linha que o worker nunca conseguiria resolver.
    has_media = media_url is not None or (
        media_type is not None and provider_message_key is not None
    )
    # "" é ausência de id disfarçada (a uazapi manda string vazia quando o
    # payload não traz messageid) e não pode participar da deduplicação.
    message_id = message_id or None
    channel_value = (
        channel.value if isinstance(channel, MessagingChannel) else str(channel)
    )

    # Hash determinístico para pg_advisory_xact_lock.
    # Usa os 8 bytes iniciais do SHA-256 convertidos para int64 signed,
    # garantindo chave única por phone+agent+channel sem risco de colisão
    # prática. Incluir o canal evita que mensagens vindas em paralelo de
    # canais distintos (ex.: mesmo phone em Twilio e uazapi) bloqueiem-se
    # mutuamente — cada combinação tem seu lock próprio.
    lock_key = int.from_bytes(
        hashlib.sha256(f"{thread_id}:{channel_value}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )

    async with pool.connection() as conn:
        # Lock transacional: serializa debounce para o mesmo phone+agent.
        # Liberado automaticamente no commit/rollback da transação.
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))

        # Reentrega do provedor: o id já virou linha. Precisa ser checado
        # aqui, antes do branch de debounce — o caminho de debounce faz
        # UPDATE, não INSERT, e escaparia do índice único da migração 009,
        # concatenando o mesmo texto duas vezes na mesma linha.
        duplicada = await _buscar_por_message_id(
            conn, channel_value, agent_id, message_id
        )
        if duplicada is not None:
            await conn.commit()
            logger.info(
                "message_duplicate_ignored",
                message_id=duplicada,
                provider_message_id=message_id,
                phone=phone_number,
                agent_id=agent_id,
                channel=channel_value,
            )
            return EnqueueResult(
                message_id=duplicada, is_buffered=False, is_duplicate=True
            )

        if has_media:
            # Mídia: flush texto pendente e inserir imediatamente.
            # O flush antecipa o process_after de textos aguardando debounce,
            # garantindo que o worker os processe antes da mídia (via created_at).
            # Filtro por channel: textos de outros canais não são afetados.
            flushed = await conn.execute(
                """
                UPDATE message_queue
                SET process_after = NOW(),
                    updated_at = NOW()
                WHERE phone_number = %s
                  AND agent_id = %s
                  AND channel = %s
                  AND status = 'queued'
                  AND process_after > NOW()
                  AND media_url IS NULL
                """,
                (phone_number, agent_id, channel_value),
            )
            if flushed.rowcount and flushed.rowcount > 0:
                logger.info(
                    "text_flushed_for_media",
                    phone=phone_number,
                    agent_id=agent_id,
                    channel=channel_value,
                    flushed_count=flushed.rowcount,
                )

            # Inserir mídia com process_after=NOW() (sem buffer)
            cursor = await conn.execute(
                f"""
                INSERT INTO message_queue
                    (message_id, phone_number, to_number, agent_id,
                     thread_id, incoming_message, media_url, media_type,
                     outbound_token, channel, provider_message_key, process_after)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                {ALVO_DE_CONFLITO}
                RETURNING id
                """,
                (
                    message_id,
                    phone_number,
                    to_number,
                    agent_id,
                    thread_id,
                    body,
                    media_url,
                    media_type,
                    outbound_token,
                    channel_value,
                    Jsonb(provider_message_key)
                    if provider_message_key is not None
                    else None,
                ),
            )
            row = await cursor.fetchone()

            if row is None:
                # Corrida perdida com outra requisição do mesmo message_id:
                # o índice único suprimiu o INSERT. A linha vencedora é a
                # resposta correta.
                duplicada = await _buscar_por_message_id(
                    conn, channel_value, agent_id, message_id
                )
                await conn.commit()
                if duplicada is None:
                    raise RuntimeError(
                        "INSERT de mídia suprimido sem linha correspondente "
                        f"para message_id={message_id!r}"
                    )
                logger.info(
                    "message_duplicate_ignored",
                    message_id=duplicada,
                    provider_message_id=message_id,
                    phone=phone_number,
                    agent_id=agent_id,
                    channel=channel_value,
                )
                return EnqueueResult(
                    message_id=duplicada, is_buffered=False, is_duplicate=True
                )

            new_id = row[0]
            await conn.commit()

            logger.info(
                "media_message_enqueued",
                message_id=new_id,
                phone=phone_number,
                agent_id=agent_id,
                channel=channel_value,
            )
            return EnqueueResult(message_id=new_id, is_buffered=False)

        # Texto: debounce normal (agrupa com texto pendente se existir).
        # Filtro por channel: mensagens do mesmo phone+agent vindas em
        # canais diferentes não são agrupadas — cada canal tem identidade
        # outbound própria.
        process_after = datetime.now(UTC) + timedelta(seconds=buffer_seconds)

        # Busca texto pendente para debounce (media_url IS NULL garante
        # que não debounce texto dentro de uma mensagem de mídia)
        cursor = await conn.execute(
            """
            SELECT id, incoming_message
            FROM message_queue
            WHERE phone_number = %s
              AND agent_id = %s
              AND channel = %s
              AND status = 'queued'
              AND process_after > NOW()
              AND media_url IS NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (phone_number, agent_id, channel_value),
        )
        existing = await cursor.fetchone()

        if existing:
            # Debounce: concatena texto e reseta timer
            existing_id, existing_body = existing
            new_body = f"{existing_body}\n{body}"

            # Atualiza o outbound_token apenas se vier preenchido — token novo
            # do mesmo phone+agent geralmente reflete a instância ativa atual.
            await conn.execute(
                """
                UPDATE message_queue
                SET incoming_message = %s,
                    process_after = %s,
                    outbound_token = COALESCE(%s, outbound_token),
                    provider_message_key = COALESCE(
                        %s, provider_message_key
                    ),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    new_body,
                    process_after,
                    outbound_token,
                    Jsonb(provider_message_key)
                    if provider_message_key is not None
                    else None,
                    existing_id,
                ),
            )
            await conn.commit()

            logger.info(
                "message_buffered",
                message_id=existing_id,
                phone=phone_number,
                agent_id=agent_id,
                channel=channel_value,
            )
            return EnqueueResult(message_id=existing_id, is_buffered=True)

        # Nova mensagem de texto na fila
        cursor = await conn.execute(
            f"""
            INSERT INTO message_queue
                (message_id, phone_number, to_number, agent_id, thread_id,
                 incoming_message, media_url, media_type, outbound_token,
                 channel, provider_message_key, process_after)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            {ALVO_DE_CONFLITO}
            RETURNING id
            """,
            (
                message_id,
                phone_number,
                to_number,
                agent_id,
                thread_id,
                body,
                None,
                None,
                outbound_token,
                channel_value,
                Jsonb(provider_message_key)
                if provider_message_key is not None
                else None,
                process_after,
            ),
        )
        row = await cursor.fetchone()

        if row is None:
            duplicada = await _buscar_por_message_id(
                conn, channel_value, agent_id, message_id
            )
            await conn.commit()
            if duplicada is None:
                raise RuntimeError(
                    "INSERT de texto suprimido sem linha correspondente "
                    f"para message_id={message_id!r}"
                )
            logger.info(
                "message_duplicate_ignored",
                message_id=duplicada,
                provider_message_id=message_id,
                phone=phone_number,
                agent_id=agent_id,
                channel=channel_value,
            )
            return EnqueueResult(
                message_id=duplicada, is_buffered=False, is_duplicate=True
            )

        new_id = row[0]
        await conn.commit()

        logger.info(
            "message_enqueued",
            message_id=new_id,
            phone=phone_number,
            agent_id=agent_id,
            channel=channel_value,
        )
        return EnqueueResult(message_id=new_id, is_buffered=False)


async def claim_next(
    pool: AsyncConnectionPool,
    lease_seconds: int = 60,
) -> MessageQueue | None:
    """Busca e reserva a próxima mensagem pronta para processamento.

    Usa FOR UPDATE SKIP LOCKED para concorrência segura entre múltiplos workers.
    Só retorna mensagens com process_after <= NOW() (debounce concluído) e
    dentro do limite de tentativas.

    Args:
        pool: Pool de conexões do psycopg.
        lease_seconds: Segundos de lock para o worker processar.

    Returns:
        MessageQueue se houver mensagem disponível, None caso contrário.
    """
    lease_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)

    async with pool.connection() as conn:
        # Evita mensagens presas eternamente em processing após crash:
        # se o lease expirou e não há mais tentativas, marca como failed.
        await conn.execute(
            """
            UPDATE message_queue
            SET status = 'failed',
                error = COALESCE(
                    error,
                    'Processing lease expired after max attempts'
                ),
                processed_at = NOW(),
                updated_at = NOW()
            WHERE status = 'processing'
              AND lease_until IS NOT NULL
              AND lease_until <= NOW()
              AND attempts >= max_attempts
            """
        )

        cursor = await conn.execute(
            """
            UPDATE message_queue
            SET status = 'processing',
                lease_until = %s,
                attempts = attempts + 1,
                updated_at = NOW()
            WHERE id = (
                SELECT id FROM message_queue
                WHERE (
                    status = 'queued'
                    AND process_after <= NOW()
                    AND attempts < max_attempts
                )
                OR (
                    status = 'processing'
                    AND lease_until IS NOT NULL
                    AND lease_until <= NOW()
                    AND attempts < max_attempts
                )
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, message_id, phone_number, to_number, agent_id, thread_id,
                      incoming_message, media_url, media_type,
                      normalized_input, media_processing_status, media_processing_error,
                      status,
                      process_after, attempts, max_attempts, lease_until,
                      response, error, outbound_token, channel,
                      created_at, updated_at, processed_at
            """,
            (lease_until,),
        )
        row = await cursor.fetchone()
        await conn.commit()

        if row is None:
            return None

        message = MessageQueue(
            id=row[0],
            message_id=row[1],
            phone_number=row[2],
            to_number=row[3],
            agent_id=row[4],
            thread_id=row[5],
            incoming_message=row[6],
            media_url=row[7],
            media_type=row[8],
            normalized_input=row[9],
            media_processing_status=row[10],
            media_processing_error=row[11],
            status=row[12],
            process_after=row[13],
            attempts=row[14],
            max_attempts=row[15],
            lease_until=row[16],
            response=row[17],
            error=row[18],
            outbound_token=row[19],
            channel=row[20],
            created_at=row[21],
            updated_at=row[22],
            processed_at=row[23],
        )

        logger.info(
            "message_claimed",
            message_id=message.id,
            phone=message.phone_number,
            agent_id=message.agent_id,
            channel=message.channel.value,
            attempt=message.attempts,
        )
        return message


async def mark_done(
    pool: AsyncConnectionPool,
    message_id: int,
    response: str,
    normalized_input: str | None = None,
    media_processing_status: str | None = None,
    media_processing_error: str | None = None,
) -> None:
    """Marca mensagem como processada com sucesso.

    Args:
        pool: Pool de conexões do psycopg.
        message_id: ID da mensagem na fila.
        response: Resposta gerada pelo agente.
        normalized_input: Texto normalizado enviado ao agente.
        media_processing_status: Resultado do pré-processamento de mídia.
        media_processing_error: Erro do pré-processamento de mídia, se houver.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE message_queue
            SET status = 'done',
                response = %s,
                normalized_input = COALESCE(%s, normalized_input),
                media_processing_status = COALESCE(%s, media_processing_status),
                media_processing_error = COALESCE(%s, media_processing_error),
                processed_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                response,
                normalized_input,
                media_processing_status,
                media_processing_error,
                message_id,
            ),
        )
        await conn.commit()

    logger.info("message_done", message_id=message_id)


async def mark_failed(
    pool: AsyncConnectionPool,
    message_id: int,
    error: str,
) -> None:
    """Marca mensagem como falha.

    Se ainda tem tentativas restantes, volta para 'queued' para retry.
    Caso contrário, marca como 'failed' definitivamente.

    Args:
        pool: Pool de conexões do psycopg.
        message_id: ID da mensagem na fila.
        error: Descrição do erro.
    """
    async with pool.connection() as conn:
        # Verifica se ainda tem tentativas
        cursor = await conn.execute(
            "SELECT attempts, max_attempts FROM message_queue WHERE id = %s",
            (message_id,),
        )
        row = await cursor.fetchone()

        if row and row[0] < row[1]:
            # Ainda tem tentativas: volta para a fila com backoff progressivo
            # Cada tentativa espera attempts * 5s antes de ser reprocessada
            backoff_seconds = row[0] * 5
            next_retry_at = datetime.now(UTC) + timedelta(seconds=backoff_seconds)
            await conn.execute(
                """
                UPDATE message_queue
                SET status = 'queued',
                    error = %s,
                    lease_until = NULL,
                    process_after = NOW() + make_interval(secs => %s),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (error, backoff_seconds, message_id),
            )
            logger.warning(
                "message_retry",
                message_id=message_id,
                attempt=row[0],
                max_attempts=row[1],
                backoff_seconds=backoff_seconds,
                next_retry_at=next_retry_at.isoformat(),
                error=error,
            )
        else:
            # Sem tentativas: falha definitiva
            await conn.execute(
                """
                UPDATE message_queue
                SET status = 'failed',
                    error = %s,
                    processed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (error, message_id),
            )
            logger.error(
                "message_failed",
                message_id=message_id,
                error=error,
            )

        await conn.commit()


async def upsert_conversation(
    pool: AsyncConnectionPool,
    phone_number: str,
    agent_id: str,
    last_message: str,
) -> None:
    """Atualiza ou cria registro de conversa.

    Usado após cada mensagem processada para manter o histórico
    de conversas atualizado (para o painel admin).

    Args:
        pool: Pool de conexões do psycopg.
        phone_number: Telefone do remetente.
        agent_id: ID do agente.
        last_message: Última mensagem processada.
    """
    thread_id = f"{phone_number}:{agent_id}"

    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO conversations (
                phone_number, agent_id, thread_id,
                last_message, last_message_at, message_count)
            VALUES (%s, %s, %s, %s, NOW(), 1)
            ON CONFLICT (phone_number, agent_id) DO UPDATE SET
                last_message = EXCLUDED.last_message,
                last_message_at = NOW(),
                message_count = conversations.message_count + 1,
                updated_at = NOW()
            """,
            (phone_number, agent_id, thread_id, last_message),
        )
        await conn.commit()
