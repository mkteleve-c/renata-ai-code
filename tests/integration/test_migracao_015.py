"""Verifica a migração 015: as duas tabelas de destino da migração de dados
do Supabase legado, com contratos OPOSTOS.

`legacy_chat_history` só aceita telefone canônico — FK para `leads_crm`,
cai junto no `DELETE`. `leads_descartados` aceita qualquer coisa em
`phone_original`, inclusive `NULL` e a string literal `"null"` — é o
depósito do que não converge numa migração de dados reais, e não pode
ganhar `NOT NULL` sob pena de perder exatamente as linhas que existe para
reportar.

A 015 já está aplicada no banco de dev (o runner pula por nome de
arquivo) — como `test_migracao_013.py`/`test_migracao_014.py`, este teste
ancora trechos do arquivo real em disco (migração nunca é editada depois
de aplicada; uma correção vira 016) além de verificar o comportamento
contra o Postgres já migrado.

`leads_descartados` não tem coluna `id` (herdada de 007_elevec.sql sem
chave própria) — as buscas abaixo usam `motivo` como discriminador único
por teste em vez de `RETURNING id`.
"""

from pathlib import Path

import psycopg
import pytest

from whatsapp_langchain.shared.db import get_pool

_MIGRACAO = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "015_legacy_chat_history.sql"
)
_SQL = _MIGRACAO.read_text(encoding="utf-8")

_PHONE = "551188990011"


def test_sql_do_arquivo_mantem_o_contrato_das_duas_tabelas():
    """Ancora o texto do arquivo — sem depender do banco — contra uma
    edição futura que afrouxe silenciosamente a FK, o UNIQUE, o CHECK ou
    os NOT NULL de leads_descartados."""
    assert "REFERENCES leads_crm(phone) ON DELETE CASCADE" in _SQL
    assert "UNIQUE (phone, ordem)" in _SQL
    assert "CHECK (papel IN ('human', 'ai'))" in _SQL
    assert "ALTER COLUMN motivo SET NOT NULL" in _SQL
    assert "ALTER COLUMN payload SET NOT NULL" in _SQL
    assert "ALTER COLUMN phone_original" not in _SQL, (
        "phone_original não pode ganhar NOT NULL -- é o que esta migração "
        "existe para não fazer"
    )


