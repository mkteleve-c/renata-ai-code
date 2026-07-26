"""Gate de ingestão: as regras que decidem se a mensagem vira item de fila."""

import asyncio

import pytest

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate

TELEFONE = "551188887777"
COM_9 = "5511988887777"
JID = {"remoteJid": "5511988887777@s.whatsapp.net", "fromMe": False}


@pytest.fixture
async def limpar():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "delete from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        await conn.execute("delete from blocklist where phone = %s", (TELEFONE,))
    yield
    async with pool.connection() as conn:
        await conn.execute(
            "delete from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
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

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        (total,) = await cur.fetchone()

    assert total == 0, "blocklist descarta antes de qualquer escrita em leads_crm"


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


async def test_from_me_sem_lead_existente_descarta_sem_erro(limpar):
    pool = await get_pool()
    r = await aplicar_gate(pool, {**JID, "fromMe": True}, push_name=None)

    assert r.aceito is False
    assert r.motivo == "from_me"
    assert r.lead is None

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        (total,) = await cur.fetchone()

    assert total == 0


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
    """Lead salvo na forma não-canônica é encontrado e a linha é canonicalizada."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, 'qualificado')",
            (COM_9,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.canonico == TELEFONE
    assert r.lead["phone"] == TELEFONE, "o UPDATE precisa canonicalizar o phone"
    assert r.lead["phase"] == "qualificado", "a fase não pode regredir"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (COM_9,)
        )
        (restantes,) = await cur.fetchone()

    assert restantes == 0, "a linha com o 9º dígito não pode sobreviver ao UPDATE"


async def test_promove_fase_formulario_para_iniciou_conversa(limpar):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, 'formulario_preenchido')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.lead["phase"] == "iniciou_conversa"


async def test_consolida_duplicata_com_e_sem_9(limpar):
    """Duas linhas para o mesmo lead (base legada com 9 + gate novo sem 9) se fundem."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, email, followup_count, "
            "last_interaction_at) values (%s, 'qualificado', 'antigo@x.com', 5, "
            "'2019-01-01')",
            (COM_9,),
        )
        await conn.execute(
            "insert into leads_crm (phone, phase, followup_count, "
            "last_interaction_at) values (%s, 'iniciou_conversa', 1, '2024-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.lead["phone"] == TELEFONE
    assert r.lead["phase"] == "qualificado", "a fase mais avançada sobrevive à fusão"
    assert r.lead["email"] == "antigo@x.com", "não-nulo da linha legada é preservado"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (COM_9,)
        )
        (restantes,) = await cur.fetchone()

    assert restantes == 0, "a linha legada é apagada após a fusão"


async def test_consolida_duplicata_com_fase_nula(limpar):
    """phase é nullable — a fusão não pode estourar KeyError numa linha sem fase."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, null)", (COM_9,)
        )
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, 'qualificado')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.lead["phase"] == "qualificado"


async def test_consolida_duplicata_agendou_sessao_vence_perdido(limpar):
    """agendou_sessao é fato verificável (evento no calendário) e vence qualquer fase,
    inclusive perdido — mesmo quando perdido é a linha mais recente."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, last_interaction_at) "
            "values (%s, 'perdido', '2024-06-01')",
            (COM_9,),
        )
        await conn.execute(
            "insert into leads_crm (phone, phase, last_interaction_at) "
            "values (%s, 'agendou_sessao', '2019-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.lead["phase"] == "agendou_sessao"


async def test_duplicata_com_legada_pausada_nao_consolida_nem_ressuscita_handover(
    limpar,
):
    """Lead pausado numa duplicata não pode ter a fusão escrita (mesma doutrina de
    test_agente_desligado_nao_escreve_nada), e o merge não pode fazer false virar
    true nem coalescer agent_reactivate_at obsoleto de volta."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, agent_active, followup_active, "
            "agent_reactivate_at, followup_count, last_interaction_at) values "
            "(%s, false, false, '2030-01-01', 5, '2020-01-01')",
            (COM_9,),
        )
        await conn.execute(
            "insert into leads_crm (phone, agent_active, followup_active, "
            "followup_count, last_interaction_at) values "
            "(%s, true, true, 3, '2024-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name="Fulano")

    assert r.aceito is False
    assert r.motivo == "agente_desligado"
    assert r.lead["agent_active"] is False
    assert r.lead["followup_active"] is False
    assert r.lead["agent_reactivate_at"] is not None, "não pode virar NULL por coalesce"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        (total,) = await cur.fetchone()
        cur = await conn.execute(
            "select followup_count from leads_crm where phone = %s", (COM_9,)
        )
        (followup_count_legada,) = await cur.fetchone()

    assert total == 2, "duplicata não pode ser consolidada com o agente desligado"
    assert followup_count_legada == 5, "a linha legada não pode ser tocada"


async def test_concorrencia_lead_novo_gera_uma_linha_so(limpar):
    """Duas mensagens simultâneas de um lead novo não podem colidir no INSERT.

    Depende do pool ter pelo menos 2 conexões — com max_size=1 as duas
    chamadas rodariam em série de fato e o teste ficaria vacuosamente verde.
    """
    pool = await get_pool()
    assert pool.max_size >= 2, "teste de concorrência exige pool com 2+ conexões"

    r1, r2 = await asyncio.gather(
        aplicar_gate(pool, JID, push_name="Fulano"),
        aplicar_gate(pool, JID, push_name="Ciclano"),
    )

    assert r1.aceito is True
    assert r2.aceito is True
    assert r1.lead is not None
    assert r2.lead is not None

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (TELEFONE,)
        )
        (total,) = await cur.fetchone()

    assert total == 1


async def test_concorrencia_lead_legado_nao_perde_atualizacao(limpar):
    """Duas mensagens simultâneas de um lead legado (com 9) não colidem nem somem."""
    pool = await get_pool()
    assert pool.max_size >= 2, "teste de concorrência exige pool com 2+ conexões"
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, 'iniciou_conversa')",
            (COM_9,),
        )

    r1, r2 = await asyncio.gather(
        aplicar_gate(pool, JID, push_name="Fulano"),
        aplicar_gate(pool, JID, push_name="Ciclano"),
    )

    assert r1.aceito is True
    assert r2.aceito is True
    assert r1.lead is not None, "lost update faria o RETURNING vir vazio"
    assert r2.lead is not None

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        (total,) = await cur.fetchone()
        cur = await conn.execute(
            "select phone from leads_crm where phone = %s", (TELEFONE,)
        )
        canonica = await cur.fetchone()

    assert total == 1
    assert canonica is not None, "a linha sobrevivente precisa estar na forma canônica"
