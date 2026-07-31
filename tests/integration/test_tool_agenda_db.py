"""A camada de banco das tools de agenda, contra o Postgres de verdade.

`tests/unit/test_tool_agenda.py` monkeypatcha `carregar_lead` e
`gravar_agendamento` — é o certo lá, porque o assunto é a política de
disponibilidade e os portões. Mas o efeito colateral é que o **SQL nunca
roda**: trocar `email = coalesce(nullif(%s,''), email)` por `email = email`
deixava 42 de 42 verdes.

O `UPDATE` daqui é o único elo entre o evento que existe no Google Calendar
e o lead que existe no banco. Se ele falhar em silêncio, o `google_event_id`
se perde e ninguém consegue remarcar nem cancelar. Por isso este arquivo
fala com o banco real.

Só a agenda continua dublada — **nenhuma escrita contra a API do Google.**
"""

from datetime import datetime
from typing import Any

import pytest
from langchain_core.runnables.config import var_child_runnable_config

from whatsapp_langchain.agents.catalog.elevec_sdr.tools import agenda
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.agenda import (
    calendar_agendar,
    calendar_delete,
    carregar_lead,
    gravar_agendamento,
)
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.google_calendar import FUSO

CANONICO = "551155551111"
COM_9 = "5511955551111"
E164 = "+5511955551111"

TERCA = datetime(2026, 2, 10, 10, 0, tzinfo=FUSO)
SLOT = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)


@pytest.fixture
async def limpar():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "delete from leads_crm where phone in (%s, %s)", (CANONICO, COM_9)
        )
    yield
    async with pool.connection() as conn:
        await conn.execute(
            "delete from leads_crm where phone in (%s, %s)", (CANONICO, COM_9)
        )


async def criar_lead(**campos: Any) -> None:
    base = {
        "name": "Ana",
        "email": None,
        "faturamento_mensal": None,
        "google_event_id": None,
    }
    base.update(campos)
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm"
            " (phone, name, email, faturamento_mensal, google_event_id,"
            "  source, phase)"
            " values (%s, %s, %s, %s, %s, 'whatsapp_direct', 'qualificado')",
            (
                CANONICO,
                base["name"],
                base["email"],
                base["faturamento_mensal"],
                base["google_event_id"],
            ),
        )


async def ler_lead() -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select name, email, faturamento_mensal, google_event_id "
            "from leads_crm where phone = %s",
            (CANONICO,),
        )
        linha = await cur.fetchone()
    if linha is None:
        return None
    return {
        "name": linha[0],
        "email": linha[1],
        "faturamento_mensal": linha[2],
        "google_event_id": linha[3],
    }


class ClienteFalso:
    """Só a agenda é dublada. O banco desta suíte é o de verdade."""

    def __init__(self):
        self.criados: list[dict[str, Any]] = []
        self.deletados: list[str] = []

    async def listar_eventos(self, inicio, fim, max_results=250):
        return []

    async def criar_evento(
        self, summary, inicio, fim, participantes=None, event_id=None, **kwargs
    ):
        self.criados.append({"summary": summary, "event_id": event_id})
        # Corpo realista: o cliente devolve o evento, com `start` e
        # `status`, e é isso que `evento_confere` lê antes de confirmar.
        return {
            "id": event_id or "evento-novo",
            "status": "confirmed",
            "start": {"dateTime": inicio.isoformat()},
            "end": {"dateTime": fim.isoformat()},
        }

    async def obter_evento(self, event_id):
        return {"id": event_id, "status": "confirmed"}

    async def deletar_evento(self, event_id, notificar=True):
        self.deletados.append(event_id)


@pytest.fixture
def turno():
    token = var_child_runnable_config.set(  # type: ignore[arg-type]
        {"configurable": {"user_id": E164}}
    )
    yield
    var_child_runnable_config.reset(token)


@pytest.fixture
def agenda_dublada(monkeypatch):
    cliente = ClienteFalso()
    monkeypatch.setattr(agenda, "obter_cliente", lambda: cliente)
    monkeypatch.setattr(agenda, "agora_sp", lambda: TERCA)
    return cliente


# --- carregar_lead ----------------------------------------------------------


async def test_carregar_lead_canonicaliza_o_telefone(limpar):
    await criar_lead(email="ana@exemplo.com")

    # O harness carrega o telefone em E.164 COM o 9; `leads_crm.phone` é
    # canônico SEM ele. Sem canonicalizar, o lead nunca é encontrado.
    lido = await carregar_lead(E164)

    assert lido is not None
    assert lido["phone"] == CANONICO
    assert lido["email"] == "ana@exemplo.com"


async def test_carregar_lead_ausente_devolve_none(limpar):
    assert await carregar_lead(E164) is None


# --- gravar_agendamento -----------------------------------------------------


