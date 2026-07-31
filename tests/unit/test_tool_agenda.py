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
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.interno import (
    PREFIXO_INTERNO,
)
from whatsapp_langchain.shared.google_calendar import (
    FUSO,
    GoogleCalendarError,
    validar_event_id,
)

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
        # `{event_id: corpo}` — ids que respondem 409 no insert. O cliente
        # real devolve o evento pré-existente nesse caso.
        self.conflitos: dict[str, dict[str, Any]] = {}
        # O que `obter_evento` devolve. Configurável para que a leitura não
        # seja sempre um `confirmed` no horário certo.
        self.detalhe: dict[str, Any] | None = None

    async def listar_eventos(self, inicio, fim, max_results=250):
        if self.erro:
            raise self.erro
        self.listagens.append((inicio, fim))
        return [item for item in self.eventos if _na_janela(item, inicio, fim)]

    async def criar_evento(
        self, summary, inicio, fim, participantes=None, event_id=None, **kwargs
    ):
        if self.erro:
            raise self.erro

        # 409: o id já existe. O cliente real resolve lendo o evento
        # existente e devolvendo ELE — que pode estar cancelado (lixeira) ou
        # em outro horário (slot já reagendado). Nada é criado.
        novo = event_id or "evento-novo"
        if novo in self.conflitos:
            return dict(self.conflitos[novo])

        self.criados.append(
            {
                "summary": summary,
                "inicio": inicio,
                "fim": fim,
                "participantes": participantes,
                "event_id": event_id,
            }
        )
        # O cliente real garante que o `id` sempre volta, e devolve o corpo
        # do evento — com `start` e `status`, que é o que a conferência lê,
        # e com `attendees`, que é o que prova que o convite saiu (o
        # `events.insert` responde com o Event criado, participantes
        # incluídos). Ver `convite_para`.
        return {
            "id": novo,
            "status": "confirmed",
            "summary": summary,
            "start": {"dateTime": inicio.isoformat()},
            "end": {"dateTime": fim.isoformat()},
            "attendees": [{"email": email} for email in (participantes or [])],
            "htmlLink": "https://cal/x",
        }

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
        # `detalhe` é o override explícito da LEITURA e vence `conflitos`,
        # que descreve o que o insert devolve no 409.
        if self.detalhe is not None:
            return {**self.detalhe, "id": event_id}
        if event_id in self.conflitos:
            return dict(self.conflitos[event_id])
        return {
            "id": event_id,
            "summary": "Consultoria de Alavancagem de Carreira - Ana",
            "start": {"dateTime": "2026-02-12T13:00:00-03:00"},
            "end": {"dateTime": "2026-02-12T14:00:00-03:00"},
            "status": "confirmed",
        }


def _dia(bloco: dict[str, Any]) -> str:
    """`AAAA-MM-DD` do bloco, por fatia de texto."""
    bruto = bloco.get("dateTime") or bloco.get("date") or ""
    return bruto[:10]


def _na_janela(item: dict[str, Any], inicio: datetime, fim: datetime) -> bool:
    """Filtro do dublê: interseção por DIA, em comparação de strings ISO.

    Deliberadamente mais grosso que a regra real e sem chamar nada de
    `agenda`. Uma versão anterior reusava `agenda.intervalo_do_evento` e
    replicava a fórmula de sobreposição — uma regressão em `_instante`
    regrediria o dublê junto e os testes de tool continuariam verdes. Como
    devolve um superconjunto do que a janela pede, quem filtra de verdade é
    sempre o código sob teste.
    """
    dia_inicio = _dia(item.get("start", {}))
    dia_fim = _dia(item.get("end", {})) or dia_inicio
    if not dia_inicio:
        return False
    return dia_inicio <= fim.strftime("%Y-%m-%d") and dia_fim >= inicio.strftime(
        "%Y-%m-%d"
    )


