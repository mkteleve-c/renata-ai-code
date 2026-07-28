"""Verifica o backfill conservador de `last_inbound_at` da migração 013.

A 013 já está aplicada em todo banco de dev — corrigi-la exigiria uma
migração nova, não editar o arquivo. Por isso o teste lê o UPDATE de
backfill direto do arquivo em disco em vez de reescrevê-lo aqui: assim ele
acompanha qualquer 014 que venha a substituir a lógica, e não só a intenção
lembrada no momento em que foi escrito.
"""

import re
from pathlib import Path

import psycopg
import pytest

from whatsapp_langchain.shared.db import get_pool

_MIGRACAO = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "013_last_inbound_at.sql"
)


def _backfill_sql() -> str:
    sql = _MIGRACAO.read_text()
    match = re.search(r"UPDATE leads_crm SET last_inbound_at.*?;", sql, re.DOTALL)
    assert match, "backfill não encontrado em 013_last_inbound_at.sql"
    return match.group(0)


@pytest.mark.asyncio
async def test_backfill_poupa_quem_ja_recebeu_followup():
    """followup_count > 0 significa que last_interaction_at é o NOSSO envio,
    não o inbound do lead — copiá-lo superestimaria a janela em até 24h.

    Roda numa transação com `psycopg.Rollback` ao final: o UPDATE extraído
    do arquivo não tem cláusula de telefone (é o backfill de produção, que
    varre a tabela inteira), então precisa ser revertido sem nunca dar
    commit — senão contamina o resto de `leads_crm` no banco de dev.
    """
    pool = await get_pool()
    resultado: dict[str, object] = {}

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await cur.execute(
                "delete from leads_crm where phone in (%s, %s)",
                ("999990000001", "999990000002"),
            )
            await cur.execute(
                "insert into leads_crm (phone, followup_count, "
                "last_interaction_at, last_inbound_at) "
                "values (%s, 0, now() - interval '5 hours', null)",
                ("999990000001",),
            )
            await cur.execute(
                "insert into leads_crm (phone, followup_count, "
                "last_interaction_at, last_inbound_at) "
                "values (%s, 3, now() - interval '5 hours', null)",
                ("999990000002",),
            )

            # .encode() — mesmo padrão de shared/db.py ao aplicar SQL lido de
            # arquivo: bytes satisfaz QueryNoTemplate sem exigir LiteralString.
            await cur.execute(_backfill_sql().encode())

            await cur.execute(
                "select phone, last_inbound_at from leads_crm where phone in (%s, %s)",
                ("999990000001", "999990000002"),
            )
            for phone, valor in await cur.fetchall():
                resultado[phone] = valor

            raise psycopg.Rollback()

    assert resultado["999990000001"] is not None, "followup_count = 0 recebe o backfill"
    assert resultado["999990000002"] is None, (
        "followup_count > 0 fica NULL — last_interaction_at ali é o nosso envio"
    )