async def test_gravar_persiste_os_tres_campos(limpar):
    await criar_lead()

    assert await gravar_agendamento(
        E164,
        google_event_id="ev-123",
        email="ana@exemplo.com",
        faturamento_mensal="uns 30 mil",
    )

    assert await ler_lead() == {
        "name": "Ana",
        "email": "ana@exemplo.com",
        "faturamento_mensal": "uns 30 mil",
        "google_event_id": "ev-123",
    }


async def test_gravar_com_campos_vazios_nao_apaga_o_que_ja_existe(limpar):
    await criar_lead(email="ana@exemplo.com", faturamento_mensal="30 mil")

    # É o caminho do reagendamento e do cancelamento, que chamam sem e-mail
    # nem faturamento. Sem o `coalesce(nullif(...))`, cancelar uma reunião
    # apagaria o cadastro do lead.
    assert await gravar_agendamento(E164, google_event_id="ev-456")

    lido = await ler_lead()
    assert lido["email"] == "ana@exemplo.com"
    assert lido["faturamento_mensal"] == "30 mil"
    assert lido["google_event_id"] == "ev-456"


async def test_gravar_none_apaga_o_vinculo_do_evento(limpar):
    await criar_lead(google_event_id="ev-antigo", email="ana@exemplo.com")

    assert await gravar_agendamento(E164, google_event_id=None)

    lido = await ler_lead()
    assert lido["google_event_id"] is None
    assert lido["email"] == "ana@exemplo.com"


async def test_gravar_com_lead_inexistente_devolve_false(limpar):
    # Zero linhas afetadas. Sem checar `rowcount` isto seria sucesso
    # silencioso — e o google_event_id de um evento que EXISTE na agenda se
    # perderia para sempre.
    assert await gravar_agendamento(E164, google_event_id="ev-orfao") is False


# --- calendar_agendar ponta a ponta (banco real) ----------------------------


async def test_agendar_grava_o_event_id_no_lead(limpar, turno, agenda_dublada):
    await criar_lead(email="ana@exemplo.com", faturamento_mensal="30 mil")

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    esperado = agenda.event_id_deterministico(E164, SLOT)
    lido = await ler_lead()
    assert lido["google_event_id"] == esperado
    assert agenda_dublada.criados[0]["event_id"] == esperado
    assert "12/02" in saida
    assert "human_handover" not in saida


async def test_agendar_persiste_o_email_do_argumento_e_nada_de_faturamento(
    limpar, turno, agenda_dublada
):
    """Agendar grava o e-mail; faturamento não passa mais por aqui.

    Aceitá-lo nesta tool era o furo que deixava o texto cru do lead chegar ao
    banco sem virar faixa — e, sem faixa, sem cancelamento abaixo do corte
    nem encaminhamento acima de R$ 25 mil. Agora só `update_crm` escreve
    esse campo, depois de normalizar.
    """
    await criar_lead()

    await calendar_agendar.ainvoke(
        {
            "inicio": "2026-02-12T13:00",
            "email": "ana@exemplo.com",
        }
    )

    lido = await ler_lead()
    assert lido["email"] == "ana@exemplo.com"
    assert lido["faturamento_mensal"] is None


async def test_agendar_sem_lead_no_banco_avisa_e_nao_perde_o_evento(
    limpar, turno, agenda_dublada, capsys
):
    # Sem linha em leads_crm: o portão passa pelos argumentos, o evento é
    # criado e o UPDATE casa zero linhas.
    saida = await calendar_agendar.ainvoke(
        {
            "inicio": "2026-02-12T13:00",
            "email": "ana@exemplo.com",
            "faturamento_mensal": "30 mil",
        }
    )

    assert agenda_dublada.criados, "o evento foi criado na agenda"
    assert "human_handover" in saida
    # O id precisa estar no log — é o que permite reconciliar à mão o evento
    # órfão. E ele é recomputável a partir de telefone + horário.
    esperado = agenda.event_id_deterministico(E164, SLOT)
    logs = capsys.readouterr().out
    assert "agenda_vinculo_nao_gravado" in logs
    assert esperado in logs


async def test_delete_limpa_o_vinculo_no_banco(limpar, turno, agenda_dublada):
    await criar_lead(google_event_id="ev-antigo", email="ana@exemplo.com")

    saida = await calendar_delete.ainvoke({})

    assert agenda_dublada.deletados == ["ev-antigo"]
    lido = await ler_lead()
    assert lido["google_event_id"] is None
    assert lido["email"] == "ana@exemplo.com"
    assert "cancel" in saida.lower()


async def test_delete_com_id_de_terceiro_nao_toca_o_banco(
    limpar, turno, agenda_dublada
):
    await criar_lead(google_event_id="ev-do-lead")

    saida = await calendar_delete.ainvoke({"event_id": "ev-de-outra-pessoa"})

    assert agenda_dublada.deletados == []
    assert (await ler_lead())["google_event_id"] == "ev-do-lead"
    assert "cancelado" not in saida.lower()
