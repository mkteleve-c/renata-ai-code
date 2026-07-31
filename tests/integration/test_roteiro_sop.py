"""O SOP inteiro, ponta a ponta, pelo grafo da Renata.

Dois modos, e a diferença entre eles é o que cada um pode provar:

- **roteirizado** (roda sempre, sem chave): o modelo é um dublê que emite
  tool calls e respostas fixas. Prova o *encanamento* — contexto do lead
  chegando, tools ligadas, portões de código segurando, balões saindo,
  texto marcado `[sistema]` não vazando pelo caminho que o worker usa.
  Não prova nada sobre o modelo seguir o SOP: quem escreveu o roteiro fui
  eu.
- **live** (`OPENROUTER_LIVE_TESTS=1` + `OPENROUTER_API_KEY` válida): o
  modelo de verdade, no `OPENROUTER_MODEL` configurado. É o único que
  responde "a Renata segue o SOP?" e "ela pula o `update_crm`?". Rode com
  `-s` para ver o transcrito.

**Nada toca serviço externo em nenhum dos dois modos.** Google Calendar,
Pipedrive e o canal de saída do handover são dublês; o único recurso real
é o Postgres local, e o lead criado aqui é apagado no fim. O envio de
WhatsApp fica em `OUTBOUND_MODE=mock` por construção — o dublê do canal
nem chega a montar requisição.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from whatsapp_langchain.agents.catalog.elevec_sdr.faixas import FAIXAS
from whatsapp_langchain.agents.catalog.elevec_sdr.saida import extrair_baloes
from whatsapp_langchain.agents.catalog.elevec_sdr.tools import agenda, crm, handover
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.interno import PREFIXO_INTERNO
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.google_calendar import FUSO
from whatsapp_langchain.shared.phone import canonico_do_lead

TELEFONE = "+5511977771111"
# A forma que `leads_crm.phone` guarda: só dígitos e sem o 9º. Derivada,
# não literal — foi exatamente essa divergência que fez a primeira versão
# deste teste inserir um lead que nenhuma tool encontrava.
CANONICO = canonico_do_lead(TELEFONE)
NOME = "Marcos"
EMAIL = "marcos.almeida@empresa.com.br"
FATURAMENTO = "em torno de 25 mil por mês"
DEAL_ID = "990011"

LIVE = os.getenv("OPENROUTER_LIVE_TESTS") == "1"
sem_live = pytest.mark.skipif(
    not LIVE,
    reason="exige OPENROUTER_LIVE_TESTS=1 e OPENROUTER_API_KEY válida",
)

# Vocabulário que não existe para o lead. É a mesma lista que o bloco
# `### Resultado de tool (texto interno)` do prompt enumera.
VOCABULARIO_INTERNO = (
    PREFIXO_INTERNO,
    "human_handover",
    "update_crm",
    "calendar_get_many",
    "calendar_agendar",
    "calendar_update",
    "calendar_delete",
    "Pipedrive",
    "event_id",
    "google_event_id",
)


# --- Dublês dos serviços externos ------------------------------------------


class CalendarFalso:
    """Agenda vazia que aceita tudo. Registra o que a Renata pediu."""

    def __init__(self):
        self.criados: list[dict[str, Any]] = []
        self.listagens: list[tuple[datetime, datetime]] = []

    async def listar_eventos(self, inicio, fim, max_results=250):
        self.listagens.append((inicio, fim))
        return []

    async def criar_evento(
        self, summary, inicio, fim, participantes=None, event_id=None, **kwargs
    ):
        self.criados.append(
            {
                "summary": summary,
                "inicio": inicio,
                "participantes": list(participantes or []),
                "event_id": event_id,
            }
        )
        return {
            "id": event_id or "ev-novo",
            "status": "confirmed",
            "summary": summary,
            "start": {"dateTime": inicio.isoformat()},
            "end": {"dateTime": fim.isoformat()},
            "attendees": [{"email": e} for e in (participantes or [])],
        }

    async def atualizar_evento(self, event_id, **campos):
        return {"id": event_id}

    async def deletar_evento(self, event_id, notificar=True):
        return None

    async def obter_evento(self, event_id):
        return {"id": event_id, "status": "confirmed"}


class PipedriveFalso:
    def __init__(self):
        self.movidos: list[tuple[str, int]] = []

    async def mover_card(self, deal_id, stage_id):
        self.movidos.append((deal_id, stage_id))


class CanalFalso:
    def __init__(self):
        self.avisos: list[tuple[str, str]] = []

    async def send_message(self, to, body, **kwargs):
        self.avisos.append((to, body))


class Dubles:
    def __init__(self):
        self.calendario = CalendarFalso()
        self.pipedrive = PipedriveFalso()
        self.canal = CanalFalso()


@pytest.fixture
def dubles(monkeypatch):
    """Troca os três clientes externos. Nenhuma chamada sai da máquina."""
    d = Dubles()
    monkeypatch.setattr(agenda, "obter_cliente", lambda: d.calendario)
    monkeypatch.setattr(crm, "obter_cliente", lambda: d.pipedrive)
    monkeypatch.setattr(handover, "obter_cliente", lambda: d.canal)
    monkeypatch.setattr(settings, "pipedrive_api_token", "token-de-teste")
    monkeypatch.setattr(settings, "handover_notify_phone", "+5511999990000")
    return d


@pytest.fixture
async def lead_no_banco():
    """Lead real em `leads_crm`, na 5440. Apagado no teardown."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (CANONICO,))
        await conn.execute(
            "insert into leads_crm (phone, name, source, phase, pipedriveid)"
            " values (%s, %s, 'linkedin_form'::lead_source,"
            " 'iniciou_conversa'::lead_phase, %s)",
            (CANONICO, NOME, DEAL_ID),
        )
    yield
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (CANONICO,))


