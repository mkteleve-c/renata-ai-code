"""`importar_leads` (Fase 4, Task 4) contra o Postgres real.

A camada de banco deste projeto não é monkeypatchada -- as validações que só
existem contra o Postgres (fases que não caem na gravação, `last_inbound_at`
intocado) e a escrita real de `leads_crm`/`leads_descartados` são testadas
aqui. A parte pura (soma fecha, CHECK, session_ids do histórico, marcação de
`reuniao_legada`) já está coberta em `tests/unit/test_migrar_supabase.py`.

**Fix round 1**: a seção "Reexecução contra produção" abaixo é o que faltava
na primeira versão deste arquivo. Provado contra este mesmo banco: migrar →
deixar produção acontecer (lead avança de fase, um handover pausa o agente,
`followup_count` sobe, a Task 5 injeta histórico) → reimportar os MESMOS
dados do Supabase revertia tudo isso em silêncio, porque o `ON CONFLICT DO
UPDATE` de `_SQL_UPSERT_LEAD` sobrescrevia cegamente e nenhuma validação lia
o estado ANTERIOR do banco para comparar. Os testes desta seção fixam esse
comportamento contra regressão.

Todos os telefones de teste usam o prefixo `5511990000` -- 12 dígitos,
forma canônica válida (não colide com nenhuma das três formas que o CHECK
de `leads_crm.phone` proíbe), e o `autouse` fixture `limpar` varre esse
prefixo inteiro antes e depois de cada teste.
"""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scripts.migrar_supabase import (
    _FONTE_MIGRACAO_DESCARTE,
    Descarte,
    LinhaFundida,
    LinhaOrigem,
    MigracaoAbortada,
    importar_leads,
    normalizar_telefone,
)
from whatsapp_langchain.shared.db import get_pool

_PREFIXO_TESTE = "5511990000"


@pytest.fixture(autouse=True)
async def limpar():
    async def apagar():
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "delete from leads_crm where phone like %s", (f"{_PREFIXO_TESTE}%",)
            )
            await conn.execute(
                "delete from leads_descartados where payload->>'fonte_migracao' = %s",
                (_FONTE_MIGRACAO_DESCARTE,),
            )

    await apagar()
    yield
    await apagar()


# --- Fábricas de dados de teste ---------------------------------------------


def _linha_origem(phone: str | None, *, phase: str | None = None) -> LinhaOrigem:
    return LinhaOrigem(
        phone=phone,
        phase=phase,
        created_at=None,
        last_interaction_at=None,
        pipedriveid=None,
        email=None,
        name=None,
        username=None,
        source=None,
        followup_count=0,
        agent_active=True,
        followup_active=True,
        agent_reactivate_at=None,
        metadata=None,
    )


def _fundida(
    canonico: str,
    *,
    phase: str | None = "iniciou_conversa",
    created_at: datetime | None = None,
    last_interaction_at: datetime | None = None,
    pipedriveid: str | None = None,
    email: str | None = None,
    name: str | None = None,
    username: str | None = None,
    source: str | None = None,
    followup_count: int = 0,
    agent_active: bool | None = True,
    followup_active: bool | None = True,
    agent_reactivate_at: datetime | None = None,
    metadata: dict | None = None,
) -> LinhaFundida:
    return LinhaFundida(
        canonico=canonico,
        phase=phase,
        created_at=created_at,
        last_interaction_at=last_interaction_at,
        pipedriveid=pipedriveid,
        email=email,
        name=name,
        username=username,
        source=source,
        followup_count=followup_count,
        agent_active=agent_active,
        followup_active=followup_active,
        agent_reactivate_at=agent_reactivate_at,
        metadata=metadata or {},
        telefones_origem=(canonico,),
        origem_por_campo={},
        mudou_phase=False,
        mudou_agent_active=False,
    )


def _descarte(phone_origem: str | None, motivo: str = "telefone_ausente") -> Descarte:
    return Descarte(phone_origem, motivo, _linha_origem(phone_origem))