def lead(**campos: Any) -> dict[str, Any]:
    base = {
        "phone": "551155554444",
        "name": "Ana",
        "email": "ana@exemplo.com",
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
            # A gravação pode falhar (lead ausente, banco fora) — os testes
            # que exercitam esse desfecho viram isto para False.
            self.gravacao_ok = True

    amb = Ambiente()

    monkeypatch.setattr(agenda, "obter_cliente", lambda: amb.cliente)
    monkeypatch.setattr(agenda, "agora_sp", lambda: amb.agora)

    async def carregar(_telefone):
        return amb.lead

    async def gravar(telefone, **campos):
        amb.gravacoes.append({"telefone": telefone, **campos})
        return amb.gravacao_ok

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
    # 29/07 tem 13,14,15,16,17,18,20,21 livres: sai `13, 18` — tarde + noite,
    # não `13, 14`. Ver `escolher_horarios`.
    assert formatar_disponibilidade(aplicar_escassez(disponibilidade, "qualquer")) == (
        "terça 28/07: 17, 18\nquarta 29/07: 13, 18"
    )


# --- Viés de horário --------------------------------------------------------


def test_dia_livre_sem_periodo_pedido_nao_comeca_as_8():
    # Sem esta regra, `[:2]` sobre a lista ordenada daria `8, 9` para toda
    # agenda com a manhã livre, e as consultorias se concentrariam às 8h.
    livres = calcular_disponibilidade([], TERCA, "qualquer")
    escolhidas = aplicar_escassez(livres, "qualquer")[0][1]
    assert escolhidas == [13, 18]


def test_periodo_pedido_pelo_lead_e_respeitado():
    livres = calcular_disponibilidade([], TERCA, "manha")
    assert aplicar_escassez(livres, "manha")[0][1] == [8, 9]


def test_sem_periodo_pedido_os_dois_horarios_vem_de_faixas_diferentes():
    # Só a tarde livre naquele dia → cai para dois da mesma faixa, sem
    # inventar horário indisponível.
    assert agenda.escolher_horarios([13, 14, 15], "qualquer") == [13, 14]
    # Manhã e noite livres → um de cada.
    assert agenda.escolher_horarios([8, 9, 20, 21], "qualquer") == [8, 20]
    assert agenda.escolher_horarios([9], "qualquer") == [9]
    assert agenda.escolher_horarios([], "qualquer") == []


# --- Normalização defensiva -------------------------------------------------


def test_evento_invertido_ainda_bloqueia():
    # Extremos trocados: sem a normalização, `ev_i < fim and ev_f > inicio`
    # é falso para todo slot e o evento some da agenda. Depois de trocar,
    # ele é o intervalo [13:00, 14:00) e bloqueia só as 13h.
    eventos = [evento("2026-02-11T14:00:00-03:00", "2026-02-11T13:00:00-03:00")]
    livres = calcular_disponibilidade(eventos, TERCA, "tarde")
    assert livres[0][1] == [14, 15, 16, 17]


def test_evento_de_duracao_zero_bloqueia_o_slot_que_o_contem():
    eventos = [evento("2026-02-11T13:00:00-03:00", "2026-02-11T13:00:00-03:00")]
    livres = calcular_disponibilidade(eventos, TERCA, "tarde")
    assert 13 not in livres[0][1]


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


@pytest.mark.parametrize("bruto", ["qualquer", "Qualquer", " QUALQUER ", "QuAlQuEr"])
async def test_get_many_normaliza_o_periodo_antes_de_repassar(turno, ambiente, bruto):
    # O período cru circulava adiante: `escolher_horarios` compara com
    # "qualquer" e qualquer maiúscula do modelo desligava o viés de horário,
    # devolvendo `8, 9` em vez de `13, 18`.
    saida = await calendar_get_many.ainvoke({"periodo": bruto})
    assert saida.splitlines()[0].endswith(": 13, 18")


def test_normalizar_periodo():
    assert agenda.normalizar_periodo(" TARDE ") == "tarde"
    assert agenda.normalizar_periodo("Noite") == "noite"
    assert agenda.normalizar_periodo("madrugada") is None
    assert agenda.normalizar_periodo("") is None


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


async def test_agendar_sem_faturamento_agora_agenda(turno, ambiente):
    """A inversão, no nível da tool.

    O faturamento era portão: `calendar_agendar` recusava e mandava "volte à
    Fase 7". Era a maior fricção do funil — quem travava ali era perdido
    inteiro, sem reunião e sem dado. Agora ele é coletado DEPOIS, e é
    `update_crm` que classifica a faixa e aplica a consequência.

    Mudar só o prompt não teria bastado: o portão vivia aqui, e o modelo
    contornava passando `faturamento_mensal` para esta tool — que gravava o
    texto cru, sem faixa, e sem disparar desfecho nenhum.
    """
    ambiente.lead = lead(faturamento_mensal="")
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert "fase 7" not in saida.lower()
    assert ambiente.cliente.criados, "recusou agendar por falta de faturamento"


async def test_agendar_recusa_email_que_nao_e_email(turno, ambiente):
    ambiente.lead = lead(email=None)
    saida = await calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00", "email": "sim"}
    )
    assert "fase 6" in saida.lower()
    assert not ambiente.cliente.criados


