"""Verifica a migração 015: o singleton não-canônico que a 014 sozinha
não alcança, e o CHECK ganhando a cláusula da forma local sem DDI.

A 015 já está aplicada no banco de dev — corrigi-la exigiria uma migração
nova, não editar o arquivo (mesma regra da 013/014). Os testes leem o SQL
direto do arquivo em disco, como `test_migracao_014.py` já faz.

Por que este arquivo existe: `014_uma_linha_por_pessoa.sql` só consolida
GRUPOS (`HAVING count(*) > 1`). Uma linha não-canônica SOZINHA — sem irmã
física no momento em que a migração roda — nunca é tocada pelo laço, e o
`ADD CONSTRAINT` da 014 estoura em cima dela. Reproduzido pela revisão com
o SQL exato da 014 contra uma base no formato da legada: 417 linhas
violavam o CHECK, 404 delas singletons que o laço de grupos nunca via.
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
    / "015_singleton_e_local_sem_ddi.sql"
)
_SQL = _MIGRACAO.read_text(encoding="utf-8")


def _trecho(inicio: str, fim: str) -> str:
    match = re.search(re.escape(inicio) + r".*?" + re.escape(fim), _SQL, re.DOTALL)
    assert match, (
        f"trecho não encontrado em 015_singleton_e_local_sem_ddi.sql: {inicio!r}"
    )
    return match.group(0)


def _sql_renomeacao_singleton() -> str:
    """Função de canonicalização + o UPDATE de renomeação — sem o CHECK."""
    return _trecho(
        "CREATE OR REPLACE FUNCTION _migracao_015_canonico",
        "DROP FUNCTION _migracao_015_canonico(TEXT);",
    )


def _sql_check() -> str:
    return _trecho(
        "ALTER TABLE leads_crm DROP CONSTRAINT IF EXISTS "
        "leads_crm_phone_canonico_check;",
        "AND phone !~ '^[0-9]{10,11}$'\n    );",
    )


async def _permitir_formas_malformadas(conn):
    """Derruba o CHECK (já com as três cláusulas, pós-015) dentro da
    transação do chamador — o estado em que a 014/015 reais encontram uma
    base com singleton malformado: CHECK ainda não criado."""
    await conn.execute(
        "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
    )


@pytest.mark.asyncio
async def test_singleton_nao_canonico_e_renomeado_para_forma_canonica():
    """O caso que a 014 sozinha não alcança: UMA linha com o 9º dígito, sem
    nenhuma irmã física — não é grupo (`count(*) = 1`), então o laço da 014
    nunca a toca. A 015 renomeia por fora do laço de grupos, sem fusão
    nenhuma (não há segunda linha para fundir com)."""
    com_9 = "5511997712350"
    canonico = "551197712350"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([com_9, canonico],)
            )
            await cur.execute(
                "insert into leads_crm (phone, name) values (%s, 'Fulano')",
                (com_9,),
            )

            await conn.execute(_sql_renomeacao_singleton().encode())

            cur2 = await conn.execute(
                "select phone, name from leads_crm where phone = any(%s)",
                ([com_9, canonico],),
            )
            linhas = await cur2.fetchall()

            assert len(linhas) == 1, "a linha não pode ter sido duplicada nem apagada"
            assert linhas[0][0] == canonico, (
                "o singleton tem que estar renomeado para a forma canônica — "
                "sem isto, o ADD CONSTRAINT seguinte estoura em cima dela"
            )
            assert linhas[0][1] == "Fulano", "renomeação não pode perder conteúdo"

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_singleton_ja_canonico_nao_e_tocado():
    """Controle negativo: uma linha já canônica não pode ser reescrita à
    toa pela 015 (o `WHERE phone <> canonico` existe por isso)."""
    canonico = "551197712351"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute("delete from leads_crm where phone = %s", (canonico,))
            await cur.execute(
                "insert into leads_crm (phone, followup_count) values (%s, 2)",
                (canonico,),
            )

            await conn.execute(_sql_renomeacao_singleton().encode())

            cur2 = await conn.execute(
                "select phone, followup_count from leads_crm where phone = %s",
                (canonico,),
            )
            linha = await cur2.fetchone()
            assert linha is not None
            assert linha == (canonico, 2)

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_check_rejeita_forma_local_sem_ddi_apos_015():
    """O furo que a 014 sozinha deixava aberto: `canonicalizar()` nunca
    produz forma local sem DDI para número brasileiro (sempre prefixa
    "55"), então nenhum caminho de escrita real do harness a gera hoje —
    mas é exatamente a classe de bug de importador que o CHECK existe para
    pegar (contrato da Fase 4, `docs/AGENTE_ELEVEC.md`). O CHECK já está em
    vigor nesta tabela (não é derrubado aqui)."""
    pool = await get_pool()
    formas_locais_sem_ddi = [
        "1187654399",  # 10 dígitos, sem o 9º
        "11987654399",  # 11 dígitos, com o 9º
    ]
    for phone in formas_locais_sem_ddi:
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor()
                with pytest.raises(psycopg.errors.CheckViolation):
                    await cur.execute(
                        "insert into leads_crm (phone) values (%s)", (phone,)
                    )
                raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_check_continua_aceitando_a_forma_canonica_apos_015():
    """Controle negativo: um CHECK bugado que rejeitasse tudo (inclusive a
    forma canônica de 12 dígitos) passaria no teste acima sem provar nada."""
    pool = await get_pool()
    phone = "551187654397"
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


def test_check_sql_da_015_cobre_a_terceira_clausula_no_arquivo():
    """Ancora o SQL da 015 em si — sem depender do banco — contra uma
    edição futura que afrouxe a cláusula nova em silêncio."""
    sql = _sql_check()
    assert "55[0-9]{2}9[0-9]{8}" in sql
    assert "^550" in sql
    assert "[0-9]{10,11}" in sql
