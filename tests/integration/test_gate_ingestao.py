"""Gate de ingestão: as regras que decidem se a mensagem vira item de fila."""

import asyncio

import pytest
from psycopg.types.json import Jsonb

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate

TELEFONE = "551188887777"
COM_9 = "5511988887777"
JID = {"remoteJid": "5511988887777@s.whatsapp.net", "fromMe": False}


@pytest.fixture
async def permitir_forma_legada():
    """Alguns testes abaixo fundem DUAS linhas físicas (`COM_9` + `TELEFONE`)
    que compartilham a mesma identidade canônica — exatamente o cenário que
    a migração `014_uma_linha_por_pessoa.sql` (Task 7) tornou
    IRREPRESENTÁVEL em `leads_crm` via o CHECK `leads_crm_phone_canonico_check`.

    O código de fusão que eles exercitam (`shared/leads.py::_fundir` /
    `_persistir_consolidacao`, dentro de `aplicar_gate`) continua existindo
    — é a referência que a migração 014 reimplementa em SQL para consolidar
    a base legada uma única vez — mas deixou de ser alcançável em produção:
    `aplicar_gate` nunca mais vai encontrar duas linhas físicas para o
    mesmo telefone, porque nunca mais vai conseguir CRIAR a segunda.

    Este fixture derruba o CHECK (via `COMMIT`, não rollback — `aplicar_gate`
    abre a PRÓPRIA conexão do pool, então uma alteração de schema só
    presa numa transação não commitada bloquearia ela, não seria enxergada)
    só para o teste que o pede, e SEMPRE restaura no teardown: outros
    testes do mesmo processo, incluindo os desta rodada de mutação,
    dependem da invariante estar em vigor.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
        )
    try:
        yield
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "alter table leads_crm add constraint "
                "leads_crm_phone_canonico_check "
                "check (phone !~ '^55[0-9]{2}9[0-9]{8}$' and phone !~ '^550')"
            )


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


async def test_encontra_lead_gravado_com_9(permitir_forma_legada, limpar):
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


async def test_consolida_duplicata_com_e_sem_9(permitir_forma_legada, limpar):
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


async def test_fusao_preserva_metadata_da_linha_legada(permitir_forma_legada, limpar):
    """`metadata` tem default '{}' — coalesce por `is not None` nunca cai para a
    linha antiga, e o DELETE seguinte torna a perda irreversível."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, metadata, last_interaction_at) "
            "values (%s, %s, '2019-01-01')",
            (COM_9, Jsonb({"origem": "linkedin", "utm": "camp-2024"})),
        )
        await conn.execute(
            "insert into leads_crm (phone, metadata, last_interaction_at) "
            "values (%s, %s, '2024-01-01')",
            (TELEFONE, Jsonb({})),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.lead["metadata"]["origem"] == "linkedin"
    assert r.lead["metadata"]["utm"] == "camp-2024"


async def test_fusao_registra_linhas_fundidas(permitir_forma_legada, limpar):
    """A auditoria da fusão: os telefones originais são a única cópia do que
    existia antes do DELETE da linha legada."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, last_interaction_at) "
            "values (%s, '2019-01-01')",
            (COM_9,),
        )
        await conn.execute(
            "insert into leads_crm (phone, last_interaction_at) "
            "values (%s, '2024-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert sorted(r.lead["metadata"]["linhas_fundidas"]) == sorted([TELEFONE, COM_9])


async def test_fusao_da_precedencia_a_linha_vencedora_por_chave(
    permitir_forma_legada, limpar
):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, metadata, last_interaction_at) "
            "values (%s, %s, '2019-01-01')",
            (COM_9, Jsonb({"origem": "linkedin", "utm": "antiga"})),
        )
        await conn.execute(
            "insert into leads_crm (phone, metadata, last_interaction_at) "
            "values (%s, %s, '2024-01-01')",
            (TELEFONE, Jsonb({"utm": "nova"})),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.lead["metadata"]["utm"] == "nova", "a linha recente vence por chave"
    assert r.lead["metadata"]["origem"] == "linkedin", "chave só da antiga sobrevive"


async def test_fusao_preserva_o_maior_followup_count(permitir_forma_legada, limpar):
    """Mesmo defeito do metadata: default 0 não é None e nunca cai para a antiga.

    Não é observável no caminho aceito (o upsert zera), mas fica gravado na
    fusão persistida e é o que o gate devolve no caminho de descarte.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, agent_active, followup_count, "
            "last_interaction_at) values (%s, false, 7, '2019-01-01')",
            (COM_9,),
        )
        await conn.execute(
            "insert into leads_crm (phone, agent_active, followup_count, "
            "last_interaction_at) values (%s, true, 0, '2024-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is False
    assert r.motivo == "agente_desligado"
    assert r.lead["followup_count"] == 7


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


async def test_consolida_duplicata_com_fase_nula(permitir_forma_legada, limpar):
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


async def test_consolida_duplicata_agendou_sessao_vence_perdido(
    permitir_forma_legada, limpar
):
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
    permitir_forma_legada,
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


async def test_duplicata_reactivate_at_nao_e_coalescido_de_volta(
    permitir_forma_legada, limpar
):
    """Discrimina coalesce de _vencedor_pausa: as duas linhas têm agent_active=true,
    então a mensagem é aceita e a fusão é persistida — mas a linha vencedora
    (a mais recente) tem agent_reactivate_at NULL, e a antiga tem um 2030 obsoleto.
    Coalesce (recente-se-não-nulo-senão-antiga) produziria 2030; o resultado tem
    que ser NULL porque NULL ali significa "sem reativação agendada", não "sem
    dado". Nenhum teste anterior discriminava isso: no cenário de
    test_duplicata_com_legada_pausada_..., a linha vencedora já tinha o 2030."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, agent_active, agent_reactivate_at, "
            "last_interaction_at) values (%s, true, '2030-01-01', '2019-01-01')",
            (COM_9,),
        )
        await conn.execute(
            "insert into leads_crm (phone, agent_active, agent_reactivate_at, "
            "last_interaction_at) values (%s, true, null, '2024-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.lead["agent_reactivate_at"] is None, "coalesce ressuscitaria o 2030"


async def test_from_me_com_duplicata_desliga_as_duas_linhas(
    permitir_forma_legada, limpar
):
    """fromMe usa `where phone in (com_9, sem_9)`, não o phone de uma consolidação —
    sem consolidar nada, as duas formas do telefone precisam ficar com agent_active
    false."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, agent_active) values (%s, true)", (COM_9,)
        )
        await conn.execute(
            "insert into leads_crm (phone, agent_active) values (%s, true)",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, {**JID, "fromMe": True}, push_name=None)

    assert r.aceito is False
    assert r.motivo == "from_me"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select phone, agent_active from leads_crm where phone in (%s, %s)",
            (TELEFONE, COM_9),
        )
        linhas = await cur.fetchall()

    assert len(linhas) == 2, "fromMe não pode consolidar nem apagar nenhuma das duas"
    assert all(agent_active is False for _, agent_active in linhas)


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


async def test_concorrencia_lead_legado_nao_perde_atualizacao(
    permitir_forma_legada, limpar
):
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