async def test_agendar_cria_evento_e_grava_google_event_id(turno, ambiente):
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    esperado = agenda.event_id_deterministico(
        TELEFONE, datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    )

    assert len(ambiente.cliente.criados) == 1
    criado = ambiente.cliente.criados[0]
    assert criado["summary"] == "Consultoria de Alavancagem de Carreira - Ana"
    assert criado["participantes"] == ["ana@exemplo.com"]
    assert criado["inicio"] == datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    assert criado["fim"] == datetime(2026, 2, 12, 14, 0, tzinfo=FUSO)
    assert criado["event_id"] == esperado

    assert ambiente.gravacoes == [
        {
            "telefone": TELEFONE,
            "google_event_id": esperado,
            "email": "ana@exemplo.com",
        }
    ]
    assert "12/02" in saida
    assert "human_handover" not in saida


async def test_event_id_e_deterministico_por_lead_e_slot():
    slot = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    outro = datetime(2026, 2, 12, 14, 0, tzinfo=FUSO)

    # Mesmo lead, mesmo slot, mesmo id — é o que faz o retry colidir em 409
    # em vez de marcar duas consultorias.
    assert agenda.event_id_deterministico(
        TELEFONE, slot
    ) == agenda.event_id_deterministico(TELEFONE, slot)
    # E o telefone entra canonicalizado: as duas formas do 9º dígito são a
    # mesma pessoa e precisam gerar o mesmo id.
    assert agenda.event_id_deterministico(
        "+5511955554444", slot
    ) == agenda.event_id_deterministico("5511955554444", slot)

    assert agenda.event_id_deterministico(
        TELEFONE, slot
    ) != agenda.event_id_deterministico(TELEFONE, outro)
    assert agenda.event_id_deterministico(
        TELEFONE, slot
    ) != agenda.event_id_deterministico("+5511933332222", slot)

    # base32hex: o Google recusaria qualquer coisa fora de `a-v` e `0-9`.
    validar_event_id(agenda.event_id_deterministico(TELEFONE, slot))


async def test_agendar_avisa_quando_o_vinculo_nao_grava(turno, ambiente):
    # Lead ausente de leads_crm com e-mail e faturamento vindos por
    # argumento: o evento nasce na agenda e o UPDATE casa zero linhas.
    ambiente.gravacao_ok = False
    saida = await calendar_agendar.ainvoke(
        {
            "inicio": "2026-02-12T13:00",
            "email": "ana@exemplo.com",
        }
    )
    # O evento EXISTE — negar isso ao lead seria mentira na direção oposta.
    assert len(ambiente.cliente.criados) == 1
    assert "12/02" in saida
    assert "human_handover" in saida


def _conflito(event_id: str, inicio: str, fim: str, status: str = "confirmed"):
    """Corpo que o cliente devolve quando o insert bate em 409."""
    return {
        "id": event_id,
        "status": status,
        "summary": "Consultoria de Alavancagem de Carreira - Ana",
        "start": {"dateTime": inicio},
        "end": {"dateTime": fim},
    }


