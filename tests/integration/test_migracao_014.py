"""Verifica a migração 014: consolidação de duplicatas seguida do CHECK.

A 014 já está aplicada no banco de dev (o runner pula por nome de arquivo) —
corrigi-la exigiria uma migração nova, não editar o arquivo. Por isso, como
`test_migracao_013.py`, os testes leem o SQL direto do arquivo em disco em
vez de reimplementar a lógica aqui: assim eles acompanham qualquer 015 que
venha a substituir a consolidação, não só a intenção lembrada agora.

O CHECK novo (`leads_crm_phone_canonico_check`) já está em vigor nesta
tabela — os testes de FUSÃO precisam derrubá-lo dentro da própria transação
(sempre revertida no final) para poder inserir as formas malformadas que a
consolidação existe para resolver. É exatamente o estado em que a 014 real
encontrou a base de produção: linhas malformadas, CHECK ainda não criado.
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
    / "014_uma_linha_por_pessoa.sql"
)
_SQL = _MIGRACAO.read_text(encoding="utf-8")


def _trecho(inicio: str, fim: str) -> str:
    match = re.search(re.escape(inicio) + r".*?" + re.escape(fim), _SQL, re.DOTALL)
    assert match, f"trecho não encontrado em 014_uma_linha_por_pessoa.sql: {inicio!r}"
    return match.group(0)


def _sql_consolidacao() -> str:
    """Função + laço de fusão + DROP FUNCTION — sem o CHECK."""
    return _trecho(
        "CREATE OR REPLACE FUNCTION _migracao_014_canonico",
        "DROP FUNCTION _migracao_014_canonico(TEXT);",
    )


def _sql_check() -> str:
    return _trecho(
        "DO $$ BEGIN\n    ALTER TABLE leads_crm",
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
    )


async def _permitir_formas_malformadas(conn):
    """Derruba o CHECK novo dentro da transação do chamador — é o estado em
    que a 014 real encontrou a base de produção: linhas malformadas, CHECK
    ainda não criado. Nunca commitada; some no rollback do teste."""
    await conn.execute(
        "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
    )


async def _consolidar_dentro_de_transacao(conn, telefones_de_teste: list[str]):
    """Roda só a consolidação do arquivo real (o CHECK precisa já ter sido
    derrubado por `_permitir_formas_malformadas`) e devolve as linhas
    remanescentes para os telefones dados — tudo dentro da transação do
    chamador, que é sempre revertida."""
    await conn.execute(_sql_consolidacao().encode())
    cur = await conn.execute(
        "select phone, name, phase, followup_count, last_inbound_at, "
        "agent_active, followup_active, metadata "
        "from leads_crm where phone = any(%s)",
        (telefones_de_teste,),
    )
    return await cur.fetchall()


@pytest.mark.asyncio
async def test_par_com_9_e_sem_9_e_consolidado_em_uma_linha():
    """O par mais comum da base legada: mesma pessoa com e sem o 9º dígito.
    Timestamps DIFERENTES nos dois lados de propósito — testes anteriores
    desta régua passaram sendo autoconfirmatórios por usar o mesmo instante
    nas duas linhas do grupo."""
    sem_9 = "551197712340"
    com_9 = "5511997712340"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([sem_9, com_9],)
            )
            await cur.execute(
                "insert into leads_crm (phone, name, last_interaction_at) "
                "values (%s, %s, now() - interval '2 hours')",
                (sem_9, "Nome Antigo"),
            )
            await cur.execute(
                "insert into leads_crm (phone, name, last_interaction_at) "
                "values (%s, %s, now() - interval '10 minutes')",
                (com_9, "Nome Recente"),
            )

            linhas = await _consolidar_dentro_de_transacao(conn, [sem_9, com_9])

            assert len(linhas) == 1, "o par tem que virar uma linha só"
            (phone,) = (linhas[0][0],)
            assert phone == sem_9, (
                "a linha sobrevivente tem que estar na forma canônica"
            )
            assert linhas[0][1] == "Nome Recente", (
                "campo de conteúdo vem do registro com last_interaction_at mais recente"
            )
            assert set(linhas[0][7].get("linhas_fundidas", [])) == {sem_9, com_9}, (
                "as formas absorvidas ficam registradas em metadata, como a 012 já faz"
            )

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_trio_incluindo_forma_de_zero_de_tronco_e_consolidado():
    """A especificação registra grupos de 3 na base legada. A terceira forma
    é o zero de tronco (`55011...`) que `phone.py` documenta como o perfil
    dos ~26 registros malformados — `variacoes()` nunca alcança essa forma
    por enumeração; só a consolidação em massa resolve."""
    sem_9 = "551197712341"
    com_9 = "5511997712341"
    zero_tronco = "55011997712341"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)",
                ([sem_9, com_9, zero_tronco],),
            )
            await cur.execute(
                "insert into leads_crm (phone, last_interaction_at) "
                "values (%s, now() - interval '3 hours')",
                (sem_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, last_interaction_at) "
                "values (%s, now() - interval '1 hours')",
                (com_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, last_interaction_at) "
                "values (%s, now() - interval '20 minutes')",
                (zero_tronco,),
            )

            linhas = await _consolidar_dentro_de_transacao(
                conn, [sem_9, com_9, zero_tronco]
            )

            assert len(linhas) == 1, "as três linhas do trio têm que virar uma só"
            assert linhas[0][0] == sem_9

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_lado_pausado_vence_na_fusao():
    """`_vencedor_pausa` (leads.py): agent_active=false vence, mesmo vindo
    da linha menos recente — errar para o lado de não mandar mensagem é
    recuperável, mandar para quem está pausado não é."""
    sem_9 = "551197712342"
    com_9 = "5511997712342"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([sem_9, com_9],)
            )
            await cur.execute(
                "insert into leads_crm (phone, agent_active, last_interaction_at) "
                "values (%s, true, now() - interval '5 minutes')",
                (sem_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, agent_active, followup_active, "
                "last_interaction_at) "
                "values (%s, false, false, now() - interval '3 hours')",
                (com_9,),
            )

            linhas = await _consolidar_dentro_de_transacao(conn, [sem_9, com_9])

            assert len(linhas) == 1
            _, _, _, _, _, agent_active, followup_active, _ = linhas[0]
            assert agent_active is False, (
                "o lado pausado (mesmo mais antigo) tem que vencer a fusão"
            )
            assert followup_active is False

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_maior_followup_count_vence():
    """A escada de follow-up já percorrida não pode ser esquecida por uma
    linha nova que nasceu com 0 — `max(followup_count)`, não o do vencedor
    de last_interaction_at."""
    sem_9 = "551197712343"
    com_9 = "5511997712343"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([sem_9, com_9],)
            )
            await cur.execute(
                "insert into leads_crm (phone, followup_count, last_interaction_at) "
                "values (%s, 2, now() - interval '1 hours')",
                (sem_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, followup_count, last_interaction_at) "
                "values (%s, 0, now() - interval '2 minutes')",
                (com_9,),
            )

            linhas = await _consolidar_dentro_de_transacao(conn, [sem_9, com_9])

            assert len(linhas) == 1
            assert linhas[0][3] == 2, (
                "o maior followup_count tem que vencer, mesmo vindo do lado "
                "MENOS recente"
            )

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_menor_last_inbound_at_vence():
    """`min(last_inbound_at)` — o conservador para a janela de 24h da régua.
    Copiar o valor mais recente superestimaria quanto tempo falta para a
    janela fechar."""
    sem_9 = "551197712344"
    com_9 = "5511997712344"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([sem_9, com_9],)
            )
            await cur.execute(
                "insert into leads_crm (phone, last_inbound_at, last_interaction_at) "
                "values (%s, now() - interval '20 hours', now() - interval '3 hours')",
                (sem_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, last_inbound_at, last_interaction_at) "
                "values (%s, now() - interval '10 minutes', "
                "now() - interval '5 minutes')",
                (com_9,),
            )

            linhas = await _consolidar_dentro_de_transacao(conn, [sem_9, com_9])

            assert len(linhas) == 1
            last_inbound_at = linhas[0][4]
            # o mais ANTIGO (20h atrás) tem que vencer, não o mais recente
            # (10 min atrás) — mesmo sendo o lado com last_interaction_at
            # menos recente.
            import datetime

            agora = datetime.datetime.now(datetime.UTC)
            assert (agora - last_inbound_at) > datetime.timedelta(hours=15), (
                last_inbound_at
            )

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_check_rejeita_as_tres_formas_proibidas_depois_de_aplicado():
    """O CHECK já está em vigor nesta tabela (não é derrubado aqui, ao
    contrário dos testes de fusão acima) — prova as três formas que a
    consolidação existe para nunca mais deixar entrar: o 9º dígito puro, o
    zero de tronco puro, e a combinação das duas (a forma real documentada
    em `phone.py`, `55011...`)."""
    pool = await get_pool()
    formas_proibidas = [
        "5511987654399",  # com o 9º dígito
        "5501187654399",  # zero de tronco
        "55011987654399",  # zero de tronco + 9º dígito — o caso real da base legada
    ]
    for phone in formas_proibidas:
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor()
                with pytest.raises(psycopg.errors.CheckViolation):
                    await cur.execute(
                        "insert into leads_crm (phone) values (%s)", (phone,)
                    )
                raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_check_do_arquivo_aceita_a_forma_canonica():
    """Controle negativo do teste acima: sem isto, um CHECK bugado que
    rejeitasse TUDO passaria nos três casos anteriores."""
    pool = await get_pool()
    phone = "551187654398"
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await cur.execute("insert into leads_crm (phone) values (%s)", (phone,))
            cur2 = await conn.execute(
                "select phone from leads_crm where phone = %s", (phone,)
            )
            linha = await cur2.fetchone()
            assert linha is not None
            assert linha[0] == phone
            raise psycopg.Rollback()


def test_check_sql_cobre_as_duas_formas_no_arquivo():
    """Ancora o SQL do CHECK em si — sem depender do banco — contra uma
    edição futura que afrouxe silenciosamente uma das duas cláusulas."""
    sql = _sql_check()
    assert "55[0-9]{2}9[0-9]{8}" in sql
    assert "^550" in sql