async def _ler_lead(phone: str) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select phase, pipedriveid, email, name, followup_count, "
            "       agent_active, metadata, last_inbound_at, last_interaction_at, "
            "       followup_active, agent_reactivate_at, username, created_at "
            "from leads_crm where phone = %s",
            (phone,),
        )
        linha = await cur.fetchone()
    if linha is None:
        return None
    return {
        "phase": linha[0],
        "pipedriveid": linha[1],
        "email": linha[2],
        "name": linha[3],
        "followup_count": linha[4],
        "agent_active": linha[5],
        "metadata": linha[6],
        "last_inbound_at": linha[7],
        "last_interaction_at": linha[8],
        "followup_active": linha[9],
        "agent_reactivate_at": linha[10],
        "username": linha[11],
        "created_at": linha[12],
    }


async def _contar_leads_crm(phone: str) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (phone,)
        )
        linha = await cur.fetchone()
    assert linha is not None
    return linha[0]


async def _contar_descartes() -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_descartados"
            " where payload->>'fonte_migracao' = %s",
            (_FONTE_MIGRACAO_DESCARTE,),
        )
        linha = await cur.fetchone()
    assert linha is not None
    return linha[0]


_MOMENTO = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

_P1 = f"{_PREFIXO_TESTE}01"
_P2 = f"{_PREFIXO_TESTE}02"


# --- Escrita real -------------------------------------------------------


async def test_importa_grava_lead_com_os_campos_computados():
    fundida = _fundida(
        _P1,
        phase="qualificado",
        pipedriveid="DEAL-1",
        email="ana@exemplo.com",
        name="Ana",
        followup_count=3,
        created_at=_MOMENTO,
        last_interaction_at=_MOMENTO + timedelta(hours=1),
    )
    grupos = {_P1: [_linha_origem(_P1, phase="qualificado")]}

    resultado = await importar_leads(
        await get_pool(), 1, grupos, {_P1: fundida}, [], agora=_MOMENTO
    )

    assert resultado.leads_gravados == 1
    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["phase"] == "qualificado"
    assert lido["pipedriveid"] == "DEAL-1"
    assert lido["email"] == "ana@exemplo.com"
    assert lido["name"] == "Ana"
    assert lido["followup_count"] == 3


async def test_importa_grava_descarte_com_motivo_e_payload():
    grupos: dict[str, list[LinhaOrigem]] = {}
    descartes = [_descarte("null", "telefone_ausente")]

    resultado = await importar_leads(await get_pool(), 1, grupos, {}, descartes)

    assert resultado.descartes_gravados == 1
    assert await _contar_descartes() == 1


# --- O contrato herdado da Fase 3 -------------------------------------------


async def test_importa_nunca_escreve_last_inbound_at():
    fundida = _fundida(_P1, phase="iniciou_conversa")
    grupos = {_P1: [_linha_origem(_P1, phase="iniciou_conversa")]}

    await importar_leads(
        await get_pool(), 1, grupos, {_P1: fundida}, [], agora=_MOMENTO
    )

    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["last_inbound_at"] is None


# --- Erro 2: reuniao_legada --------------------------------------------------


async def test_importa_marca_reuniao_legada_para_agendou_sessao():
    fundida = _fundida(_P1, phase="agendou_sessao")
    grupos = {_P1: [_linha_origem(_P1, phase="agendou_sessao")]}

    resultado = await importar_leads(
        await get_pool(), 1, grupos, {_P1: fundida}, [], agora=_MOMENTO
    )

    assert resultado.reunioes_legadas_marcadas == 1
    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["metadata"]["reuniao_legada"] is True


async def test_importa_nao_marca_reuniao_legada_fora_de_agendou_sessao():
    fundida = _fundida(_P1, phase="qualificado")
    grupos = {_P1: [_linha_origem(_P1, phase="qualificado")]}

    resultado = await importar_leads(
        await get_pool(), 1, grupos, {_P1: fundida}, [], agora=_MOMENTO
    )

    assert resultado.reunioes_legadas_marcadas == 0
    lido = await _ler_lead(_P1)
    assert lido is not None
    assert "reuniao_legada" not in lido["metadata"]


# --- Validações bloqueantes abortam ANTES de gravar --------------------------