async def estado_do_lead() -> dict[str, Any]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select phase, email, faturamento_mensal, google_event_id,"
            " followup_active, agent_active from leads_crm where phone = %s",
            (CANONICO,),
        )
        linha = await cur.fetchone()
    campos = (
        "phase",
        "email",
        "faturamento_mensal",
        "google_event_id",
        "followup_active",
        "agent_active",
    )
    return dict(zip(campos, linha, strict=True)) if linha else {}


# --- Modelo roteirizado -----------------------------------------------------


class ModeloRoteirizado(BaseChatModel):
    """Dublê do LLM que devolve uma sequência fixa de `AIMessage`.

    Existe porque este ambiente não tem chave do OpenRouter, e um teste que
    só roda com credencial de terceiro não protege ninguém no dia a dia. O
    que ele prova é o encanamento: prompt interpolado chegando, tool calls
    executando de verdade contra as tools reais, portões de código
    segurando, `extrair_baloes` recortando a saída.

    `bind_tools` devolve `self`: o dublê ignora o schema porque quem decide
    as chamadas é o roteiro, não o modelo.
    """

    respostas: list[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "roteirizado"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self.respostas:
            raise AssertionError("o roteiro acabou e o grafo pediu outra resposta")
        return ChatResult(generations=[ChatGeneration(message=self.respostas.pop(0))])


def fala(baloes: list[str]) -> AIMessage:
    """Resposta final no formato que o output parser do n8n exige."""
    return AIMessage(content=json.dumps({"messages": baloes}, ensure_ascii=False))


def chama(nome: str, args: dict[str, Any], id_: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": nome, "args": args, "id": id_, "type": "tool_call"}],
    )


def proxima_quinta() -> datetime:
    """Um slot sempre válido: dia útil, hora cheia, dentro da janela D+1..D+4."""
    hoje = datetime.now(FUSO)
    for delta in range(1, 5):
        dia = hoje + timedelta(days=delta)
        if dia.weekday() < 5:
            return dia.replace(hour=14, minute=0, second=0, microsecond=0)
    raise AssertionError("janela de 4 dias sem nenhum dia útil")


# --- Execução de um roteiro -------------------------------------------------


class Turno:
    def __init__(self, texto: str):
        self.lead = texto
        self.tool_calls: list[str] = []
        self.tool_outs: list[tuple[str, str]] = []
        self.baloes: list[str] = []


async def conversar(grafo, roteiro: list[str], rotulo: str) -> list[Turno]:
    """Roda o roteiro turno a turno e imprime o transcrito."""
    config = {
        "configurable": {"thread_id": f"{TELEFONE}:elevec_sdr", "user_id": TELEFONE}
    }

    print(f"\n{'=' * 76}\n### {rotulo}\n{'=' * 76}")
    print(
        f"modelo: {settings.openrouter_model if LIVE else 'roteirizado (dublê)'} | "
        f"outbound: {settings.resolved_outbound_mode}"
    )

    turnos: list[Turno] = []
    vistos = 0
    for i, texto in enumerate(roteiro, 1):
        turno = Turno(texto)
        print(f"\n--- turno {i} ---\nLEAD: {texto}")

        resultado = await grafo.ainvoke(
            {"messages": [HumanMessage(content=texto)]}, config=config
        )
        mensagens = resultado["messages"]
        novas = mensagens[vistos:]
        vistos = len(mensagens)

        for msg in novas:
            for call in getattr(msg, "tool_calls", None) or []:
                turno.tool_calls.append(call["name"])
                args = json.dumps(call["args"], ensure_ascii=False)
                print(f"  TOOL CALL: {call['name']}({args})")
            if msg.__class__.__name__ == "ToolMessage":
                turno.tool_outs.append((msg.name, str(msg.content)))
                print(f"  TOOL OUT : {msg.name} -> {msg.content}")

        turno.baloes = extrair_baloes(mensagens[-1].content)
        for j, balao in enumerate(turno.baloes, 1):
            print(f"  RENATA [{j}/{len(turno.baloes)}]: {balao}")
        turnos.append(turno)

    print(f"\n--- estado final do lead ---\n{await estado_do_lead()}")
    return turnos


def conferir_nao_vazamento(turnos: list[Turno]) -> None:
    """Nenhum balão pode carregar texto interno de tool.

    É a asserção que o marcador `[sistema]` e a regra do prompt existem
    para sustentar. Vale nos dois modos: no roteirizado ela prova que o
    caminho `ToolMessage -> resposta final -> extrair_baloes` não concatena
    a saída da tool sozinho; no live, que o modelo obedece a regra.
    """
    for turno in turnos:
        for balao in turno.baloes:
            for termo in VOCABULARIO_INTERNO:
                assert termo not in balao, (
                    f"vazou {termo!r} para o lead no turno {turno.lead!r}: {balao!r}"
                )


# --- Roteiro A: SOP completo ------------------------------------------------

ROTEIRO_SOP = [
    "Oi, tudo bem?",
    "Meu desafio é que estou há 3 anos como gerente de operações e não "
    "consigo chegar a diretor. Sinto que travei.",
    "Já fiz MBA e entrego resultado, mas na promoção escolhem sempre outra "
    "pessoa. Me falta visibilidade e posicionamento com a diretoria.",
    "Sim, faz total sentido. É exatamente isso que eu preciso.",
    "Prefiro à tarde.",
    "Pode ser o primeiro horário que você falou.",
    EMAIL,
    f"Hoje meu faturamento fica {FATURAMENTO}.",
]


def _roteiro_do_dubl_sop() -> list[AIMessage]:
    """As respostas do dublê para o ROTEIRO_SOP.

    O turno 6 traz de propósito uma **violação do SOP**: o dublê chama
    `calendar_agendar` sem nunca ter perguntado e-mail nem faturamento. É
    o cenário que o prompt chama de "TERMINANTEMENTE PROIBIDO", e o teste
    existe para mostrar que quem o impede é o `if` da tool, não o
    parágrafo.
    """
    slot = proxima_quinta().strftime("%Y-%m-%dT%H:%M")
    return [
        fala(["Oi, Marcos!", "Posso te fazer uma pergunta rápida?"]),
        fala(["Me conta: qual o principal desafio na sua carreira hoje?"]),
        fala(
            [
                "Entendido, Marcos.",
                "A metodologia do Silvio te torna estratégico.",
                "Isso se alinha com o que você busca?",
            ]
        ),
        chama("update_crm", {"phase": "qualificado"}, "c1"),
        fala(["Que bom!", "Qual turno fica melhor: manhã, tarde ou noite?"]),
        chama("calendar_get_many", {"periodo": "tarde"}, "c2"),
        fala(["Consegui esses horários com o Silvio.", "Qual deles te atende?"]),
        # VIOLAÇÃO deliberada: agendar sem e-mail e sem faturamento.
        chama("calendar_agendar", {"inicio": slot}, "c3"),
        fala(["Combinado! Qual seu melhor e-mail?"]),
        fala(["Perfeito! Qual seu faturamento médio mensal hoje?"]),
        chama(
            "calendar_agendar",
            {"inicio": slot, "email": EMAIL, "faturamento_mensal": FATURAMENTO},
            "c4",
        ),
        chama("update_crm", {"phase": "agendou_sessao"}, "c5"),
        fala(["Feito, agendado!", "Te mandei o convite no e-mail.", "Até lá!"]),
    ]


async def test_roteiro_sop_pelo_grafo_com_modelo_roteirizado(dubles, lead_no_banco):
    """O encanamento inteiro, sem chave: tools, portões, banco e balões."""
    from langchain.agents import create_agent

    from whatsapp_langchain.agents.catalog.elevec_sdr.contexto import (
        criar_middleware_contexto,
    )
    from whatsapp_langchain.agents.catalog.elevec_sdr.prompts import SYSTEM_PROMPT
    from whatsapp_langchain.agents.catalog.elevec_sdr.tools import TOOLS_ELEVEC

    grafo = create_agent(
        model=ModeloRoteirizado(respostas=_roteiro_do_dubl_sop()),
        tools=TOOLS_ELEVEC,
        system_prompt=SYSTEM_PROMPT,
        middleware=[criar_middleware_contexto()],
        checkpointer=InMemorySaver(),
    )

    turnos = await conversar(grafo, ROTEIRO_SOP, "ROTEIRO A — SOP (dublê do modelo)")

    # O portão segurou a chamada prematura: a tool recusou e mandou de volta
    # para a Fase 6, sem criar nada na agenda.
    recusa = [
        saida
        for turno in turnos
        for nome, saida in turno.tool_outs
        if nome == "calendar_agendar" and "Fase 6" in saida
    ]
    assert recusa, "calendar_agendar deveria ter recusado sem e-mail"

    # E o agendamento válido, depois de e-mail e faturamento, criou UM evento.
    assert len(dubles.calendario.criados) == 1
    assert dubles.calendario.criados[0]["participantes"] == [EMAIL]

    # As duas transições de fase moveram o card nos estágios 12 e 13.
    assert dubles.pipedrive.movidos == [
        (DEAL_ID, settings.pipedrive_stage_qualificado),
        (DEAL_ID, settings.pipedrive_stage_agendado),
    ]

    estado = await estado_do_lead()
    assert estado["phase"] == "agendou_sessao"
    assert estado["email"] == EMAIL
    assert estado["google_event_id"]
    assert estado["followup_active"] is False

    # Múltiplos balões em pelo menos um turno — é a marca da Renata.
    assert any(len(turno.baloes) > 1 for turno in turnos)

    conferir_nao_vazamento(turnos)


# --- Roteiro B: desqualificação C1 ------------------------------------------

ROTEIRO_C1 = [
    "Oi",
    "Na real eu quero uma vaga. Estou desempregado e preciso que o Silvio me "
    "indique para alguma empresa, uma recolocação rápida mesmo.",
    "É, minha prioridade agora é conseguir a vaga, não desenvolvimento.",
]


async def test_roteiro_c1_desqualifica_sem_agendar(dubles, lead_no_banco):
    """C1 (recolocação): encerra educadamente, sem tocar a agenda."""
    from langchain.agents import create_agent

    from whatsapp_langchain.agents.catalog.elevec_sdr.contexto import (
        criar_middleware_contexto,
    )
    from whatsapp_langchain.agents.catalog.elevec_sdr.prompts import SYSTEM_PROMPT
    from whatsapp_langchain.agents.catalog.elevec_sdr.tools import TOOLS_ELEVEC

    respostas = [
        fala(["Oi, Marcos!", "Posso te fazer uma pergunta rápida?"]),
        fala(["Entendi.", "Nosso trabalho não é a recolocação direta."]),
        chama("update_crm", {"phase": "desqualificado"}, "d1"),
        fala(
            [
                "Compreendo perfeitamente, Marcos.",
                "Nossa metodologia pode não ser o caminho mais rápido para o "
                "seu objetivo imediato.",
                "Desejo muito sucesso na sua busca!",
            ]
        ),
    ]

    grafo = create_agent(
        model=ModeloRoteirizado(respostas=respostas),
        tools=TOOLS_ELEVEC,
        system_prompt=SYSTEM_PROMPT,
        middleware=[criar_middleware_contexto()],
        checkpointer=InMemorySaver(),
    )

    turnos = await conversar(grafo, ROTEIRO_C1, "ROTEIRO B — C1 (dublê do modelo)")

    assert dubles.calendario.criados == []
    # `desqualificado` não tem estágio no funil: o card não se move.
    assert dubles.pipedrive.movidos == []

    estado = await estado_do_lead()
    assert estado["phase"] == "desqualificado"
    assert estado["followup_active"] is False
    assert estado["google_event_id"] is None

    conferir_nao_vazamento(turnos)


# --- Live: o modelo de verdade ----------------------------------------------


@sem_live
async def test_roteiro_sop_com_llm_real(dubles, lead_no_banco, capsys):
    """O SOP inteiro com o `OPENROUTER_MODEL` configurado.

    É este que responde as perguntas que o dublê não responde: a Renata
    agenda antes de ter e-mail e faturamento? Ela chama `update_crm` nas
    transições? Ela repassa texto de tool ao lead? Rode com `-s`.
    """
    from whatsapp_langchain.agents.catalog.elevec_sdr.agent import build_graph

    grafo = build_graph(checkpointer=InMemorySaver())
    turnos = await conversar(grafo, ROTEIRO_SOP, "ROTEIRO A — SOP (LLM real)")

    chamadas = [nome for turno in turnos for nome in turno.tool_calls]

    # A agenda só foi consultada e escrita depois de e-mail e faturamento.
    assert len(dubles.calendario.criados) <= 1, "não pode marcar duas consultorias"
    if dubles.calendario.criados:
        estado = await estado_do_lead()
        assert estado["email"], "agendou sem e-mail no cadastro"
        # Faturamento NÃO é mais pré-condição do agendamento — a ordem foi
        # invertida (mudança documentada nº 6 do golden). O que ele precisa
        # ser é COLETADO, e isso o roteiro por faixa abaixo prova.
        assert estado["faturamento_mensal"], "não coletou faturamento depois de agendar"

    assert "calendar_get_many" in chamadas, "não consultou a agenda"
    assert "update_crm" in chamadas, "não registrou nenhuma transição de fase"
    assert any(len(turno.baloes) > 1 for turno in turnos), "resposta em balão único"

    conferir_nao_vazamento(turnos)


@sem_live
async def test_roteiro_c1_com_llm_real(dubles, lead_no_banco):
    """C1 com o modelo real: encerra sem agendar."""
    from whatsapp_langchain.agents.catalog.elevec_sdr.agent import build_graph

    grafo = build_graph(checkpointer=InMemorySaver())
    turnos = await conversar(grafo, ROTEIRO_C1, "ROTEIRO B — C1 (LLM real)")

    assert dubles.calendario.criados == [], "agendou um lead desqualificado"
    estado = await estado_do_lead()
    assert estado["google_event_id"] is None

    conferir_nao_vazamento(turnos)


# --- Live: o desfecho por faixa (mudança nº 6) ------------------------------

_ATE_O_AGENDAMENTO = ROTEIRO_SOP[:-1]  # tudo menos a fala do faturamento


@sem_live
@pytest.mark.parametrize(
    ("faixa", "fala", "espera_evento", "espera_handover", "fase"),
    [
        ("5-8k", "uns 6 mil por mês", True, False, "agendou_sessao"),
        ("8-25k", "gira em torno de 20 mil", True, False, "agendou_sessao"),
        (">25k", "uns 40 mil por mês", True, True, "agendou_sessao"),
        ("<5k", "uns 3 mil por mês", False, True, "desqualificado"),
    ],
    # ids ASCII e sem espaço: sem eles o pytest gera `m\xeas` e o node id não
    # casa em `-k` nem na linha de comando, o que impede rodar uma faixa
    # isolada — e isolar é justamente como se descobre vazamento entre casos.
    ids=["faixa_5_8k", "faixa_8_25k", "faixa_acima_25k", "faixa_abaixo_5k"],
)
async def test_desfecho_por_faixa_de_faturamento(
    dubles, lead_no_banco, faixa, fala, espera_evento, espera_handover, fase
):
    """As faixas do `nXuIqeQ0tBialBsR` (YAY FORMS), com o modelo real.

    **Isto é fumaça, não a garantia.** A garantia do desfecho é
    determinística e mora em `test_desfecho_faixa.py` (8 casos) e
    `test_faixa_faturamento.py` (40) — nenhum deles chama LLM, nenhum
    oscila. Este teste existe para responder outra pergunta: percorrendo a
    conversa inteira com o modelo de verdade, ela chega a coletar o
    faturamento e passar para `update_crm`?

    Ele PODE falhar por variância: o roteiro tem 8 falas enlatadas e, se o
    modelo perguntar outra coisa num turno do meio, as respostas deixam de
    encaixar e a conversa não chega ao agendamento. Falha aqui com o estado
    vazio (`iniciou_conversa`, sem e-mail, sem evento) é isso — não é
    regressão do desfecho. Falha com o estado PREENCHIDO e a consequência
    errada, sim.
    """
    from whatsapp_langchain.agents.catalog.elevec_sdr.agent import build_graph

    grafo = build_graph(checkpointer=InMemorySaver())
    turnos = await conversar(
        grafo,
        [*_ATE_O_AGENDAMENTO, f"Hoje meu faturamento fica {fala}."],
        f"FAIXA {faixa} — {fala}",
    )
    chamadas = [nome for turno in turnos for nome in turno.tool_calls]
    estado = await estado_do_lead()

    assert estado["faturamento_mensal"] in FAIXAS, (
        f"gravou {estado['faturamento_mensal']!r} em vez de uma faixa do funil"
    )

    if espera_evento:
        assert estado["google_event_id"], f"{faixa}: perdeu a reunião indevidamente"
        assert "calendar_delete" not in chamadas, f"{faixa}: cancelou sem motivo"
    else:
        assert estado["google_event_id"] is None, (
            "abaixo de R$ 5 mil não pode ficar com reunião marcada"
        )

    # O handover NÃO é mais chamada do modelo: `update_crm` o dispara por
    # dentro, a partir da faixa. O que se observa aqui é o efeito —
    # `agent_active` desligado — e não a boa vontade do modelo em lembrar.
    assert (estado["agent_active"] is False) is espera_handover, (
        f"{faixa}: handover esperado={espera_handover}, "
        f"agent_active={estado['agent_active']}, chamadas={chamadas}"
    )
    assert estado["phase"] == fase

    conferir_nao_vazamento(turnos)
