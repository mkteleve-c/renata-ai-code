import pytest_asyncio

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.phone import canonicalizar, variacoes


@pytest_asyncio.fixture
async def lead_factory():
    """Cria leads com relógios controlados. Limpa o que criou no teardown.

    O teardown apaga as DUAS variações (com/sem o 9º dígito) do telefone
    passado, não o literal: o caminho aceito do gate canonicaliza `phone` no
    mesmo UPDATE que grava `last_inbound_at`, então a linha pode sobreviver
    sob uma forma diferente da que criou o lead — sem isto, ela vaza para a
    próxima rodada de testes já em condições de ser reivindicada pela régua.
    """
    criados: list[str] = []

    async def _criar(
        phone: str,
        *,
        phase: str = "iniciou_conversa",
        followup_count: int = 0,
        followup_active: bool = True,
        agent_active: bool = True,
        name: str | None = "Fulano",
        minutos_desde_interacao: int = 0,
        minutos_desde_inbound: int | None = None,
    ) -> str:
        """`minutos_desde_inbound=None` deixa last_inbound_at NULL de propósito."""
        pool = await get_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "insert into leads_crm "
                "  (phone, name, phase, followup_count, followup_active, "
                "   agent_active, last_interaction_at, last_inbound_at) "
                "values (%s, %s, %s, %s, %s, %s, "
                "        now() - make_interval(mins => %s), "
                "        case when %s::int is null then null "
                "             else now() - make_interval(mins => %s) end)",
                (
                    phone,
                    name,
                    phase,
                    followup_count,
                    followup_active,
                    agent_active,
                    minutos_desde_interacao,
                    minutos_desde_inbound,
                    minutos_desde_inbound,
                ),
            )
            await conn.commit()
        criados.append(phone)
        return phone

    yield _criar

    pool = await get_pool()
    telefones = {
        variacao
        for phone in criados
        for variacao in variacoes(canonicalizar(phone) or phone)
    }
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "delete from leads_crm where phone = any(%s)", (list(telefones),)
        )
        await conn.commit()
