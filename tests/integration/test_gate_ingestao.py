"""Gate de ingestão: as regras que decidem se a mensagem vira item de fila.

Duplicata física (duas linhas para a mesma identidade canônica) não é mais
testável AQUI: a migração `014_uma_linha_por_pessoa.sql` (+ `015`, que fecha
o furo do singleton) tornou `leads_crm.phone` sempre canônico, garantido
pelo CHECK `leads_crm_phone_canonico_check` — inserir a forma com o 9º
dígito direto na tabela, como os testes de fusão desta suíte faziam,
levanta `CheckViolation`. O código de fusão que eles exercitavam
(`shared/leads.py::_fundir`/`_persistir_consolidacao`, dentro de
`aplicar_gate`) continua existindo — é a referência que a 014 reimplementa
em SQL — mas ficou inalcançável em produção: `aplicar_gate` nunca mais vai
encontrar duas linhas físicas, porque nunca mais vai conseguir CRIAR a
segunda. A prova das regras de fusão mora em `test_migracao_014.py`, contra
o SQL real da migração, não aqui.
"""

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
        await conn.execute(
            "delete from blocklist where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        await conn.execute(
            "delete from leads_descartados where phone_original like %s", ("%g.us",)
        )
    yield
    async with pool.connection() as conn:
        await conn.execute(
            "delete from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        await conn.execute(
            "delete from blocklist where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        await conn.execute(
            "delete from leads_descartados where phone_original like %s", ("%g.us",)
        )


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


async def test_blocklist_na_forma_com_9_descarta(limpar):
    """Opt-out importado do n8n vem com o 9º dígito e precisa barrar do mesmo jeito.

    O CHECK da blocklist aceita as duas formas e o lookup de leads_crm já
    consultava as duas — só a blocklist olhava para a canônica.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("insert into blocklist (phone) values (%s)", (COM_9,))

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is False
    assert r.motivo == "blocklist"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone in (%s, %s)", (TELEFONE, COM_9)
        )
        (total,) = await cur.fetchone()

    assert total == 0, "blocklist com 9 não pode deixar criar lead"


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


async def test_telefone_invalido_e_retido_em_leads_descartados(limpar):
    """Mensagem real de WhatsApp descartada não pode sumir só com uma linha de log."""
    pool = await get_pool()
    key = {"remoteJid": "1234-5678@g.us", "fromMe": False, "id": "MSG-DESCARTE"}

    r = await aplicar_gate(pool, key, push_name=None, payload={"key": key})

    assert r.aceito is False
    assert r.motivo == "telefone_invalido"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select motivo, payload from leads_descartados where phone_original = %s",
            ("1234-5678@g.us",),
        )
        linha = await cur.fetchone()

    assert linha is not None, "o descarte precisa ficar em leads_descartados"
    assert linha[0] == "telefone_invalido"
    assert linha[1]["key"]["id"] == "MSG-DESCARTE", "o payload precisa ser retido"


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


async def _minutos_desde_inbound(phone: str) -> float:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select extract(epoch from now() - last_inbound_at) / 60 "
            "from leads_crm where phone = %s",
            (phone,),
        )
        (minutos,) = await cur.fetchone()
    return minutos


async def test_gate_grava_last_inbound_at(lead_factory):
    """Cobre o ramo UPDATE do upsert: lead já existe (pré-criado pela fixture).

    `lead_factory` insere na forma literal passada, sem canonicalizar — e
    `leads_crm.phone` agora só aceita a forma canônica (CHECK da migração
    014). O JID do webhook continua chegando com o 9º dígito (é assim que a
    Meta manda) — só a linha pré-criada precisa nascer já canônica.
    """
    await lead_factory("551187654321", minutos_desde_inbound=180)

    resultado = await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511987654321@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )
    assert resultado.aceito
    # Literal, não resultado.canonico: se a canonicalização quebrar e o gate
    # criar uma linha nova em vez de atualizar a existente, o teste tem que
    # acusar isso — não seguir o valor que o próprio código sob teste produziu.
    assert await _minutos_desde_inbound("551187654321") < 1


async def test_gate_grava_last_inbound_at_em_lead_novo():
    """Cobre o ramo INSERT do upsert — nenhum teste anterior o exercitava.

    lead_factory sempre pré-cria a linha, então test_gate_grava_last_inbound_at
    só provava o UPDATE. Sem isto, tirar last_inbound_at do INSERT (leads.py)
    passa 100% da suíte: todo lead novo nasceria com a coluna NULL e a régua
    da Task 3, que filtra `last_inbound_at is not null`, nunca o reivindicaria.
    """
    telefone, canonico = "5511987650001", "551187650001"
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (canonico,))

    try:
        resultado = await aplicar_gate(
            pool,
            key={"remoteJid": f"{telefone}@s.whatsapp.net", "fromMe": False},
            push_name="Fulano",
        )
        assert resultado.aceito
        assert resultado.canonico == canonico
        assert await _minutos_desde_inbound(canonico) < 1
    finally:
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = %s", (canonico,))


async def test_inbound_de_lead_pausado_ainda_move_a_janela(lead_factory):
    """A janela é fato sobre a Meta, não sobre o nosso funil.

    Sem isto, tudo que o lead escreve durante o handover humano não conta, e na
    retomada ele pode estar inalcançável pela régua com a janela real aberta.

    A linha pré-criada nasce canônica (CHECK da migração 014); o JID do
    webhook continua chegando com o 9º dígito.
    """
    await lead_factory("551187654322", agent_active=False, minutos_desde_inbound=180)

    resultado = await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511987654322@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )
    assert not resultado.aceito
    assert resultado.motivo == "agente_desligado"
    assert await _minutos_desde_inbound("551187654322") < 1


async def test_mensagem_nossa_nao_move_a_janela(lead_factory):
    """fromMe somos nós — não abre janela nenhuma."""
    await lead_factory("551187654323", minutos_desde_inbound=180)

    await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511987654323@s.whatsapp.net", "fromMe": True},
        push_name="Fulano",
    )
    assert await _minutos_desde_inbound("551187654323") > 170
