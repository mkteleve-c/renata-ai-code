"""O desfecho por faixa é garantido por `if`, não pedido ao modelo.

Medido com LLM real: pedindo no prompt, o modelo grava `"20 mil"` cru numa
rodada e a faixa certa noutra; chama `human_handover` às vezes. Duas
consequências dependem disso e as duas mexem com gente:

- abaixo de R$ 5 mil, a reunião é cancelada;
- acima de R$ 25 mil, o lead vai para o Silvio.

Regra de negócio decidida por prompt é sugestão. Aqui ela é `if`.

O prompt continua responsável por **coletar** o número — que é o que modelo
faz bem. `update_crm` normaliza, decide e executa.
"""

from typing import Any

import pytest

from whatsapp_langchain.agents.catalog.elevec_sdr import faixas
from whatsapp_langchain.agents.catalog.elevec_sdr.tools import crm
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "+5511977771111"
CANONICO = "551177771111"


@pytest.fixture
def turno(monkeypatch):
    monkeypatch.setattr(crm, "telefone_do_turno", lambda: TELEFONE)


@pytest.fixture
def sem_pipedrive(monkeypatch):
    async def _nada(*_a, **_k):
        return True, "sem pipedrive no teste"

    monkeypatch.setattr(crm, "mover_card", _nada)


@pytest.fixture
def espioes(monkeypatch):
    """Substitui as duas consequências por espiões — o que importa aqui é
    SE foram acionadas, não o efeito delas (coberto nos testes das tools)."""
    registro: dict[str, Any] = {"cancelou": None, "handover": None}

    async def _cancelar(telefone: str) -> bool:
        registro["cancelou"] = telefone
        return True

    async def _avisar(motivo: str) -> bool:
        registro["handover"] = motivo
        return True

    monkeypatch.setattr(crm, "cancelar_reuniao_do_lead", _cancelar)
    monkeypatch.setattr(crm, "acionar_handover", _avisar)
    return registro


@pytest.fixture
async def lead():
    pool = await get_pool()

    async def limpa():
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = %s", (CANONICO,))

    await limpa()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, name, phase, google_event_id) "
            "values (%s, 'Lead', 'qualificado'::lead_phase, 'evt_existente')",
            (CANONICO,),
        )
    yield
    await limpa()


async def _estado() -> tuple[str, str | None, str | None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select phase, faturamento_mensal, google_event_id "
            "from leads_crm where phone = %s",
            (CANONICO,),
        )
        return await cur.fetchone()


@pytest.mark.parametrize(
    ("fala", "faixa_esperada"),
    [
        ("uns 6 mil por mês", faixas.DE_5K_A_8K),
        ("gira em torno de 20 mil", faixas.DE_15K_A_25K),
        # 8.000 é o piso da faixa de cima (teto exclusivo no meio da escala)
        ("R$ 8.000", faixas.DE_8K_A_15K),
        ("R$ 7.999", faixas.DE_5K_A_8K),
    ],
)
async def test_dentro_da_faixa_normaliza_e_nao_dispara_nada(
    turno, sem_pipedrive, espioes, lead, fala, faixa_esperada
):
    """O texto cru do lead nunca chega ao banco — vira faixa."""
    await crm.update_crm.ainvoke(
        {"phase": "agendou_sessao", "faturamento_mensal": fala}
    )

    phase, faturamento, evento = await _estado()
    assert faturamento == faixa_esperada, "gravou texto cru em vez da faixa"
    assert phase == "agendou_sessao"
    assert evento == "evt_existente", "cancelou reunião de lead qualificado"
    assert espioes["cancelou"] is None
    assert espioes["handover"] is None


async def test_abaixo_de_5k_cancela_a_reuniao_e_desqualifica(
    turno, sem_pipedrive, espioes, lead
):
    """A regra que custa uma reunião, garantida por código.

    O modelo pode até pedir `agendou_sessao`: abaixo do corte, quem manda é
    o `if`.
    """
    saida = await crm.update_crm.ainvoke(
        {"phase": "agendou_sessao", "faturamento_mensal": "uns 3 mil por mês"}
    )

    phase, faturamento, _ = await _estado()
    assert faturamento == faixas.DE_3K_A_5K
    assert phase == "desqualificado", "abaixo do corte não pode ficar agendado"
    assert espioes["cancelou"] == TELEFONE, "não cancelou a reunião"
    assert espioes["handover"] is not None, "não avisou ninguém"
    assert "[sistema]" in saida


async def test_acima_de_25k_mantem_a_reuniao_e_aciona_o_silvio(
    turno, sem_pipedrive, espioes, lead
):
    await crm.update_crm.ainvoke(
        {"phase": "agendou_sessao", "faturamento_mensal": "uns 40 mil por mês"}
    )

    phase, faturamento, evento = await _estado()
    assert faturamento == faixas.ACIMA_DE_25K
    assert phase == "agendou_sessao", "lead grande não pode perder a reunião"
    assert evento == "evt_existente", "cancelou reunião de lead acima de 25k"
    assert espioes["cancelou"] is None
    assert espioes["handover"] is not None, "não encaminhou para o Silvio"


async def test_faturamento_indecidivel_aciona_humano_sem_cancelar(
    turno, sem_pipedrive, espioes, lead
):
    """ "prefiro não dizer" não é zero.

    Tratar como abaixo do corte cancelaria a reunião de alguém que talvez
    faturasse 50 mil. O código não adivinha: mantém tudo e chama gente.
    """
    await crm.update_crm.ainvoke(
        {"phase": "agendou_sessao", "faturamento_mensal": "prefiro não dizer"}
    )

    phase, faturamento, evento = await _estado()
    assert phase == "agendou_sessao"
    assert evento == "evt_existente", "cancelou sem saber o faturamento"
    assert faturamento is None, "não pode inventar faixa"
    assert espioes["cancelou"] is None
    assert espioes["handover"] is not None


async def test_sem_faturamento_o_comportamento_antigo_e_preservado(
    turno, sem_pipedrive, espioes, lead
):
    """Contraprova: transição de fase sem faturamento não aciona nada.

    A maioria das chamadas de `update_crm` é assim (`qualificado`,
    `desqualificado` por C1), e nenhuma pode virar handover por acidente.
    """
    await crm.update_crm.ainvoke({"phase": "agendou_sessao"})

    phase, faturamento, evento = await _estado()
    assert phase == "agendou_sessao"
    assert faturamento is None
    assert evento == "evt_existente"
    assert espioes["cancelou"] is None
    assert espioes["handover"] is None