async def test_agendar_nao_confirma_id_que_colidiu_com_evento_cancelado(
    turno, ambiente
):
    # CASO A: o id determinístico já existe na Lixeira do Google (o lead
    # marcou e cancelou este mesmo slot). O insert responde 409 e o cliente
    # devolve o evento CANCELADO. Sem conferir, a tool diria "Agendado" e
    # gravaria vínculo para um evento que não existe mais — convite nenhum.
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = _conflito(
        colidido,
        "2026-02-12T13:00:00-03:00",
        "2026-02-12T14:00:00-03:00",
        status="cancelled",
    )

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    # Recriou com id aleatório — e o que foi gravado é o id novo, vivo.
    assert len(ambiente.cliente.criados) == 1
    assert ambiente.cliente.criados[0]["event_id"] is None
    assert ambiente.gravacoes[0]["google_event_id"] != colidido
    assert "12/02" in saida


async def test_agendar_nao_confirma_id_que_colidiu_com_outro_horario(turno, ambiente):
    # CASO B: o lead marcou às 13h de 12/02 (id derivado desse slot),
    # remarcou para 20/02 às 10h — o evento manteve o id — e o modelo chama
    # agendar no slot original. O 409 devolve o evento VIVO de 20/02. Sem
    # conferir, a tool diria "Agendado: quinta 12/02 às 13h" para uma agenda
    # que tem 20/02 às 10h.
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = _conflito(
        colidido, "2026-02-20T10:00:00-03:00", "2026-02-20T11:00:00-03:00"
    )

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert len(ambiente.cliente.criados) == 1
    assert ambiente.cliente.criados[0]["event_id"] is None
    assert ambiente.cliente.criados[0]["inicio"] == momento
    assert ambiente.gravacoes[0]["google_event_id"] != colidido
    assert "12/02" in saida


async def test_agendar_nao_mente_quando_nem_a_recriacao_confere(turno, ambiente):
    # Patológico: a colisão persiste e a recriação também devolve algo que
    # não é o que foi pedido. O único desfecho honesto é NÃO confirmar.
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = _conflito(
        colidido, "2026-02-12T13:00:00-03:00", "2026-02-12T14:00:00-03:00", "cancelled"
    )
    ambiente.cliente.conflitos["evento-novo"] = _conflito(
        "evento-novo", "2026-02-20T10:00:00-03:00", "2026-02-20T11:00:00-03:00"
    )

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert ambiente.gravacoes == []
    assert "agendado:" not in saida.lower()
    assert "human_handover" in saida


async def test_agendar_confere_na_fonte_quando_o_corpo_vem_sem_horario(turno, ambiente):
    # 2xx com corpo irreconhecível: o cliente cai no fallback `{"id": ...}`,
    # sem `start`. O evento na agenda está CERTO — confirmado, no horário
    # pedido. Sem ir à fonte, a conferência leria `inicio=None`, concluiria
    # divergência e criaria um SEGUNDO evento no mesmo horário.
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = {"id": colidido}
    ambiente.cliente.detalhe = {
        "status": "confirmed",
        "start": {"dateTime": "2026-02-12T13:00:00-03:00"},
    }

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert ambiente.cliente.criados == [], "não pode criar um segundo evento"
    assert ambiente.gravacoes[0]["google_event_id"] == colidido
    assert "12/02" in saida


async def test_agendar_recria_quando_a_fonte_diz_que_o_evento_esta_cancelado(
    turno, ambiente
):
    # Mesmo fallback sem `start`, mas a fonte diz `cancelled`: aí sim recria.
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = {"id": colidido}
    ambiente.cliente.detalhe = {
        "status": "cancelled",
        "start": {"dateTime": "2026-02-12T13:00:00-03:00"},
    }

    await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert ambiente.cliente.criados[0]["event_id"] is None


async def test_agendar_so_promete_convite_quando_o_evento_lista_o_lead(turno, ambiente):
    """Caminho normal: o insert saiu, com `attendees` — o convite foi.

    O par negativo é `test_409_que_confere_nao_promete_convite`: lá o
    `events.insert` não chega a acontecer, e a mesma frase seria mentira.
    """
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert "Convite enviado para ana@exemplo.com" in saida
    assert not saida.startswith(PREFIXO_INTERNO)
    assert "ATENÇÃO" not in saida


