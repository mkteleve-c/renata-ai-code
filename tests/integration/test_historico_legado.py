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

`test_marca_sobrevive_a_falha_no_meio_do_turno` é a exceção: end-to-end,
via `create_agent` + `AsyncPostgresSaver` reais, com um modelo falso que
levanta exceção -- é o único jeito de provar a alegação central da task
("a marca protege contra reinjeção mesmo quando o turno falha depois dela
ser gravada") sem confiar só na leitura direta da tabela.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables.config import var_child_runnable_config

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


async def _rodar(canonico: str):
    token = var_child_runnable_config.set(_configurable(canonico))
    try:
        return await _mw().abefore_model(
            {"messages": [HumanMessage(content="oi")]}, None
        )
    finally:
        var_child_runnable_config.reset(token)


# --- Cenários centrais (Step 3 do plano) ------------------------------------


async def test_injeta_historico_no_primeiro_turno_sem_checkpoint():
    await _criar_lead(_P1)
    await _gravar_historico(
        _P1, [("human", "Oi, quero saber mais"), ("ai", "Oi! Qual seu momento?")]
    )

    resultado = await _rodar(_P1)

    assert resultado is not None
    mensagens = resultado["messages"]
    assert len(mensagens) == 2
    assert isinstance(mensagens[0], HumanMessage)
    assert mensagens[0].content == "Oi, quero saber mais"
    assert isinstance(mensagens[1], AIMessage)
    assert mensagens[1].content == "Oi! Qual seu momento?"
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
