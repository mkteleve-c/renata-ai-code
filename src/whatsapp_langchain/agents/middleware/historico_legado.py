"""Injeta o histórico legado do Supabase no primeiro turno pós-cutover (Fase 4, Task 5).

Sem isto, os leads ativos migrados (`iniciou_conversa`/`qualificado` --
529 na medição da Fase 4) recebem a saudação da Fase 1 outra vez no primeiro
turno depois do cutover -- inclusive quem já passou pelo portão de
faturamento e já disse o próprio nome. Para o lead, é o robô esquecendo tudo.

`legacy_chat_history` (migração 015 + `scripts/migrar_supabase.py::
importar_historico`, Task 5) guarda até 12 turnos de conversa real por
telefone canônico -- só `human` e `ai` final, sem ruído de tool (ver o
cabeçalho da seção "Histórico e continuidade" em `migrar_supabase.py`).
Este middleware carrega esse histórico e injeta no ESTADO do grafo, como
mensagens reais (`HumanMessage`/`AIMessage`) -- diferente de
`contexto.py::criar_middleware_contexto`, que injeta CONTEXTO no system
prompt via `@dynamic_prompt`. Aqui é histórico de CONVERSA: precisa
aparecer para o modelo (e para quem inspecionar o checkpoint depois) como
turnos que realmente aconteceram, com papel humano/IA -- não como
instrução.

Duas condições, as duas verificadas com o Postgres real, decidem se um
turno injeta:

1. `thread_id` do turno ainda NÃO tem nenhum checkpoint gravado -- medido
   empiricamente contra este mesmo `create_agent`/`AsyncPostgresSaver`
   (ver `tests/integration/test_historico_legado.py`): no exato instante em
   que o PRIMEIRO `before_model` de uma thread nova executa, a tabela
   `checkpoints` do Postgres tem ZERO linhas para aquele `thread_id` -- o
   primeiro `checkpointer.put()` só acontece depois que este nó conclui. A
   partir da segunda mensagem em diante, `checkpoints` já tem linhas antes
   mesmo do `before_model` rodar. Checar `state["messages"]` (ex.:
   `len(...) == 1`) daria o mesmo sinal só por acidente -- e quebraria no
   caminho de teste sem checkpointer (`webhook_sync`); checar o Postgres é
   o sinal que corresponde exatamente ao que a task pede ("thread_id sem
   checkpoint").
2. `leads_crm.metadata->>'historico_injetado'` ainda não é `'true'` para o
   telefone canônico do turno.

A condição 2 é a que realmente PROTEGE contra duplicação: a marca é gravada
ANTES do turno prosseguir -- antes de este middleware devolver a atualização
de `state` para o grafo, ou seja, antes do modelo ser invocado. Se o turno
falhar DEPOIS disso (LLM fora do ar, timeout, erro de tool), a marca já
está persistida em `leads_crm`; a PRÓXIMA tentativa (reprocessamento da
fila) vê a marca e não injeta de novo -- o lead nunca vê a própria conversa
duplicada. A alternativa -- gravar a marca só depois que o turno inteiro
tiver sucesso -- é exatamente o bug que a task pede para evitar: uma
falha no meio faria a tentativa seguinte injetar outra vez, e o lead veria
os mesmos 12 turnos empilhados duas vezes.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain.agents import AgentState
from langchain.agents.middleware import before_model
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.runtime import Runtime
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.phone import canonicalizar, from_e164

logger = structlog.get_logger()

_MARCA_METADATA = "historico_injetado"


def _thread_id_do_turno() -> str | None:
    """`thread_id` do `configurable` do invoke atual, via o mesmo `ContextVar`
    que `contexto.py::_configurable` lê -- não há como chegar no `thread_id`
    de dentro de um `@before_model` de outro jeito neste projeto: `Runtime`
    só expõe `context` (schema explícito, não usado aqui), nunca
    `configurable`.
    """
    config = var_child_runnable_config.get(None)
    if isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict):
            thread_id = configurable.get("thread_id")
            if thread_id:
                return str(thread_id)
    return None


async def _tem_checkpoint(pool: AsyncConnectionPool, thread_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select 1 from checkpoints where thread_id = %s limit 1", (thread_id,)
        )
        linha = await cur.fetchone()
    return linha is not None


async def _ja_injetado(pool: AsyncConnectionPool, phone: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select coalesce(metadata->>%s, 'false') = 'true' "
            "from leads_crm where phone = %s",
            (_MARCA_METADATA, phone),
        )
        linha = await cur.fetchone()
    return bool(linha and linha[0])


async def _carregar_historico(
    pool: AsyncConnectionPool, phone: str
) -> list[tuple[str, str]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select papel, conteudo from legacy_chat_history "
            "where phone = %s order by ordem",
            (phone,),
        )
        linhas = await cur.fetchall()
    return [(papel, conteudo) for papel, conteudo in linhas]


async def _marcar_injetado(pool: AsyncConnectionPool, phone: str) -> None:
    """Mescla `historico_injetado: true` no `metadata` de `leads_crm`, sem
    apagar o resto -- mesmo padrão de merge-em-Python de
    `shared/leads.py::_fundir_metadata` (ler, mesclar em dict, regravar com
    `Jsonb`): não existe precedente de `jsonb_set` atômico em SQL para
    `metadata` neste projeto, e um lead sem linha em `leads_crm` (nunca
    deveria acontecer -- o telefone vem de `telefone_do_turno`, que só
    existe porque o gate já criou o lead) simplesmente não tem o que
    marcar, e a query não afeta nenhuma linha.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select metadata from leads_crm where phone = %s", (phone,)
        )
        linha = await cur.fetchone()
        if linha is None:
            logger.warning("historico_legado_lead_ausente", phone=phone)
            return
        atual = linha[0] if isinstance(linha[0], dict) else {}
        novo = {**atual, _MARCA_METADATA: True}
        await conn.execute(
            "update leads_crm set metadata = %s where phone = %s",
            (Jsonb(novo), phone),
        )