async def test_409_que_confere_nao_promete_convite(turno, ambiente):
    """409 resolvido lendo o evento: nada foi inserido, nenhum convite saiu.

    O cliente devolve o evento pré-existente, criado em outro turno e com
    os participantes que ELE tinha. Antes desta rodada a tool dizia
    "Convite enviado para ana@exemplo.com" — uma frase que o lead confere
    na caixa de entrada e não encontra.
    """
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = _conflito(
        colidido, "2026-02-12T13:00:00-03:00", "2026-02-12T14:00:00-03:00"
    )

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    # O agendamento vale: o evento está lá, no horário certo.
    assert ambiente.cliente.criados == []
    assert ambiente.gravacoes[0]["google_event_id"] == colidido
    assert "12/02" in saida
    # Mas o convite não foi prometido.
    assert "Convite enviado" not in saida
    assert "convite" in saida.lower()
    assert saida.startswith(PREFIXO_INTERNO)


async def test_409_com_lead_nos_attendees_confirma_o_convite(turno, ambiente):
    """O evento pré-existente já tem o lead: o convite chegou lá atrás."""
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    corpo = _conflito(
        colidido, "2026-02-12T13:00:00-03:00", "2026-02-12T14:00:00-03:00"
    )
    corpo["attendees"] = [{"email": "ANA@Exemplo.com"}]
    ambiente.cliente.conflitos[colidido] = corpo

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert "Convite enviado para ana@exemplo.com" in saida
    assert not saida.startswith(PREFIXO_INTERNO)


async def test_409_com_evento_vivo_em_outro_horario_avisa_do_orfao(turno, ambiente):
    """O colidido VIVO fica na agenda do Silvio, e ninguém mais o alcança.

    Recriar com id novo resolve a mentira do horário, mas o evento antigo
    continua marcado — e o cadastro passa a apontar para o novo, então nem
    `calendar_delete` chega nele. Sem este aviso, uma reunião fantasma fica
    na agenda de uma pessoa real e a resposta ao lead é "Agendado" liso.
    """
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = _conflito(
        colidido, "2026-02-20T10:00:00-03:00", "2026-02-20T11:00:00-03:00"
    )

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert ambiente.gravacoes[0]["google_event_id"] != colidido
    assert "12/02" in saida
    assert "human_handover" in saida
    assert saida.startswith(PREFIXO_INTERNO)


async def test_409_com_evento_cancelado_nao_avisa_de_orfao(turno, ambiente):
    """Lixeira não deixa órfão: o colidido já não existe na agenda.

    O par do teste acima. Avisar aqui mandaria alguém procurar um evento
    que não está lá — o aviso perde valor se dispara nos dois casos.
    """
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)
    ambiente.cliente.conflitos[colidido] = _conflito(
        colidido,
        "2026-02-12T13:00:00-03:00",
        "2026-02-12T14:00:00-03:00",
        status="cancelled",
    )

    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert "12/02" in saida
    assert "órfão" not in saida.lower()
    assert "duplicado" not in saida.lower()
    # Recriou com id aleatório e o insert levou os attendees.
    assert "Convite enviado para ana@exemplo.com" in saida
    assert "human_handover" not in saida


async def test_agendar_recusa_lead_que_ja_tem_consultoria_marcada(turno, ambiente):
    # Marcar um segundo evento deixaria o primeiro órfão — o cadastro só
    # guarda um id — e o lead com duas consultorias.
    ambiente.lead = lead(google_event_id="ev-existente")
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})

    assert not ambiente.cliente.criados
    assert not ambiente.gravacoes
    assert "calendar_update" in saida


async def test_agendar_ignora_o_proprio_evento_na_reconsulta(turno, ambiente):
    # Retry do mesmo agendamento: o evento já criado tem o id determinístico
    # e não pode bloquear a si mesmo, senão a segunda tentativa diria
    # "ocupado" para um horário que é do próprio lead.
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    ambiente.cliente.eventos = [
        evento(
            "2026-02-12T13:00:00-03:00",
            "2026-02-12T14:00:00-03:00",
            event_id=agenda.event_id_deterministico(TELEFONE, momento),
        )
    ]
    await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    assert ambiente.cliente.criados