async def test_soma_nao_fecha_aborta_sem_gravar_nada():
    fundida = _fundida(_P1)
    grupos = {_P1: [_linha_origem(_P1)]}

    with pytest.raises(MigracaoAbortada, match="soma não fecha"):
        # origem diz 2, só existe 1 linha migrada e nenhum descarte.
        await importar_leads(await get_pool(), 2, grupos, {_P1: fundida}, [])

    assert await _contar_leads_crm(_P1) == 0


async def test_canonico_fora_do_check_aborta_sem_gravar_nada():
    canonico_invalido = "1234567890"  # 10 dígitos -- colide com forma local BR
    fundida = _fundida(canonico_invalido)
    grupos = {canonico_invalido: [_linha_origem(canonico_invalido)]}

    with pytest.raises(MigracaoAbortada, match="CHECK"):
        await importar_leads(
            await get_pool(), 1, grupos, {canonico_invalido: fundida}, []
        )

    assert await _contar_leads_crm(canonico_invalido) == 0


async def test_session_id_orfao_aborta_sem_gravar_nada():
    fundida = _fundida(_P1)
    grupos = {_P1: [_linha_origem(_P1)]}

    with pytest.raises(MigracaoAbortada, match="session_id"):
        await importar_leads(
            await get_pool(),
            1,
            grupos,
            {_P1: fundida},
            [],
            session_ids_historico=["551100009999"],  # não casa com nenhum migrado
        )

    assert await _contar_leads_crm(_P1) == 0


async def test_reimportar_lead_com_last_inbound_at_ja_preenchido_nao_aborta():
    """Fix round 1: a versão anterior desta validação abortava sempre que
    QUALQUER lead importado já tinha `last_inbound_at` preenchido -- o que
    soa como o contrato certo, mas na prática bloqueava uma reexecução
    LEGÍTIMA contra uma base viva. `_P1` chega com `last_inbound_at` real
    (um humano respondeu depois da primeira importação); reimportar os
    mesmos dados do Supabase não pode falhar por causa disso, e o valor
    tem que sobreviver intocado -- é exatamente esse o contrato: o
    IMPORTADOR nunca escreve nesta coluna, não que a coluna tenha que
    estar vazia.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, last_inbound_at) "
            "values (%s, 'iniciou_conversa', now() - interval '2 hours')",
            (_P1,),
        )
    antes = await _ler_lead(_P1)
    assert antes is not None
    original = antes["last_inbound_at"]

    fundida = _fundida(_P1, phase="qualificado")
    grupos = {_P1: [_linha_origem(_P1, phase="qualificado")]}

    resultado = await importar_leads(
        pool, 1, grupos, {_P1: fundida}, [], agora=_MOMENTO
    )

    assert resultado.leads_gravados == 1
    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["last_inbound_at"] == original


# --- Reexecução contra produção (Crítico 1 e 2 do fix round 1) -------------
#
# `_P1` acumula "o que produção teria feito" entre a primeira importação e
# a reexecução; `fundida_reimportada` simula o MESMO snapshot do Supabase
# sendo reimportado -- sem saber nada do que aconteceu em produção. O
# `ON CONFLICT DO UPDATE` de `_SQL_UPSERT_LEAD` é quem tem que proteger
# cada campo -- estes testes leem de volta do banco, não confiam no valor
# em memória que foi passado para `importar_leads`.


async def test_reexecucao_nao_religa_lead_pausado():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm"
            " (phone, phase, agent_active, followup_active, agent_reactivate_at)"
            " values (%s, 'qualificado', false, false, %s)",
            (_P1, _MOMENTO + timedelta(days=1)),
        )

    # A linha reimportada do Supabase nunca soube do handover -- chega com
    # agent_active/followup_active ativos, como qualquer lead legado.
    fundida = _fundida(
        _P1, phase="qualificado", agent_active=True, followup_active=True
    )
    grupos = {_P1: [_linha_origem(_P1, phase="qualificado")]}

    await importar_leads(pool, 1, grupos, {_P1: fundida}, [], agora=_MOMENTO)

    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["agent_active"] is False, "false vence -- não pode religar o agente"
    assert lido["followup_active"] is False
    assert lido["agent_reactivate_at"] == _MOMENTO + timedelta(days=1)


async def test_reexecucao_nao_rebobina_o_funil():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, 'agendou_sessao')",
            (_P1,),
        )

    # O snapshot do Supabase é mais antigo que a reunião marcada em produção.
    fundida = _fundida(_P1, phase="formulario_preenchido")
    grupos = {_P1: [_linha_origem(_P1, phase="formulario_preenchido")]}

    await importar_leads(pool, 1, grupos, {_P1: fundida}, [], agora=_MOMENTO)

    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["phase"] == "agendou_sessao", "fase mais avançada em produção vence"


async def test_reexecucao_nao_zera_followup_count():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, followup_count)"
            " values (%s, 'iniciou_conversa', 3)",
            (_P1,),
        )

    fundida = _fundida(_P1, phase="iniciou_conversa", followup_count=0)
    grupos = {_P1: [_linha_origem(_P1, phase="iniciou_conversa")]}

    await importar_leads(pool, 1, grupos, {_P1: fundida}, [], agora=_MOMENTO)

    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["followup_count"] == 3, "a escada já percorrida não pode zerar"


async def test_reexecucao_preserva_chave_de_metadata_gravada_em_producao():
    """`historico_injetado` (Task 5) só existe em produção -- a origem do
    Supabase nunca tem essa chave. Uma reimportação que sobrescrevesse
    `metadata` inteiro apagaria a marca e reabriria a injeção de histórico.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, metadata)"
            " values (%s, 'iniciou_conversa', %s)",
            (_P1, Jsonb({"historico_injetado": True})),
        )

    fundida = _fundida(_P1, phase="iniciou_conversa", metadata={"origem": "supabase"})
    grupos = {_P1: [_linha_origem(_P1, phase="iniciou_conversa")]}

    await importar_leads(pool, 1, grupos, {_P1: fundida}, [], agora=_MOMENTO)

    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["metadata"]["historico_injetado"] is True
    assert lido["metadata"]["origem"] == "supabase"


