"""`historico_legado` (Fase 4, Task 5) contra o Postgres real.

Duas fontes de verdade decidem se um turno injeta -- as duas testadas aqui
contra o banco de verdade, nunca monkeypatchadas:

1. `checkpoints` (tabela do `AsyncPostgresSaver`): `thread_id` sem nenhuma
   linha é sinal de "primeiro turno desta thread". Confirmado empiricamente
   (fora deste arquivo, contra este mesmo `create_agent`) que a tabela tem
   ZERO linhas para uma thread nova no exato instante em que o primeiro
   `before_model` roda -- o primeiro `checkpointer.put()` só acontece depois.
2. `leads_crm.metadata->>'historico_injetado'`: a marca que sobrevive entre
   tentativas, e é o que realmente impede reinjeção se o turno falhar no
   meio (ver o docstring do módulo).

Os testes chamam `mw.abefore_model(state, None)` diretamente -- mesmo padrão
de `tests/integration/test_context_middleware.py` para `trim.before_model`
--, com o `configurable` do turno simulado via o `ContextVar` que
`telefone_do_turno`/`_thread_id_do_turno` leem (`var_child_runnable_config`).
Isso evita depender de uma chamada real ao modelo para testar a decisão de
injetar -- o `runtime` nunca é lido pelo middleware, só o `ContextVar` e o
Postgres.

`test_marca_sobrevive_a_falha_no_meio_do_turno` e
`test_modelo_recebe_historico_antes_da_mensagem_nova` são a exceção:
end-to-end, via `create_agent` + `AsyncPostgresSaver` reais -- são o único
jeito de provar, respectivamente, que a marca protege contra reinjeção
quando o turno falha DEPOIS dela ser gravada, e que o MODELO (não só o
dict que o middleware devolve, não só o checkpoint depois de mesclado) de
fato recebe o histórico ANTES da mensagem nova.

FIX ROUND 2 -- o Crítico que motivou `test_modelo_recebe_historico_antes_
da_mensagem_nova`: `test_injeta_historico_no_primeiro_turno_sem_checkpoint`
inspecionava só o DICT devolvido pelo middleware, nunca o `state` depois do
merge pelo reducer `add_messages` (que ANEXA, não insere na posição que o
dict sugere). Isso escondeu que a primeira versão do middleware devolvia
`{"messages": mensagens}` sem o `RemoveMessage(id=REMOVE_ALL_MESSAGES)` na
frente -- o histórico entrava DEPOIS da mensagem nova do lead, não antes.
Toda a fronteira de teste deste arquivo estava do lado de cá do reducer;
os dois testes abaixo passam por ele.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.graph.message import add_messages

from whatsapp_langchain.agents.middleware.historico_legado import (
    criar_middleware_historico_legado,
)
from whatsapp_langchain.shared.db import get_pool, open_checkpointer

_PREFIXO_TESTE = "5511990100"
_P1 = f"{_PREFIXO_TESTE}01"
_P2 = f"{_PREFIXO_TESTE}02"
_AGENT_ID = "elevec_sdr"


def _thread_id(canonico: str) -> str:
    return f"+{canonico}:{_AGENT_ID}"


def _configurable(canonico: str) -> dict:
    return {
        "configurable": {
            "thread_id": _thread_id(canonico),
            "user_id": f"+{canonico}",
        }
    }


@pytest.fixture(autouse=True)
async def limpar():
    async def apagar():
        pool = await get_pool()
        async with pool.connection() as conn:
            # cascata apaga legacy_chat_history junto (FK ON DELETE CASCADE,
            # migração 015)
            await conn.execute(
                "delete from leads_crm where phone like %s", (f"{_PREFIXO_TESTE}%",)
            )
            await conn.execute(
                "delete from checkpoint_writes where thread_id like %s",
                (f"+{_PREFIXO_TESTE}%",),
            )
            await conn.execute(
                "delete from checkpoints where thread_id like %s",
                (f"+{_PREFIXO_TESTE}%",),
            )

    await apagar()
    yield
    await apagar()


async def _criar_lead(phone: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase, followup_active, agent_active) "
            "values (%s, 'iniciou_conversa', true, true) "
            "on conflict (phone) do nothing",
            (phone,),
        )


async def _gravar_historico(phone: str, turnos: list[tuple[str, str]]) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        for ordem, (papel, conteudo) in enumerate(turnos, start=1):
            await conn.execute(
                "insert into legacy_chat_history (phone, ordem, papel, conteudo) "
                "values (%s, %s, %s, %s)",
                (phone, ordem, papel, conteudo),
            )


async def _metadata_marcado(phone: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select coalesce(metadata->>'historico_injetado', 'false') = 'true' "
            "from leads_crm where phone = %s",
            (phone,),
        )
        linha = await cur.fetchone()
    return bool(linha and linha[0])


async def _inserir_checkpoint_falso(thread_id: str) -> None:
    """Insere uma linha mínima em `checkpoints` -- só o suficiente para
    `_tem_checkpoint` enxergar "esta thread já tem checkpoint", sem passar
    pelo `AsyncPostgresSaver` de verdade (que exigiria uma execução completa
    do grafo). O conteúdo de `checkpoint`/`metadata` não importa -- a query
    do middleware só checa EXISTÊNCIA de linha."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
            "values (%s, '', 'fake-checkpoint', '{}'::jsonb, '{}'::jsonb)",
            (thread_id,),
        )