async def test_agendar_persiste_email_novo_e_nao_toca_faturamento(turno, ambiente):
    """A tool grava o e-mail que acabou de receber, e só ele.

    `faturamento_mensal` saiu da assinatura: quem o grava é `update_crm`,
    depois de normalizar para a faixa. Deixar esta tool aceitá-lo era o furo
    que fazia o texto cru ("uns 3 mil por mês") chegar ao banco sem passar
    pela classificação — e, com ele, sem disparar o cancelamento da faixa
    abaixo do corte.
    """
    ambiente.lead = lead(email=None, faturamento_mensal=None)
    await calendar_agendar.ainvoke(
        {
            "inicio": "2026-02-12T13:00",
            "email": "novo@exemplo.com",
        }
    )
    assert ambiente.gravacoes[0]["email"] == "novo@exemplo.com"
    assert "faturamento_mensal" not in ambiente.gravacoes[0], (
        "a tool não pode mais escrever faturamento — quem faz isso é update_crm"
    )
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


async def test_agendar_recusa_microssegundo(turno, ambiente):
    # `minute` e `second` zerados, `microsecond` não: passaria e criaria um
    # evento às 13:00:00.5.
    saida = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00:00.500000"})
    assert not ambiente.cliente.criados
    assert "hora cheia" in saida.lower()


async def test_agendar_prefere_o_email_do_cadastro_ao_argumento(turno, ambiente):
    # A coluna é o que o lead realmente disse; o argumento é fallback. A
    # partir da Task 6 (que persiste esses campos) isto passa a ser o
    # caminho normal, e um valor inventado no turno não sobrepõe o cadastro.
    ambiente.lead = lead(email="cadastro@exemplo.com")
    await calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00", "email": "inventado@exemplo.com"}
    )
    assert ambiente.cliente.criados[0]["participantes"] == ["cadastro@exemplo.com"]
    assert ambiente.gravacoes[0]["email"] == "cadastro@exemplo.com"


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


async def test_delete_recusa_event_id_que_nao_e_do_lead(turno, ambiente):
    # O modelo alucina um id. Sem a checagem: 404 tolerado pelo cliente
    # idempotente → "Cancelado" para o lead → evento intacto na agenda do
    # Silvio → vínculo apagado → nunca mais dá para cancelar.
    ambiente.lead = lead(google_event_id="ev-do-lead")
    saida = await calendar_delete.ainvoke({"event_id": "ev-de-outra-pessoa"})

    assert ambiente.cliente.deletados == []
    assert ambiente.gravacoes == []
    assert "cancelado" not in saida.lower()
    assert "event_id" in saida.lower()