async def test_reexecucao_preserva_email_gravado_em_producao():
    """`email` capturado por `update_crm` depois da primeira importação não
    pode ser revertido para o valor (potencialmente vazio ou desatualizado)
    do snapshot do Supabase.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, email)"
            " values (%s, 'qualificado', 'real@producao.com')",
            (_P1,),
        )

    fundida = _fundida(_P1, phase="qualificado", email="velho@supabase.com")
    grupos = {_P1: [_linha_origem(_P1, phase="qualificado")]}

    await importar_leads(pool, 1, grupos, {_P1: fundida}, [], agora=_MOMENTO)

    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["email"] == "real@producao.com"


async def test_reexecucao_preenche_email_quando_producao_nao_tinha():
    """O outro lado do `coalesce`: quando produção NÃO tem o campo, o valor
    reimportado ainda precisa entrar -- a proteção não pode virar "nunca
    aceita nada do Supabase de novo"."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, email)"
            " values (%s, 'qualificado', null)",
            (_P1,),
        )

    fundida = _fundida(_P1, phase="qualificado", email="do_supabase@exemplo.com")
    grupos = {_P1: [_linha_origem(_P1, phase="qualificado")]}

    await importar_leads(pool, 1, grupos, {_P1: fundida}, [], agora=_MOMENTO)

    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["email"] == "do_supabase@exemplo.com"


# --- Idempotência -------------------------------------------------------


async def test_idempotencia_leads_nao_duplicam_nem_falham():
    fundida = _fundida(_P1, phase="qualificado", email="ana@exemplo.com")
    grupos = {_P1: [_linha_origem(_P1, phase="qualificado")]}

    primeira = await importar_leads(
        await get_pool(), 1, grupos, {_P1: fundida}, [], agora=_MOMENTO
    )
    segunda = await importar_leads(
        await get_pool(), 1, grupos, {_P1: fundida}, [], agora=_MOMENTO
    )

    assert primeira.leads_gravados == segunda.leads_gravados == 1
    assert await _contar_leads_crm(_P1) == 1
    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["email"] == "ana@exemplo.com"


