"""`importar_leads` (Fase 4, Task 4) contra o Postgres real.

A camada de banco deste projeto não é monkeypatchada -- as validações que só
existem contra o Postgres (`last_inbound_at` nunca escrito, fases que não
retrocedem na gravação) e a escrita real de `leads_crm`/`leads_descartados`
são testadas aqui. A parte pura (soma fecha, CHECK, session_ids do
histórico, marcação de `reuniao_legada`) já está coberta em
`tests/unit/test_migrar_supabase.py` -- este arquivo confirma que as MESMAS
validações, quando conectadas à escrita real, realmente abortam a transação
inteira, não só devolvem um erro sem desfazer o que já tinha sido gravado.

Todos os telefones de teste usam o prefixo `5511990000` -- 12 dígitos,
forma canônica válida (não colide com nenhuma das três formas que o CHECK
de `leads_crm.phone` proíbe), e o `autouse` fixture `limpar` varre esse
prefixo inteiro antes e depois de cada teste.
"""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.migrar_supabase import (
    _FONTE_MIGRACAO_DESCARTE,
    Descarte,
    LinhaFundida,
    LinhaOrigem,
    MigracaoAbortada,
    importar_leads,
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
            "       agent_active, metadata, last_inbound_at, last_interaction_at "
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


async def test_lead_com_last_inbound_at_ja_preenchido_aborta_e_desfaz_tudo():
    """O contrato herdado da Fase 3, verificado contra escrita real.

    `_P1` já chegou com `last_inbound_at` preenchido -- simula um lead que
    algum outro caminho (bug futuro no INSERT, ou um lead que já existia em
    produção) contaminou. `_P2` é uma segunda linha, limpa, na MESMA
    chamada. A validação tem que abortar a chamada inteira -- inclusive
    desfazendo `_P2`, que não tinha nada de errado -- porque
    `importar_leads` não tem conceito de "gravar parcialmente".
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, last_inbound_at) "
            "values (%s, 'formulario_preenchido', now())",
            (_P1,),
        )

    grupos = {
        _P1: [_linha_origem(_P1, phase="qualificado")],
        _P2: [_linha_origem(_P2, phase="qualificado")],
    }
    fundidas = {
        _P1: _fundida(_P1, phase="qualificado"),
        _P2: _fundida(_P2, phase="qualificado"),
    }

    with pytest.raises(MigracaoAbortada, match="last_inbound_at"):
        await importar_leads(pool, 2, grupos, fundidas, [], agora=_MOMENTO)

    # _P2 nunca deveria ter sobrevivido -- a transação inteira desfez.
    assert await _contar_leads_crm(_P2) == 0
    # _P1 continua exatamente como entrou -- não foi sobrescrito pela fusão
    # (que diria "qualificado") antes do rollback.
    lido = await _ler_lead(_P1)
    assert lido is not None
    assert lido["phase"] == "formulario_preenchido"


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