def _mw():
    return criar_middleware_historico_legado()


async def _rodar(canonico: str, mensagem_nova: HumanMessage | None = None):
    token = var_child_runnable_config.set(_configurable(canonico))
    try:
        return await _mw().abefore_model(
            {"messages": [mensagem_nova or HumanMessage(content="oi")]}, None
        )
    finally:
        var_child_runnable_config.reset(token)


# --- Cenários centrais (Step 3 do plano) ------------------------------------


async def test_injeta_historico_no_primeiro_turno_sem_checkpoint():
    """A fronteira certa: aplica o reducer `add_messages` real sobre o
    dict devolvido -- é o que o LangGraph faz de verdade para produzir o
    `state` final. Checar só `resultado["messages"]` cru (a versão
    anterior deste teste) não pega a inversão de ordem -- ver FIX ROUND 2
    no cabeçalho do módulo."""
    await _criar_lead(_P1)
    await _gravar_historico(
        _P1, [("human", "Oi, quero saber mais"), ("ai", "Oi! Qual seu momento?")]
    )
    mensagem_nova = HumanMessage(content="Pode ser amanhã às 15h?")

    resultado = await _rodar(_P1, mensagem_nova=mensagem_nova)

    assert resultado is not None
    final = add_messages([mensagem_nova], resultado["messages"])

    assert [type(m).__name__ for m in final] == [
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert final[0].content == "Oi, quero saber mais"
    assert final[1].content == "Oi! Qual seu momento?"
    # a mensagem NOVA do lead continua por último -- é a resposta que o
    # modelo precisa dar, não uma fala antiga da Renata para continuar.
    assert final[2].content == "Pode ser amanhã às 15h?"
    assert await _metadata_marcado(_P1)


async def test_segunda_chamada_nao_injeta_de_novo():
    """A marca é o que impede a reinjeção -- não depende de um checkpoint
    real ter sido criado entre as duas chamadas (aqui não existe nenhum)."""
    await _criar_lead(_P1)
    await _gravar_historico(_P1, [("human", "oi"), ("ai", "Oi!")])

    primeira = await _rodar(_P1)
    segunda = await _rodar(_P1)

    assert primeira is not None
    assert segunda is None


async def test_lead_sem_historico_nao_quebra():
    await _criar_lead(_P2)

    resultado = await _rodar(_P2)

    assert resultado is None
    # decisão já foi tomada (não há o que injetar) -- marca do mesmo jeito,
    # para não reconsultar `legacy_chat_history` a cada turno futuro.
    assert await _metadata_marcado(_P2)


async def test_ignora_thread_com_checkpoint_existente():
    await _criar_lead(_P1)
    await _gravar_historico(_P1, [("human", "oi"), ("ai", "Oi!")])
    await _inserir_checkpoint_falso(_thread_id(_P1))

    resultado = await _rodar(_P1)

    assert resultado is None
    # nem chegou a decidir marcar -- a thread já tinha conversa em andamento,
    # a decisão sobre injetar histórico não é deste middleware.
    assert not await _metadata_marcado(_P1)


async def test_sem_configurable_no_turno_nao_quebra():
    """`thread_id`/`user_id` ausentes do `configurable` (ex.: chamada fora
    do fluxo normal do worker) -- o middleware não injeta, mas também não
    levanta."""
    token = var_child_runnable_config.set({"configurable": {}})
    try:
        resultado = await _mw().abefore_model(
            {"messages": [HumanMessage(content="oi")]}, None
        )
    finally:
        var_child_runnable_config.reset(token)

    assert resultado is None


async def test_lead_sem_linha_em_leads_crm_nao_quebra():
    """Telefone que normaliza mas não tem lead em `leads_crm` -- não deveria
    acontecer em produção (o gate sempre cria o lead antes do agente rodar),
    mas o middleware não pode derrubar o turno se acontecer."""
    resultado = await _rodar(f"{_PREFIXO_TESTE}09")

    assert resultado is None


# --- A marca sobrevive a uma falha no meio do turno (Step 2 do plano) ------


class _ModeloQueFalhaDepoisDoPrimeiroToken:
    """Simula "LLM fora do ar" -- levanta assim que é invocado, DEPOIS que
    todo o encadeamento de `before_model` (inclusive a injeção de histórico
    e a gravação da marca) já rodou."""

    def bind_tools(self, *args, **kwargs):
        return self

    def bind(self, *args, **kwargs):
        return self

    async def ainvoke(self, *args, **kwargs):
        raise RuntimeError("LLM fora do ar (simulado)")

    def invoke(self, *args, **kwargs):
        raise RuntimeError("LLM fora do ar (simulado)")


async def test_marca_sobrevive_a_falha_no_meio_do_turno():
    """Prova de ponta a ponta da alegação central da task: se o turno falhar
    DEPOIS que a marca foi gravada, a marca persiste -- uma reexecução não
    reinjeta o histórico."""
    from langchain.agents import create_agent

    canonico = _P1
    await _criar_lead(canonico)
    await _gravar_historico(canonico, [("human", "oi"), ("ai", "Oi! Tudo bem?")])

    stack, checkpointer = await open_checkpointer()
    await checkpointer.setup()

    agent = create_agent(
        model=_ModeloQueFalhaDepoisDoPrimeiroToken(),
        tools=[],
        system_prompt="teste",
        middleware=[criar_middleware_historico_legado()],
        checkpointer=checkpointer,
    )

    config = {
        "configurable": {
            "thread_id": _thread_id(canonico),
            "user_id": f"+{canonico}",
        }
    }

    with pytest.raises(RuntimeError, match="LLM fora do ar"):
        await agent.ainvoke({"messages": [HumanMessage(content="oi")]}, config=config)

    # a marca sobreviveu à falha do modelo
    assert await _metadata_marcado(canonico)

    # uma segunda tentativa (reprocessamento da fila) não reinjeta --
    # confirmado chamando o middleware isoladamente com o MESMO configurable,
    # que é o que o worker faria numa nova tentativa.
    resultado_retry = await _rodar(canonico)
    assert resultado_retry is None

    await stack.aclose()


# --- O que o MODELO recebe, na ordem (Fix round 2) --------------------------


class _ModeloCapturaMensagens:
    """Fake model que GRAVA a lista de mensagens de cada chamada -- é o
    único jeito de provar o que o modelo de fato recebe. Inspecionar o
    dict que o middleware devolve, ou o `state` no checkpoint depois do
    turno, não pega uma inversão de ordem que só existe ENTRE essas duas
    coisas (no merge do reducer) -- foi exatamente esse ponto cego que
    deixou passar o Crítico do fix round 2."""

    def __init__(self):
        self.chamadas: list[list] = []

    def bind_tools(self, *args, **kwargs):
        return self

    def bind(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        self.chamadas.append(list(messages))
        return AIMessage(content='{"messages": ["resposta simulada"]}')

    def invoke(self, messages, *args, **kwargs):
        self.chamadas.append(list(messages))
        return AIMessage(content='{"messages": ["resposta simulada"]}')


async def test_modelo_recebe_historico_antes_da_mensagem_nova():
    """Ponta a ponta com `create_agent` real: roda o grafo inteiro e
    inspeciona a lista de mensagens que o MODELO recebeu na chamada -- não
    o dict devolvido pelo middleware, não o state pós-turno. É a fronteira
    que faltava (apontada no fix round 2): sem ela, uma inversão de ordem
    introduzida entre o middleware e o merge do reducer passa por todos os
    testes que só olham um lado ou outro dessa fronteira."""
    from langchain.agents import create_agent

    canonico = _P1
    await _criar_lead(canonico)
    await _gravar_historico(
        canonico,
        [
            ("human", "Oi, vi o anúncio"),
            ("ai", "Oi, Diego! Qual seu momento de carreira?"),
            ("human", "Faturo 30 mil por mês"),
            ("ai", "Posso te chamar de Diego mesmo?"),
        ],
    )

    stack, checkpointer = await open_checkpointer()
    await checkpointer.setup()

    modelo = _ModeloCapturaMensagens()
    agent = create_agent(
        model=modelo,
        tools=[],
        system_prompt="SOP da Renata",
        middleware=[criar_middleware_historico_legado()],
        checkpointer=checkpointer,
    )

    mensagem_nova = HumanMessage(content="Pode ser amanhã às 15h?")
    config = {
        "configurable": {
            "thread_id": _thread_id(canonico),
            "user_id": f"+{canonico}",
        }
    }

    await agent.ainvoke({"messages": [mensagem_nova]}, config=config)

    assert len(modelo.chamadas) == 1
    recebidas = modelo.chamadas[0]

    conteudos = [getattr(m, "content", None) for m in recebidas]
    tipos = [type(m).__name__ for m in recebidas]

    # SystemMessage (SOP) + os 4 turnos legados, em ordem cronológica +
    # a mensagem NOVA do lead por ÚLTIMO -- nunca uma fala antiga da
    # Renata por último (que seria lida como prefill pelo modelo).
    assert tipos == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert conteudos[1] == "Oi, vi o anúncio"
    assert conteudos[2] == "Oi, Diego! Qual seu momento de carreira?"
    assert conteudos[3] == "Faturo 30 mil por mês"
    assert conteudos[4] == "Posso te chamar de Diego mesmo?"
    assert conteudos[-1] == "Pode ser amanhã às 15h?"
    assert tipos[-1] == "HumanMessage"

    await stack.aclose()
