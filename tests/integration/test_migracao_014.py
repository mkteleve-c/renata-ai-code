"""Verifica a migração 014: reparo de canonicalização (grupos, singletons e
malformados irrecuperáveis) seguido do CHECK de três cláusulas.

A 014 já está aplicada no banco de dev (o runner pula por nome de arquivo) —
corrigi-la de novo exigiria uma migração nova, não editar o arquivo outra
vez. Por isso, como `test_migracao_013.py`, os testes leem o SQL direto do
arquivo em disco em vez de reimplementar a lógica aqui.

O CHECK novo (`leads_crm_phone_canonico_check`) já está em vigor nesta
tabela — os testes de reparo precisam derrubá-lo dentro da própria transação
(sempre revertida no final) para poder inserir as formas malformadas que a
migração existe para resolver. É exatamente o estado em que a 014 real
encontra uma base com dados legados: linhas malformadas, CHECK ainda não
criado.
"""

import re
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

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


def _sql_reparo() -> str:
    """Função de canonicalização + etapa 1 (excisão) + etapa 2 (grupos) +
    etapa 3 (singleton) — sem o CHECK (etapa 4)."""
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
    que a 014 real encontra uma base com dados legados: linhas malformadas,
    CHECK ainda não criado. Nunca commitada; some no rollback do teste."""
    await conn.execute(
        "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
    )


async def _reparar_dentro_de_transacao(conn, telefones_de_teste: list[str]):
    """Roda só o reparo do arquivo real (etapas 1-3; o CHECK precisa já ter
    sido derrubado por `_permitir_formas_malformadas`) e devolve as linhas
    remanescentes para os telefones dados — tudo dentro da transação do
    chamador, que é sempre revertida."""
    await conn.execute(_sql_reparo().encode())
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

            linhas = await _reparar_dentro_de_transacao(conn, [sem_9, com_9])

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

            linhas = await _reparar_dentro_de_transacao(
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

            linhas = await _reparar_dentro_de_transacao(conn, [sem_9, com_9])

            assert len(linhas) == 1
            _, _, _, _, _, agent_active, followup_active, _ = linhas[0]
            assert agent_active is False, (
                "o lado pausado (mesmo mais antigo) tem que vencer a fusão"
            )
            assert followup_active is False

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_agent_reactivate_at_do_vencedor_nao_e_coalescido_com_o_obsoleto():
    """Espelho SQL de `test_fundir_agent_reactivate_at_...` (unit, sobre
    `leads.py::_fundir`) — a mesma regra, reimplementada aqui, precisa da
    própria prova: sem teste dedicado, `v_agent_reactivate_at :=
    COALESCE(linha.agent_reactivate_at, v_agent_reactivate_at)` no lugar da
    atribuição direta passaria limpo em todo o resto da suíte.

    A linha mais antiga (processada primeiro) fica pausada com um
    `agent_reactivate_at` futuro obsoleto; a mais recente TAMBÉM pausada
    (mesmo estado — desempate por recência) tem `agent_reactivate_at=NULL`.
    Como as duas são `agent_active=false`, a mais recente vence o desempate
    e o resultado tem que ser NULL — `NULL` ali significa "sem reativação
    agendada", não "sem dado a preencher"."""
    sem_9 = "551197712346"
    com_9 = "5511997712346"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([sem_9, com_9],)
            )
            await cur.execute(
                "insert into leads_crm (phone, agent_active, agent_reactivate_at, "
                "last_interaction_at) values "
                "(%s, false, now() + interval '3 days', now() - interval '3 hours')",
                (sem_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, agent_active, agent_reactivate_at, "
                "last_interaction_at) values "
                "(%s, false, null, now() - interval '10 minutes')",
                (com_9,),
            )

            await conn.execute(_sql_reparo().encode())

            cur2 = await conn.execute(
                "select agent_reactivate_at from leads_crm where phone = any(%s)",
                ([sem_9, com_9],),
            )
            linhas = await cur2.fetchall()
            assert len(linhas) == 1
            assert linhas[0][0] is None, "coalesce ressuscitaria o valor obsoleto"

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_agendou_sessao_vence_a_fusao_mesmo_vindo_da_linha_mais_antiga():
    """Espelho SQL de `test_fundir_phase_e_a_mais_avancada_...` (unit, sobre
    `leads.py::_fundir`/`_FASE_RANK`) — sem teste dedicado aqui, o rank de
    `agendou_sessao` no `CASE` do arquivo pode cair silenciosamente (ex.:
    de 5 para 0) sem quebrar nenhum outro teste de fusão desta suíte. Se
    isso acontecer de verdade, um grupo {agendou_sessao, iniciou_conversa}
    consolidaria para `iniciou_conversa` — que NÃO é fase terminal — e a
    régua volta a perseguir quem já agendou reunião."""
    sem_9 = "551197712347"
    com_9 = "5511997712347"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([sem_9, com_9],)
            )
            # agendou_sessao vem da linha MENOS recente -- de propósito:
            # phase precisa vencer por RANK, não por last_interaction_at.
            await cur.execute(
                "insert into leads_crm (phone, phase, last_interaction_at) "
                "values (%s, 'agendou_sessao', now() - interval '30 days')",
                (sem_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, phase, last_interaction_at) "
                "values (%s, 'iniciou_conversa', now() - interval '5 minutes')",
                (com_9,),
            )

            linhas = await _reparar_dentro_de_transacao(conn, [sem_9, com_9])

            assert len(linhas) == 1
            assert linhas[0][2] == "agendou_sessao", (
                "agendou_sessao tem que vencer por rank, mesmo sendo a "
                "linha bem mais antiga"
            )

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

            linhas = await _reparar_dentro_de_transacao(conn, [sem_9, com_9])

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

            linhas = await _reparar_dentro_de_transacao(conn, [sem_9, com_9])

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
async def test_metadata_da_linha_mais_recente_tem_precedencia_por_chave():
    """Chave em comum entre as duas linhas: o valor do lado com
    `last_interaction_at` mais recente vence — mesma regra de
    `leads.py::_fundir_metadata` (`{**base, **topo}`, "topo" é o mais
    recente). Sem teste dedicado, inverter a ordem do `||` no arquivo
    (`v_metadata || linha.metadata` → `linha.metadata || v_metadata`) não
    quebrava nenhum teste existente — os testes de par/trio só checavam
    `linhas_fundidas`, não conflito de chave."""
    sem_9 = "551197712345"
    com_9 = "5511997712345"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)", ([sem_9, com_9],)
            )
            await cur.execute(
                "insert into leads_crm (phone, metadata, last_interaction_at) "
                "values (%s, %s, now() - interval '3 hours')",
                (sem_9, Jsonb({"utm": "antiga", "origem": "linkedin"})),
            )
            await cur.execute(
                "insert into leads_crm (phone, metadata, last_interaction_at) "
                "values (%s, %s, now() - interval '10 minutes')",
                (com_9, Jsonb({"utm": "nova"})),
            )

            linhas = await _reparar_dentro_de_transacao(conn, [sem_9, com_9])

            assert len(linhas) == 1
            metadata = linhas[0][7]
            assert metadata["utm"] == "nova", "a linha mais recente vence por chave"
            assert metadata["origem"] == "linkedin", "chave só da antiga sobrevive"

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_singleton_com_forma_local_sem_ddi_e_reparado():
    """O Crítico da segunda rodada: a primeira versão do reparo não
    prefixava "55" nos ramos LOCAL (sem DDI) — `"1187654321"` (10 dígitos)
    ficava inalterado e violava a cláusula nova do CHECK em vez de virar
    `"551187654321"`. Singleton — sem irmã física — então é a etapa 3
    (renomeação pura) que precisa pegar isto, não a etapa 2 (grupos)."""
    local_sem_9 = "1187654321"
    canonico = "551187654321"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)",
                ([local_sem_9, canonico],),
            )
            await cur.execute(
                "insert into leads_crm (phone, name) values (%s, 'Fulano')",
                (local_sem_9,),
            )

            linhas = await _reparar_dentro_de_transacao(conn, [local_sem_9, canonico])

            assert len(linhas) == 1
            assert linhas[0][0] == canonico, (
                "forma local sem DDI (10 dígitos) tem que virar canônica "
                "(55 + DDD + 8 dígitos), não ficar como está"
            )
            assert linhas[0][1] == "Fulano"

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_par_de_formas_locais_que_colidem_e_fundido_sem_erro_de_pk():
    """A colisão que a primeira versão do reparo não previa: `"1187654321"`
    (LOCAL_SEM_9) e `"11987654321"` (LOCAL_COM_9) canonicalizam para a
    MESMA identidade (`"551187654321"`) — se a etapa 2 (grupos) não
    reconhecesse isso, a etapa 3 tentaria renomear as duas para o mesmo
    `phone` e a segunda renomeação estouraria violação de chave primária.
    Como a etapa 2 usa a MESMA função de canonicalização corrigida, as duas
    linhas são um GRUPO (`count(*) = 2`) e passam pela fusão normal, não por
    uma renomeação cega."""
    local_sem_9 = "1187654321"
    local_com_9 = "11987654321"
    canonico = "551187654321"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute(
                "delete from leads_crm where phone = any(%s)",
                ([local_sem_9, local_com_9, canonico],),
            )
            await cur.execute(
                "insert into leads_crm (phone, followup_count, last_interaction_at) "
                "values (%s, 1, now() - interval '2 hours')",
                (local_sem_9,),
            )
            await cur.execute(
                "insert into leads_crm (phone, followup_count, last_interaction_at) "
                "values (%s, 0, now() - interval '10 minutes')",
                (local_com_9,),
            )

            linhas = await _reparar_dentro_de_transacao(
                conn, [local_sem_9, local_com_9, canonico]
            )

            assert len(linhas) == 1, (
                "as duas formas locais colidem na mesma identidade e têm "
                "que virar uma linha só, não duas nem um erro de PK"
            )
            assert linhas[0][0] == canonico
            assert linhas[0][3] == 1, "max(followup_count) do grupo"

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_forma_nao_canonicalizavel_e_descartada_nao_bloqueia_o_check():
    """`"550117654321"` é o outro lado do mesmo Crítico: zero de tronco
    colado numa forma que, depois de removido, ainda não fecha com nenhum
    padrão válido (11 dígitos, sem o "9" na posição certa para LOCAL_COM_9).
    `canonicalizar()` devolve `None` para isto — a migração precisa fazer o
    mesmo (excisar para `leads_descartados`), nunca gravar um valor
    inventado que o CHECK rejeitaria de qualquer forma."""
    malformado = "550117654321"
    pool = await get_pool()

    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            await _permitir_formas_malformadas(conn)
            await cur.execute("delete from leads_crm where phone = %s", (malformado,))
            await cur.execute(
                "delete from leads_descartados where phone_original = %s",
                (malformado,),
            )
            await cur.execute(
                "insert into leads_crm (phone, name) values (%s, 'Fulano')",
                (malformado,),
            )

            await conn.execute(_sql_reparo().encode())

            cur2 = await conn.execute(
                "select count(*) from leads_crm where phone = %s", (malformado,)
            )
            linha_count = await cur2.fetchone()
            assert linha_count is not None
            assert linha_count[0] == 0, "não pode sobrar em leads_crm sob forma nenhuma"

            cur3 = await conn.execute(
                "select motivo, payload from leads_descartados "
                "where phone_original = %s",
                (malformado,),
            )
            linha = await cur3.fetchone()
            assert linha is not None, (
                "tem que ficar registrado em leads_descartados, não só sumir"
            )
            assert linha[0] == "telefone_nao_canonicalizavel_migracao_014"
            assert linha[1]["name"] == "Fulano"

            raise psycopg.Rollback()


