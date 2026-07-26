"""Gate de ingestão: as regras que decidem se a mensagem vira item de fila."""

import pytest

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate

TELEFONE = "551188887777"
JID = {"remoteJid": "5511988887777@s.whatsapp.net", "fromMe": False}


@pytest.fixture
async def limpar():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute("delete from blocklist where phone = %s", (TELEFONE,))
    yield
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute("delete from blocklist where phone = %s", (TELEFONE,))


async def test_lead_novo_e_criado_e_aceito(limpar):
    pool = await get_pool()
    r = await aplicar_gate(pool, JID, push_name="Fulano")

    assert r.aceito is True
    assert r.canonico == TELEFONE
    assert r.lead["name"] == "Fulano"
    assert r.lead["phase"] == "iniciou_conversa"


async def test_blocklist_descarta(limpar):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("insert into blocklist (phone) values (%s)", (TELEFONE,))

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is False
    assert r.motivo == "blocklist"


async def test_from_me_desliga_agente_e_descarta(limpar):
    pool = await get_pool()
    await aplicar_gate(pool, JID, push_name="Fulano")

    r = await aplicar_gate(pool, {**JID, "fromMe": True}, push_name=None)

    assert r.aceito is False
    assert r.motivo == "from_me"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active, followup_active from leads_crm where phone = %s",
            (TELEFONE,),
        )
        agent_active, followup_active = await cur.fetchone()

    assert agent_active is False
    assert followup_active is False


async def test_agente_desligado_nao_escreve_nada(limpar):
    """A checagem vem ANTES do upsert — lead pausado não tem contador zerado."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, agent_active, followup_count, "
            "last_interaction_at) values (%s, false, 2, '2020-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name="Fulano")

    assert r.aceito is False
    assert r.motivo == "agente_desligado"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select followup_count, extract(year from last_interaction_at)::int "
            "from leads_crm where phone = %s",
            (TELEFONE,),
        )
        contador, ano = await cur.fetchone()

    assert contador == 2, "followup_count não pode ser zerado para lead pausado"
    assert ano == 2020, "last_interaction_at não pode ser renovado"


async def test_telefone_invalido_descarta(limpar):
    pool = await get_pool()
    r = await aplicar_gate(pool, {"remoteJid": "abc@g.us"}, push_name=None)

    assert r.aceito is False
    assert r.motivo == "telefone_invalido"


async def test_encontra_lead_gravado_com_9(limpar):
    """Lead salvo na forma não-canônica ainda é encontrado."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, 'qualificado')",
            ("5511988887777",),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.canonico == TELEFONE
    assert r.lead["phase"] == "qualificado", "a fase não pode regredir"

    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", ("5511988887777",))
