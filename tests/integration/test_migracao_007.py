"""Verifica que a migração 007 cria o schema do SDR da EleveC."""

import pytest

from whatsapp_langchain.shared.db import get_pool


@pytest.mark.asyncio
async def test_tabelas_do_sdr_existem():
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public'"
        )
        tabelas = {linha[0] for linha in await cur.fetchall()}

    assert {
        "leads_crm",
        "blocklist",
        "legacy_chat_history",
        "leads_descartados",
    } <= tabelas


@pytest.mark.asyncio
async def test_phone_rejeita_formato_invalido():
    pool = await get_pool()
    async with pool.connection() as conn:
        with pytest.raises(Exception):
            await conn.execute(
                "insert into leads_crm (phone) values ('+5511987654321')"
            )


@pytest.mark.asyncio
async def test_phone_aceita_canonico():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone) values ('551199990000') "
            "on conflict do nothing"
        )
        cur = await conn.execute(
            "select phase from leads_crm where phone = '551199990000'"
        )
        linha = await cur.fetchone()
        await conn.execute("delete from leads_crm where phone = '551199990000'")

    assert linha[0] == "formulario_preenchido"