async def test_delete_aceita_event_id_redundante(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-do-lead")
    await calendar_delete.ainvoke({"event_id": "ev-do-lead"})
    assert ambiente.cliente.deletados == ["ev-do-lead"]


async def test_update_recusa_event_id_que_nao_e_do_lead(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-do-lead")
    saida = await calendar_update.ainvoke(
        {"novo_inicio": "2026-02-13T14:00", "event_id": "ev-de-outra-pessoa"}
    )
    assert ambiente.cliente.atualizados == []
    assert "event_id" in saida.lower()


async def test_get_event_recusa_event_id_que_nao_e_do_lead(turno, ambiente):
    # Com id de terceiro, a tool devolveria o `summary` cru de um
    # compromisso alheio para dentro da conversa com o lead.
    ambiente.lead = lead(google_event_id="ev-do-lead")
    saida = await calendar_get_event.ainvoke({"event_id": "ev-de-outra-pessoa"})
    assert "Consultoria de Alavancagem" not in saida
    assert "event_id" in saida.lower()


async def test_delete_avisa_quando_nao_consegue_limpar_o_vinculo(turno, ambiente):
    ambiente.lead = lead(google_event_id="ev-do-lead")
    ambiente.gravacao_ok = False
    saida = await calendar_delete.ainvoke({})
    assert ambiente.cliente.deletados == ["ev-do-lead"]
    assert "human_handover" in saida


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


async def test_delete_sem_evento_conhecido_manda_human_handover(turno, ambiente):
    # Não pode mandar "agende do zero": se o lead diz que tem reunião
    # marcada, o cenário provável é evento vivo na agenda sem vínculo no
    # cadastro, e agendar de novo deixa o órfão lá E cria um segundo.
    ambiente.lead = lead(google_event_id=None)
    saida = await calendar_delete.ainvoke({})
    assert not ambiente.cliente.deletados
    assert "não encontrei" in saida.lower()
    assert "human_handover" in saida
    assert "agende do zero" not in saida.lower()


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


# --- Marcador [sistema] -----------------------------------------------------

# Vocabulário que não existe para o lead. A Renata fala de "horário",
# "convite" e "reunião"; "human_handover", "event_id" e "cadastro" são
# processo interno, e o lead da EleveC recebendo qualquer um deles pelo
# WhatsApp é vazamento.
VOCABULARIO_INTERNO = (
    "human_handover",
    "calendar_agendar",
    "calendar_update",
    "calendar_delete",
    "event_id",
    "cadastro",
    "Pipedrive",
    # As recusas de portão mandam o agente VOLTAR a uma fase do SOP. É
    # instrução de roteiro, não fato — um lead lendo "volte à Fase 6" vê o
    # processo interno da EleveC.
    "volte à Fase",
)


def _saidas_de_agenda() -> list[tuple[str, str]]:
    """`(origem, texto)` de toda string de saída fixa do módulo."""
    saidas = [("FALHA_AGENDA", agenda.FALHA_AGENDA)]
    saidas += [
        (f"SEM_EVENTO_GRAVADO[{k}]", v) for k, v in agenda.SEM_EVENTO_GRAVADO.items()
    ]
    return saidas


@pytest.mark.parametrize("origem, texto", _saidas_de_agenda())
def test_saida_fixa_com_vocabulario_interno_vem_marcada(origem, texto):
    """A regra do prompt é sobre o prefixo, não sobre uma lista de frases.

    Quem escrever uma frase nova citando `human_handover` sem marcá-la
    reabre o vazamento em silêncio — este teste é o que faz isso doer.
    """
    if any(termo in texto for termo in VOCABULARIO_INTERNO):
        assert texto.startswith(PREFIXO_INTERNO), origem


async def test_nenhum_desfecho_de_agendar_vaza_vocabulario_interno(turno, ambiente):
    """Varre os desfechos de `calendar_agendar` que produzem texto interno."""
    momento = datetime(2026, 2, 12, 13, 0, tzinfo=FUSO)
    colidido = agenda.event_id_deterministico(TELEFONE, momento)

    cenarios: dict[str, Any] = {}

    # Órfão pós-409.
    ambiente.cliente.conflitos[colidido] = _conflito(
        colidido, "2026-02-20T10:00:00-03:00", "2026-02-20T11:00:00-03:00"
    )
    cenarios["orfao"] = await calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    ambiente.cliente.conflitos.clear()
    ambiente.lead = lead()

    # Vínculo não gravado.
    ambiente.gravacao_ok = False
    cenarios["sem_vinculo"] = await calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00"}
    )
    ambiente.gravacao_ok = True

    # Agenda fora do ar.
    ambiente.cliente.erro = GoogleCalendarError(500, "boom")
    cenarios["agenda_fora"] = await calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00"}
    )
    ambiente.cliente.erro = None

    # Portões de e-mail e faturamento.
    ambiente.lead = lead(email=None)
    cenarios["sem_email"] = await calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00"}
    )
    ambiente.lead = lead(faturamento_mensal="")
    cenarios["sem_faturamento"] = await calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00"}
    )

    for nome, saida in cenarios.items():
        if any(termo in saida for termo in VOCABULARIO_INTERNO):
            assert saida.startswith(PREFIXO_INTERNO), nome