def criar_middleware_historico_legado():
    """Cria o middleware `before_model` que injeta o histórico legado.

    `telefone_do_turno` é importado AQUI DENTRO, não no topo do módulo, de
    propósito: `agents/middleware/` é importado por
    `agents/catalog/elevec_sdr/agent.py`, que por sua vez é importado pelo
    `__init__.py` do pacote `elevec_sdr` -- e `telefone_do_turno` mora em
    `elevec_sdr/contexto.py`, dentro do MESMO pacote. Um `import` no topo
    deste arquivo fecharia um ciclo (`elevec_sdr` -> `agent.py` ->
    `middleware.historico_legado` -> `elevec_sdr.contexto` -> reentra em
    `elevec_sdr/__init__.py` ainda incompleto) e quebraria com
    `ImportError: cannot import name ... (most likely due to a circular
    import)`. Adiar o import para dentro da factory -- chamada por
    `build_graph()` depois que todos os módulos já terminaram de carregar
    -- resolve sem duplicar `telefone_do_turno` nem mudar onde ele mora.
    """
    from whatsapp_langchain.agents.catalog.elevec_sdr.contexto import (
        telefone_do_turno,
    )

    @before_model
    async def historico_legado(
        state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        thread_id = _thread_id_do_turno()
        telefone_e164 = telefone_do_turno()
        if thread_id is None or telefone_e164 is None:
            logger.warning("historico_legado_sem_configurable_no_turno")
            return None

        canonico = canonicalizar(telefone_e164) or from_e164(telefone_e164)
        if canonico is None:
            return None

        pool = await get_pool()

        if await _tem_checkpoint(pool, thread_id):
            return None

        if await _ja_injetado(pool, canonico):
            return None

        historico = await _carregar_historico(pool, canonico)

        # A marca é gravada AQUI -- antes de devolver a atualização de
        # `state` para o grafo, ou seja, antes do modelo ser invocado. Ver
        # o docstring do módulo: é o que impede reinjeção se o restante do
        # turno falhar.
        await _marcar_injetado(pool, canonico)

        if not historico:
            return None

        mensagens = [
            HumanMessage(content=conteudo)
            if papel == "human"
            else AIMessage(content=conteudo)
            for papel, conteudo in historico
        ]
        logger.info("historico_legado_injetado", phone=canonico, turnos=len(mensagens))
        return {"messages": mensagens}

    return historico_legado
