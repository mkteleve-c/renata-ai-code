"""Testes das tools de agenda da Renata.

Nenhum teste toca a API real — e nenhum **escreve** nela em hipótese
alguma. A agenda é de trabalho de uma pessoa; criar, mover ou apagar evento
só acontece contra o cliente falso deste arquivo.

Os eventos usados aqui não são inventados: reproduzem a forma do que a
leitura real trouxe na Task 4 — duração quebrada (`13:15–14:15`,
`09:30–10:05`), sobreposição entre eventos (`09:30–10:05` cruzando
`10:00–10:30`) e recorrência de 45 min no começo da manhã.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from langchain_core.runnables.config import var_child_runnable_config

from whatsapp_langchain.agents.catalog.elevec_sdr.tools import agenda
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.agenda import (
    aplicar_escassez,
    calcular_disponibilidade,
    calendar_agendar,
    calendar_delete,
    calendar_get_event,
    calendar_get_many,
    calendar_update,
    formatar_disponibilidade,
)
from whatsapp_langchain.shared.google_calendar import FUSO, GoogleCalendarError

TELEFONE = "+5511955554444"

# Terça-feira. D+1..D+4 = quarta 11, quinta 12, sexta 13, sábado 14.
# 12/02/2026 é quinta — a mesma data do exemplo de saída do n8n.
TERCA = datetime(2026, 2, 10, 10, 0, tzinfo=FUSO)
QUINTA = datetime(2026, 2, 12, 10, 0, tzinfo=FUSO)


def evento(
    inicio: str,
    fim: str,
    status: str = "confirmed",
    event_id: str = "ev1",
) -> dict[str, Any]:
    """Evento com hora, no shape que o Google devolve."""
    return {
        "id": event_id,
        "status": status,
        "summary": "ocupado",
        "start": {"dateTime": inicio, "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": fim, "timeZone": "America/Sao_Paulo"},
    }


def evento_dia_inteiro(
    inicio: str, fim: str, status: str = "confirmed"
) -> dict[str, Any]:
    """Evento de dia inteiro: `date` em vez de `dateTime`, `end` exclusivo."""
    return {
        "id": "allday",
        "status": status,
        "summary": "férias",
        "start": {"date": inicio},
        "end": {"date": fim},
    }


class ClienteFalso:
    """Dublê do GoogleCalendarClient. Registra o que foi chamado."""

    def __init__(self, eventos: list[dict[str, Any]] | None = None, erro=None):
        self.eventos = eventos or []
        self.erro = erro
        self.criados: list[dict[str, Any]] = []
        self.atualizados: list[dict[str, Any]] = []
        self.deletados: list[str] = []
        self.listagens: list[tuple[datetime, datetime]] = []

    async def listar_eventos(self, inicio, fim, max_results=250):
        if self.erro:
            raise self.erro
        self.listagens.append((inicio, fim))
        return [item for item in self.eventos if _cruza(item, inicio, fim)]

    async def criar_evento(self, summary, inicio, fim, participantes=None, **kwargs):
        if self.erro:
            raise self.erro
        self.criados.append(
            {
                "summary": summary,
                "inicio": inicio,
                "fim": fim,
                "participantes": participantes,
            }
        )
        return {"id": "evento-novo", "htmlLink": "https://cal/evento-novo"}

    async def atualizar_evento(self, event_id, **campos):
        if self.erro:
            raise self.erro
        self.atualizados.append({"event_id": event_id, **campos})
        return {"id": event_id}

    async def deletar_evento(self, event_id, notificar=True):
        if self.erro:
            raise self.erro
        self.deletados.append(event_id)

    async def obter_evento(self, event_id):
        if self.erro:
            raise self.erro
        return {
            "id": event_id,
            "summary": "Consultoria de Alavancagem de Carreira - Ana",
            "start": {"dateTime": "2026-02-12T13:00:00-03:00"},
            "end": {"dateTime": "2026-02-12T14:00:00-03:00"},
            "status": "confirmed",
        }


def _cruza(item: dict[str, Any], inicio: datetime, fim: datetime) -> bool:
    intervalo = agenda.intervalo_do_evento(item)
    if intervalo is None:
        return True
    return intervalo[0] < fim and intervalo[1] > inicio


def lead(**campos: Any) -> dict[str, Any]:
    base = {
        "phone": "551155554444",
        "name": "Ana",
        "email": "ana@exemplo.com",
        "faturamento_mensal": "uns 30 mil",
        "google_event_id": None,
    }
    base.update(campos)
    return base


@pytest.fixture
def turno():
    """Coloca o telefone do lead no `configurable`, como o worker faz."""
    token = var_child_runnable_config.set(  # type: ignore[arg-type]
        {"configurable": {"user_id": TELEFONE, "thread_id": f"{TELEFONE}:elevec_sdr"}}
    )
    yield
    var_child_runnable_config.reset(token)


@pytest.fixture
def ambiente(monkeypatch):
    """Cliente falso, lead falso e relógio congelado.

    Devolve um objeto com `cliente`, `lead` e `gravacoes` para os testes
    ajustarem e inspecionarem.
    """

    class Ambiente:
        def __init__(self):
            self.cliente = ClienteFalso()
            self.lead: dict[str, Any] | None = lead()
            self.gravacoes: list[dict[str, Any]] = []
            self.agora = TERCA

    amb = Ambiente()

    monkeypatch.setattr(agenda, "obter_cliente", lambda: amb.cliente)
    monkeypatch.setattr(agenda, "agora_sp", lambda: amb.agora)

    async def carregar(_telefone):
        return amb.lead

    async def gravar(telefone, **campos):
        amb.gravacoes.append({"telefone": telefone, **campos})

    monkeypatch.setattr(agenda, "carregar_lead", carregar)
    monkeypatch.setattr(agenda, "gravar_agendamento", gravar)
    return amb


# --- Disponibilidade (funções puras) ---------------------------------------


def test_dia_livre_lista_todas_as_horas_do_periodo():
    livres = calcular_disponibilidade([], TERCA, "tarde")
    assert livres[0] == (date(2026, 2, 11), [13, 14, 15, 16, 17])


def test_formato_da_saida_replica_o_n8n():
    disponibilidade = [(date(2026, 2, 12), [13, 14, 16, 17])]
    assert formatar_disponibilidade(disponibilidade) == "quinta 12/02: 13, 14, 16, 17"


def test_fim_de_semana_nunca_aparece():
    # Quinta 12/02: a janela D+1..D+4 cobre sexta 13, sábado 14, domingo 15 e
    # segunda 16. Só os dois dias úteis podem sair.
    livres = calcular_disponibilidade([], QUINTA, "manha")
    dias = [dia for dia, _ in livres]
    assert dias == [date(2026, 2, 13), date(2026, 2, 16)]


def test_hoje_nunca_aparece():
    livres = calcular_disponibilidade([], TERCA, "qualquer")
    dias = [dia for dia, _ in livres]
    assert date(2026, 2, 10) not in dias
    assert dias[0] == date(2026, 2, 11)


def test_evento_sobreposto_de_duracao_quebrada_bloqueia_os_dois_slots():
    # Caso real da agenda: 13:15–14:15 cruza tanto o slot das 13h quanto o
    # das 14h. Comparar hora de início liberaria os dois.
    eventos = [evento("2026-02-11T13:15:00-03:00", "2026-02-11T14:15:00-03:00")]
    livres = calcular_disponibilidade(eventos, TERCA, "tarde")
    assert livres[0] == (date(2026, 2, 11), [15, 16, 17])


def test_eventos_sobrepostos_entre_si_bloqueiam_a_uniao():
    # 09:30–10:05 cruzando 10:00–10:30, exatamente como a leitura real trouxe.
    eventos = [
        evento("2026-02-11T09:30:00-03:00", "2026-02-11T10:05:00-03:00", event_id="a"),
        evento("2026-02-11T10:00:00-03:00", "2026-02-11T10:30:00-03:00", event_id="b"),
    ]
    livres = calcular_disponibilidade(eventos, TERCA, "manha")
    assert livres[0] == (date(2026, 2, 11), [8, 11])


def test_evento_encostado_no_slot_nao_bloqueia():
    # 12:00–13:00 termina onde o slot das 13h começa. Não há conflito.
    eventos = [evento("2026-02-11T12:00:00-03:00", "2026-02-11T13:00:00-03:00")]
    livres = calcular_disponibilidade(eventos, TERCA, "tarde")
    assert livres[0][1][0] == 13


def test_evento_cancelado_nao_bloqueia():
    eventos = [
        evento(
            "2026-02-11T13:00:00-03:00",
            "2026-02-11T14:00:00-03:00",
            status="cancelled",
        )
    ]
    livres = calcular_disponibilidade(eventos, TERCA, "tarde")
    assert 13 in livres[0][1]


def test_evento_de_dia_inteiro_bloqueia_o_dia():
    eventos = [evento_dia_inteiro("2026-02-11", "2026-02-12")]
    livres = calcular_disponibilidade(eventos, TERCA, "qualquer")
    dias = [dia for dia, _ in livres]
    assert date(2026, 2, 11) not in dias
    assert date(2026, 2, 12) in dias


def test_evento_sem_horario_reconhecivel_nao_derruba_a_consulta():
    eventos = [{"id": "x", "status": "confirmed", "start": {}, "end": {}}]
    # Um evento ilegível não pode virar "agenda livre" — mas também não pode
    # derrubar a consulta. Ele é ignorado e os demais dias seguem.
    livres = calcular_disponibilidade(eventos, TERCA, "tarde")
    assert livres  # a consulta não quebra


# --- Replay da agenda real --------------------------------------------------

# Os 29 eventos que a leitura real da Task 4 trouxe da agenda
# `silvio.hirata@eleve-c.co` na janela 28/07–31/07/2026, transcritos do
# relatório. Nenhuma chamada de rede: é replay, não integração.
AGENDA_REAL = [
    ("2026-07-28", "07:15", "08:00"),
    ("2026-07-28", "08:00", "09:00"),
    ("2026-07-28", "09:00", "10:00"),
    ("2026-07-28", "10:00", "10:30"),
    ("2026-07-28", "10:30", "12:00"),
    ("2026-07-28", "12:00", "13:00"),
    ("2026-07-28", "13:15", "14:15"),
    ("2026-07-28", "14:45", "15:45"),
    ("2026-07-28", "16:00", "17:00"),
    ("2026-07-28", "19:30", "20:30"),
    ("2026-07-28", "21:00", "22:00"),
    ("2026-07-29", "07:15", "08:00"),
    ("2026-07-29", "08:30", "09:30"),
    ("2026-07-29", "09:30", "10:05"),
    ("2026-07-29", "10:00", "10:30"),
    ("2026-07-29", "10:30", "12:00"),
    ("2026-07-29", "12:00", "13:00"),
    ("2026-07-29", "19:00", "20:00"),
    ("2026-07-30", "07:15", "08:00"),
    ("2026-07-30", "09:00", "10:00"),
    ("2026-07-30", "10:00", "10:30"),
    ("2026-07-30", "10:30", "12:00"),
    ("2026-07-30", "12:00", "13:00"),
    ("2026-07-30", "18:00", "19:00"),
    ("2026-07-31", "07:15", "08:00"),
    ("2026-07-31", "09:00", "10:00"),
    ("2026-07-31", "10:00", "10:30"),
    ("2026-07-31", "10:30", "12:00"),
    ("2026-07-31", "12:00", "13:00"),
]


def eventos_reais() -> list[dict[str, Any]]:
    return [
        evento(f"{dia}T{ini}:00-03:00", f"{dia}T{fim}:00-03:00", event_id=f"real{i}")
        for i, (dia, ini, fim) in enumerate(AGENDA_REAL)
    ]


def test_replay_da_agenda_real_nao_oferece_horario_ocupado():
    # Segunda 27/07/2026: a janela D+1..D+4 é exatamente 28 a 31.
    segunda = datetime(2026, 7, 27, 9, 0, tzinfo=FUSO)
    livres = dict(calcular_disponibilidade(eventos_reais(), segunda, "qualquer"))

    # 28/07 é o dia mais denso: 13:15–14:15 mata as 13h e as 14h, 14:45–15:45
    # mata as 15h, e 10:30–12:00 mata as 11h. Sobram duas ilhas.
    assert livres[date(2026, 7, 28)] == [17, 18]
    # 29/07: 09:30–10:05 sobreposto a 10:00–10:30 fecha a manhã inteira a
    # partir das 8h30; 19:00–20:00 abre as 20h de volta.
    assert livres[date(2026, 7, 29)] == [13, 14, 15, 16, 17, 18, 20, 21]
    assert livres[date(2026, 7, 30)] == [8, 13, 14, 15, 16, 17, 19, 20, 21]


def test_replay_da_agenda_real_com_escassez():
    segunda = datetime(2026, 7, 27, 9, 0, tzinfo=FUSO)
    disponibilidade = calcular_disponibilidade(eventos_reais(), segunda, "qualquer")
    assert formatar_disponibilidade(aplicar_escassez(disponibilidade)) == (
        "terça 28/07: 17, 18\nquarta 29/07: 13, 14"
    )


# --- calendar_get_many ------------------------------------------------------


async def test_get_many_aplica_escassez_de_dois_dias_e_dois_horarios(turno, ambiente):
    saida = await calendar_get_many.ainvoke({"periodo": "tarde"})
    linhas = saida.strip().splitlines()
    assert len(linhas) == 2
    for linha in linhas:
        horas = linha.split(":")[1].split(",")
        assert len(horas) == 2


async def test_get_many_nao_oferece_hoje_nem_fim_de_semana(turno, ambiente):
    ambiente.agora = QUINTA
    saida = await calendar_get_many.ainvoke({"periodo": "manha"})
    assert "12/02" not in saida
    assert "14/02" not in saida and "15/02" not in saida
    assert "13/02" in saida


async def test_get_many_com_periodo_invalido_devolve_mensagem(turno, ambiente):
    saida = await calendar_get_many.ainvoke({"periodo": "madrugada"})
    assert "madrugada" in saida
    assert "manha" in saida
    assert not ambiente.cliente.listagens


async def test_get_many_com_erro_do_google_devolve_mensagem(turno, ambiente):
    ambiente.cliente.erro = GoogleCalendarError(503, "indisponível")
    saida = await calendar_get_many.ainvoke({"periodo": "tarde"})
    assert "não consegui" in saida.lower()


async def test_get_many_sem_horario_livre_avisa(turno, ambiente):
    ambiente.cliente.eventos = [
        evento_dia_inteiro("2026-02-11", "2026-02-17"),
    ]
    saida = await calendar_get_many.ainvoke({"periodo": "tarde"})
    assert "nenhum hor" in saida.lower()


# --- calendar_agendar -------------------------------------------------------


async def test_agendar_recusa_sem_email(turno, ambiente):
    ambiente.lead = lead(email=None)
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert "fase 6" in saida.lower()
    assert not ambiente.cliente.criados
    assert not ambiente.gravacoes


async def test_agendar_recusa_sem_faturamento(turno, ambiente):
    ambiente.lead = lead(faturamento_mensal="")
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert "fase 7" in saida.lower()
    assert not ambiente.cliente.criados
    assert not ambiente.gravacoes


async def test_agendar_recusa_email_que_nao_e_email(turno, ambiente):
    ambiente.lead = lead(email=None)
    saida = await calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00", "email": "sim"}
    )
    assert "fase 6" in saida.lower()
    assert not ambiente.cliente.criados


async def test_agendar_cria_evento_e_grava_google_event_id(turno, ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert len(ambiente.cliente.criados) == 1
    criado = ambiente.cliente.criados[0]
    assert criado["summary"] == "Consultoria de Alavancagem de Carreira - Ana"
    assert criado["participantes"] == ["ana@exemplo.com"]
    assert criado["inicio"] == datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    assert criado["fim"] == datetime(2026, 2, 12, 14, 0, tzinfo=FUSO)

    assert ambiente.gravacoes == [
        {
            "telefone": TELEFONE,
            "google_event_id": "evento-novo",
            "email": "ana@exemplo.com",
            "faturamento_mensal": "uns 30 mil",
        }
    ]
    assert "12/02" in saida


async def test_agendar_persiste_email_e_faturamento_novos(turno, ambiente):
    ambiente.lead = lead(email=None, faturamento_mensal=None)
    await calendar_agendar.ainvoke(
        {
            "inicio": "2026-02-12T13:00",
            "email": "novo@exemplo.com",
            "faturamento_mensal": "40 mil",
        }
    )
    assert ambiente.gravacoes[0]["email"] == "novo@exemplo.com"
    assert ambiente.gravacoes[0]["faturamento_mensal"] == "40 mil"
    assert ambiente.cliente.criados[0]["participantes"] == ["novo@exemplo.com"]


async def test_agendar_reconsulta_e_recusa_horario_ocupado(turno, ambiente):
    ambiente.cliente.eventos = [
        evento("2026-02-12T13:15:00-03:00", "2026-02-12T14:15:00-03:00")
    ]
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert not ambiente.cliente.criados
    assert "ocupado" in saida.lower() or "não está livre" in saida.lower()


async def test_agendar_recusa_fim_de_semana(turno, ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-14T13:00"})
    assert not ambiente.cliente.criados
    assert "segunda a sexta" in saida.lower()


async def test_agendar_recusa_hoje(turno, ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-10T13:00"})
    assert not ambiente.cliente.criados
    assert "a partir de amanh" in saida.lower()


async def test_agendar_recusa_hora_fora_da_grade(turno, ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T12:00"})
    assert not ambiente.cliente.criados
    assert "12h" in saida or "12:00" in saida


async def test_agendar_recusa_hora_quebrada(turno, ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:30"})
    assert not ambiente.cliente.criados
    assert "hora cheia" in saida.lower()


async def test_agendar_com_data_ilegivel_devolve_mensagem(turno, ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "quinta que vem"})
    assert not ambiente.cliente.criados
    assert "não entendi" in saida.lower()


async def test_agendar_aceita_formato_brasileiro(turno, ambiente):
    await calendar_agendar.ainvoke({"inicio": "12/02/2026 13:00"})
    assert ambiente.cliente.criados[0]["inicio"] == datetime(
        2026, 2, 12, 13, 0, tzinfo=FUSO
    )


async def test_agendar_sem_lead_no_banco_recusa(turno, ambiente):
    ambiente.lead = None
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert not ambiente.cliente.criados
    assert "fase 6" in saida.lower()


async def test_agendar_com_falha_do_google_nao_grava_lead(turno, ambiente):
    ambiente.cliente.erro = GoogleCalendarError(500, "boom")
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert not ambiente.gravacoes
    assert "não consegui" in saida.lower()


# --- update / delete / get_event -------------------------------------------


async def test_update_usa_o_google_event_id_do_lead(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-antigo")
    saida = await calendar_update.ainvoke({"novo_inicio": "2026-02-13T14:00"})
    assert ambiente.cliente.atualizados[0]["event_id"] == "ev-antigo"
    assert ambiente.cliente.atualizados[0]["inicio"] == datetime(
        2026, 2, 13, 14, 0, tzinfo=FUSO
    )
    assert "13/02" in saida


async def test_update_sem_evento_conhecido_avisa(turno, ambiente):
    ambiente.lead = lead(google_event_id=None)
    saida = await calendar_update.ainvoke({"novo_inicio": "2026-02-13T14:00"})
    assert not ambiente.cliente.atualizados
    assert "não encontrei" in saida.lower()


async def test_update_recusa_horario_ocupado(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-antigo")
    ambiente.cliente.eventos = [
        evento("2026-02-13T13:30:00-03:00", "2026-02-13T14:30:00-03:00", event_id="x")
    ]
    saida = await calendar_update.ainvoke({"novo_inicio": "2026-02-13T14:00"})
    assert not ambiente.cliente.atualizados
    assert "ocupado" in saida.lower() or "não está livre" in saida.lower()


async def test_update_ignora_o_proprio_evento_na_reconsulta(turno, ambiente):
    # O evento que está sendo movido aparece na listagem e não pode bloquear
    # a si mesmo — senão remarcar para o mesmo horário seria impossível.
    ambiente.lead = lead(google_event_id="ev-antigo")
    ambiente.cliente.eventos = [
        evento(
            "2026-02-13T14:00:00-03:00",
            "2026-02-13T15:00:00-03:00",
            event_id="ev-antigo",
        )
    ]
    await calendar_update.ainvoke({"novo_inicio": "2026-02-13T14:00"})
    assert ambiente.cliente.atualizados


async def test_delete_usa_o_google_event_id_do_lead(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-antigo")
    saida = await calendar_delete.ainvoke({})
    assert ambiente.cliente.deletados == ["ev-antigo"]
    assert ambiente.gravacoes[0]["google_event_id"] is None
    assert "cancel" in saida.lower()


async def test_delete_sem_evento_conhecido_avisa(turno, ambiente):
    ambiente.lead = lead(google_event_id=None)
    saida = await calendar_delete.ainvoke({})
    assert not ambiente.cliente.deletados
    assert "não encontrei" in saida.lower()


async def test_get_event_descreve_o_evento_do_lead(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-antigo")
    saida = await calendar_get_event.ainvoke({})
    assert "12/02" in saida
    assert "13" in saida


async def test_get_event_com_erro_devolve_mensagem(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-antigo")
    ambiente.cliente.erro = GoogleCalendarError(404, "sumiu")
    saida = await calendar_get_event.ainvoke({})
    assert "não consegui" in saida.lower()


# --- Sem telefone no turno --------------------------------------------------


async def test_tools_sem_telefone_no_configurable_nao_quebram(ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert not ambiente.cliente.criados
    assert saida


async def test_get_many_funciona_sem_telefone(ambiente):
    # Consultar disponibilidade não depende do lead — só agendar depende.
    saida = await calendar_get_many.ainvoke({"periodo": "tarde"})
    assert "11/02" in saida


# --- Cliente sem credencial -------------------------------------------------


async def test_sem_credencial_configurada_devolve_mensagem(turno, monkeypatch):
    def sem_credencial():
        raise ValueError("GoogleCalendarClient exige client_id preenchido(s)")

    monkeypatch.setattr(agenda, "obter_cliente", sem_credencial)
    saida = await calendar_get_many.ainvoke({"periodo": "tarde"})
    assert "não consegui" in saida.lower()