async def test_idempotencia_descartes_nao_duplicam_nem_acumulam():
    descartes = [
        _descarte("null", "telefone_ausente"),
        _descarte("", "telefone_ausente"),
    ]

    await importar_leads(await get_pool(), 2, {}, {}, descartes)
    await importar_leads(await get_pool(), 2, {}, {}, descartes)

    # Duas execuções da MESMA fusão -- o total de descartes marcados com
    # esta migração continua 2, nunca 4: o DELETE por `fonte_migracao`
    # roda antes de cada reinserção.
    assert await _contar_descartes() == 2


# --- O CHECK real do banco, sem parsing de arquivo (Importante 3) ----------
#
# `tests/unit/test_migrar_supabase.py::test_saida_sempre_satisfaz_o_check_do_
# banco` extrai a regra de `007_elevec.sql`/`014_uma_linha_por_pessoa.sql` e
# reimplementa a checagem em regex Python -- rápido e sem banco, mas cego a
# uma migração 016 futura que altere a constraint (o teste seguiria lendo os
# dois arquivos fixos e passaria verde contra uma regra que já não é a que
# está em vigor). Este teste não extrai nada: pergunta para o Postgres
# REAL, com um INSERT de sacrifício sempre desfeito -- sobrevive a qualquer
# migração futura sem precisar ser editado.


async def _aceito_pelo_check_real(conn, canonico: str) -> bool:
    try:
        async with conn.transaction():
            await conn.execute("insert into leads_crm (phone) values (%s)", (canonico,))
            raise psycopg.Rollback()
    except psycopg.errors.CheckViolation:
        return False
    return True


_FORMAS_REPRESENTATIVAS = [
    "+5511987654321",
    "5511987654321",
    "551187654321",
    "11987654321",
    "1187654321",
    "55011987654321",
    "(11) 98765-4321",
    "258864038352",  # Moçambique
    "351914355881",  # Portugal
    "+14242123771",  # EUA -- normalizar_telefone já descarta antes daqui
    "5511988887777@lid",
    "120363012345678901@g.us",
    "null",
    "",
    "519985344",
    "5511666666665",
]


async def test_normalizar_telefone_bate_com_o_check_real_do_banco():
    """Pergunta ao Postgres, não reimplementa a regra -- ver o cabeçalho
    da seção acima. Qualquer canônico que `normalizar_telefone` devolve
    tem que ser aceito pelo CHECK de verdade, hoje e depois de uma 016."""
    pool = await get_pool()
    canonicos_esperados = {
        c
        for c in (normalizar_telefone(b).canonico for b in _FORMAS_REPRESENTATIVAS)
        if c is not None
    }
    async with pool.connection() as conn:
        # Defensivo: se algum desses canônicos já existir por acaso (outro
        # teste da suíte usa os mesmos números representativos e pode ter
        # deixado uma linha para trás num cenário de falha a meio), o
        # INSERT de sacrifício bateria em UniqueViolation em vez de
        # exercitar o CHECK -- limpa antes de começar, para este teste só
        # depender do próprio estado que ele cria e desfaz.
        await conn.execute(
            "delete from leads_crm where phone = any(%s)",
            (list(canonicos_esperados),),
        )
        await conn.commit()

    algum_canonico_produzido = False
    async with pool.connection() as conn:
        for bruto in _FORMAS_REPRESENTATIVAS:
            resultado = normalizar_telefone(bruto)
            if resultado.canonico is None:
                continue
            algum_canonico_produzido = True
            aceito = await _aceito_pelo_check_real(conn, resultado.canonico)
            assert aceito, (
                f"{bruto!r} normalizou para {resultado.canonico!r}, que o "
                "CHECK real de leads_crm.phone recusou"
            )
            # Sacrifício desfeito -- confirma que não sobrou lixo entre
            # uma iteração e a próxima (o INSERT usaria a mesma PK de novo
            # se dois brutos convergissem para o mesmo canônico).
            assert await _contar_leads_crm(resultado.canonico) == 0

    assert algum_canonico_produzido
