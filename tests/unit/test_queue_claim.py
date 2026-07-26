"""Testes de claim da fila com recuperação de lease expirado."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from whatsapp_langchain.shared.queue import claim_next


class TestClaimNextLeaseRecovery:
    """Garante que mensagens não ficam presas em status processing."""

    @pytest.fixture
    def mock_pool(self):
        conn = AsyncMock()
        pool = AsyncMock()

        @asynccontextmanager
        async def fake_connection():
            yield conn

        pool.connection = fake_connection
        return pool, conn

    async def test_marks_expired_processing_as_failed_when_max_attempts_reached(
        self, mock_pool
    ):
        """Lease expirado sem retries restantes deve virar failed."""
        pool, conn = mock_pool

        stale_cursor = AsyncMock()
        claim_cursor = AsyncMock()
        claim_cursor.fetchone = AsyncMock(return_value=None)
        conn.execute = AsyncMock(side_effect=[stale_cursor, claim_cursor])

        result = await claim_next(pool, lease_seconds=60)

        assert result is None
        calls = conn.execute.call_args_list
        assert len(calls) == 2

        stale_sql = calls[0][0][0]
        assert "SET status = 'failed'" in stale_sql
        assert "status = 'processing'" in stale_sql
        assert "lease_until <= NOW()" in stale_sql
        assert "attempts >= max_attempts" in stale_sql

    async def test_reclaims_expired_processing_when_attempts_remain(self, mock_pool):
        """Claim deve considerar processing com lease expirado para retry."""
        pool, conn = mock_pool

        stale_cursor = AsyncMock()
        claim_cursor = AsyncMock()
        claim_cursor.fetchone = AsyncMock(return_value=None)
        conn.execute = AsyncMock(side_effect=[stale_cursor, claim_cursor])

        result = await claim_next(pool, lease_seconds=60)

        assert result is None
        calls = conn.execute.call_args_list
        assert len(calls) == 2

        claim_sql = calls[1][0][0]
        assert "status = 'queued'" in claim_sql
        assert "status = 'processing'" in claim_sql
        assert "lease_until <= NOW()" in claim_sql
        assert "attempts < max_attempts" in claim_sql


class TestClaimNextProviderMessageKey:
    """A key do provedor precisa chegar ao worker para o download de mídia."""

    @pytest.fixture
    def mock_pool(self):
        conn = AsyncMock()
        pool = AsyncMock()

        @asynccontextmanager
        async def fake_connection():
            yield conn

        pool.connection = fake_connection
        return pool, conn

    async def test_claim_devolve_provider_message_key(self, mock_pool):
        """Sem a coluna no RETURNING, a Evolution não tem como baixar mídia."""
        pool, conn = mock_pool
        agora = datetime.now(UTC)
        key = {"id": "MSG1", "remoteJid": "5511987654321@s.whatsapp.net"}

        row = (
            10,  # id
            "EVO1",  # message_id
            "+5511987654321",  # phone_number
            "+5511111111111",  # to_number
            "illumi_assistant",  # agent_id
            "+5511987654321:illumi_assistant",  # thread_id
            "olha a foto",  # incoming_message
            None,  # media_url
            "image/jpeg",  # media_type
            None,  # normalized_input
            None,  # media_processing_status
            None,  # media_processing_error
            "processing",  # status
            agora,  # process_after
            1,  # attempts
            3,  # max_attempts
            agora,  # lease_until
            None,  # response
            None,  # error
            None,  # outbound_token
            "evolution",  # channel
            key,  # provider_message_key
            agora,  # created_at
            agora,  # updated_at
            None,  # processed_at
        )

        stale_cursor = AsyncMock()
        claim_cursor = AsyncMock()
        claim_cursor.fetchone = AsyncMock(return_value=row)
        conn.execute = AsyncMock(side_effect=[stale_cursor, claim_cursor])

        message = await claim_next(pool, lease_seconds=60)

        claim_sql = conn.execute.call_args_list[1][0][0]
        assert "provider_message_key" in claim_sql
        assert message is not None
        assert message.provider_message_key == key
