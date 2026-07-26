"""Verifica que a migração 007 cria o schema do SDR da EleveC."""

import psycopg
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
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "insert into leads_crm (phone) values ('+5511987654321')"
            )


@pytest.mark.asyncio
async def test_colunas_de_pausa_sao_not_null():
    """NULL em agent_active significava coisas opostas em dois lugares.

    O gate (`is False`) lia NULL como ativo; o índice parcial de follow-up
    (`WHERE followup_active AND agent_active`) excluía a linha, ou seja lia
    NULL como pausado. Sem NULL possível, não há duas leituras.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select column_name, is_nullable from information_schema.columns "
            "where table_name = 'leads_crm' and column_name in "
            "('agent_active', 'followup_active', 'followup_count', 'metadata')"
        )
        nulabilidade = dict(await cur.fetchall())

    assert nulabilidade == {
        "agent_active": "NO",
        "followup_active": "NO",
        "followup_count": "NO",
        "metadata": "NO",
    }


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