@pytest.mark.asyncio
async def test_check_rejeita_as_tres_formas_proibidas_depois_de_aplicado():
    """O CHECK já está em vigor nesta tabela (não é derrubado aqui, ao
    contrário dos testes de reparo acima) — prova as três formas que a
    migração existe para nunca mais deixar entrar: o 9º dígito puro, o
    zero de tronco puro, e a forma local sem DDI."""
    pool = await get_pool()
    formas_proibidas = [
        "5511987654399",  # com o 9º dígito
        "5501187654399",  # zero de tronco
        "1187654399",  # forma local sem DDI (10 dígitos)
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


@pytest.mark.asyncio
async def test_check_ddl_do_arquivo_e_executado_e_rejeita_forma_local_sem_ddi():
    """Sem este teste, `_sql_check()` só era consumida por `assert
    <substring> in sql` (ver `test_check_sql_cobre_as_tres_formas_no_arquivo`
    abaixo) — nenhum teste rodava o DDL de verdade contra o banco.
    Reproduzido pela revisão: mutar `phone !~ '^550'` para `'^5500'` no
    ARQUIVO passava limpo em toda a suíte, porque nenhum teste recriava o
    CHECK a partir do texto do arquivo.

    Aqui o CHECK vivo é derrubado e RECRIADO com o DDL extraído do arquivo
    (`_sql_check()`, não uma cópia escrita à mão) — tudo dentro da mesma
    transação, revertida no final. Um `ALTER TABLE ADD CONSTRAINT` é DDL
    transacional em Postgres, então isto é seguro: a constraint criada aqui
    nunca sobrevive além do rollback deste teste.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
            )
            await conn.execute(_sql_check().encode())

            for phone in ("5511987654399", "5501187654399", "1187654399"):
                # `async with conn.transaction()` aninhado vira SAVEPOINT no
                # psycopg3 — a violação reverte só até aqui, não a transação
                # inteira, então o laço pode testar as três formas na MESMA
                # transação externa (que carrega o CHECK recriado do arquivo).
                with pytest.raises(psycopg.errors.CheckViolation):
                    async with conn.transaction():
                        await conn.execute(
                            "insert into leads_crm (phone) values (%s)", (phone,)
                        )

            raise psycopg.Rollback()


def test_check_sql_cobre_as_tres_formas_no_arquivo():
    """Ancora o SQL do CHECK em si — sem depender do banco — contra uma
    edição futura que afrouxe silenciosamente uma das três cláusulas."""
    sql = _sql_check()
    assert "55[0-9]{2}9[0-9]{8}" in sql
    assert "^550" in sql
    assert "[0-9]{10,11}" in sql