@pytest.fixture
async def lead_ancora():
    """Garante um lead canônico em leads_crm para a FK de
    legacy_chat_history apontar, e limpa antes/depois."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "delete from legacy_chat_history where phone = %s", (_PHONE,)
        )
        await conn.execute("delete from leads_crm where phone = %s", (_PHONE,))
        await conn.execute("insert into leads_crm (phone) values (%s)", (_PHONE,))
    yield _PHONE
    async with pool.connection() as conn:
        await conn.execute(
            "delete from legacy_chat_history where phone = %s", (_PHONE,)
        )
        await conn.execute("delete from leads_crm where phone = %s", (_PHONE,))


@pytest.mark.asyncio
async def test_legacy_chat_history_rejeita_telefone_que_nao_existe_em_leads_crm():
    pool = await get_pool()
    inexistente = "551100000099"
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (inexistente,))
        # pytest.raises FORA de conn.transaction(): a exceção precisa
        # propagar através do __aexit__ da transação para que psycopg faça
        # ROLLBACK. Se pytest.raises engolir a exceção por dentro, a
        # transação não sabe que falhou e tenta COMMIT sobre uma conexão já
        # abortada pelo Postgres -- InFailedSqlTransaction, não o teste que
        # se quer provar aqui.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            async with conn.transaction():
                await conn.execute(
                    "insert into legacy_chat_history (phone, ordem, papel, conteudo) "
                    "values (%s, 1, 'human', 'oi')",
                    (inexistente,),
                )


@pytest.mark.asyncio
async def test_legacy_chat_history_aceita_telefone_canonico_existente(lead_ancora):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into legacy_chat_history (phone, ordem, papel, conteudo) "
            "values (%s, 1, 'human', 'oi')",
            (lead_ancora,),
        )
        cur = await conn.execute(
            "select conteudo from legacy_chat_history where phone = %s", (lead_ancora,)
        )
        linha = await cur.fetchone()
    assert linha is not None
    assert linha[0] == "oi"


@pytest.mark.asyncio
async def test_legacy_chat_history_cai_junto_no_delete_do_lead(lead_ancora):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into legacy_chat_history (phone, ordem, papel, conteudo) "
            "values (%s, 1, 'human', 'oi')",
            (lead_ancora,),
        )
        await conn.execute("delete from leads_crm where phone = %s", (lead_ancora,))
        cur = await conn.execute(
            "select count(*) from legacy_chat_history where phone = %s", (lead_ancora,)
        )
        linha = await cur.fetchone()
    assert linha is not None
    assert linha[0] == 0, "a mensagem tem que cair junto quando o lead é apagado"


@pytest.mark.asyncio
async def test_legacy_chat_history_rejeita_ordem_duplicada_para_o_mesmo_phone(
    lead_ancora,
):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into legacy_chat_history (phone, ordem, papel, conteudo) "
            "values (%s, 1, 'human', 'oi')",
            (lead_ancora,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            async with conn.transaction():
                await conn.execute(
                    "insert into legacy_chat_history (phone, ordem, papel, conteudo) "
                    "values (%s, 1, 'ai', 'de novo')",
                    (lead_ancora,),
                )


@pytest.mark.asyncio
async def test_legacy_chat_history_rejeita_papel_fora_da_lista(lead_ancora):
    pool = await get_pool()
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            async with conn.transaction():
                await conn.execute(
                    "insert into legacy_chat_history (phone, ordem, papel, conteudo) "
                    "values (%s, 1, 'bot', 'oi')",
                    (lead_ancora,),
                )


@pytest.mark.asyncio
async def test_leads_descartados_aceita_phone_nulo():
    pool = await get_pool()
    motivo = "teste_015_phone_nulo"
    async with pool.connection() as conn:
        await conn.execute("delete from leads_descartados where motivo = %s", (motivo,))
        async with conn.transaction():
            await conn.execute(
                "insert into leads_descartados (phone_original, motivo, payload) "
                "values (null, %s, '{}')",
                (motivo,),
            )
            cur = await conn.execute(
                "select phone_original from leads_descartados where motivo = %s",
                (motivo,),
            )
            linha = await cur.fetchone()
            assert linha is not None
            assert linha[0] is None
            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_leads_descartados_aceita_string_null_literal():
    """A linha real da base legada cujo phone é o TEXTO 'null', não o valor
    NULL — não pode virar '55null' nem string vazia, tem que caber como
    está."""
    pool = await get_pool()
    motivo = "teste_015_string_null_literal"
    async with pool.connection() as conn:
        await conn.execute("delete from leads_descartados where motivo = %s", (motivo,))
        async with conn.transaction():
            await conn.execute(
                "insert into leads_descartados (phone_original, motivo, payload) "
                "values ('null', %s, '{}')",
                (motivo,),
            )
            cur = await conn.execute(
                "select phone_original from leads_descartados where motivo = %s",
                (motivo,),
            )
            linha = await cur.fetchone()
            assert linha is not None
            assert linha[0] == "null"
            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_leads_descartados_aceita_string_vazia():
    pool = await get_pool()
    motivo = "teste_015_string_vazia"
    async with pool.connection() as conn:
        await conn.execute("delete from leads_descartados where motivo = %s", (motivo,))
        async with conn.transaction():
            await conn.execute(
                "insert into leads_descartados (phone_original, motivo, payload) "
                "values ('', %s, '{}')",
                (motivo,),
            )
            cur = await conn.execute(
                "select phone_original from leads_descartados where motivo = %s",
                (motivo,),
            )
            linha = await cur.fetchone()
            assert linha is not None
            assert linha[0] == ""
            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_leads_descartados_rejeita_motivo_nulo():
    pool = await get_pool()
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            async with conn.transaction():
                await conn.execute(
                    "insert into leads_descartados (phone_original, motivo, payload) "
                    "values ('551100000000', null, '{}')"
                )


@pytest.mark.asyncio
async def test_leads_descartados_rejeita_payload_nulo():
    pool = await get_pool()
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            async with conn.transaction():
                await conn.execute(
                    "insert into leads_descartados (phone_original, motivo, payload) "
                    "values ('551100000000', 'telefone_ausente', null)"
                )
