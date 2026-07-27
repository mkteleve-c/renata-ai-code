"""Agente elevec_sdr - Renata, assistente de pré-vendas da EleveC.

Agente simples usando create_agent do LangChain 1.0. Portado do workflow n8n
`#1 Agente SDR | 10/02/26 | V2.2` (ver docs/evidencias/prompt-renata-n8n.md).

Este arquivo contém a factory `build_graph()`. Para langgraph dev,
veja graph.py que exporta a variável `graph`.

Diferenças em relação ao illumi_assistant/rhawk_assistant nesta fase:
- Tools de agenda, CRM e handover ligadas. A Renata não usa
  save_memory/read_memory mesmo se um store for passado, para paridade
  com o n8n, que não tem memória cross-thread.
- temperature=0.3, replicando a configuração do nó AI Agent no n8n
  (ver docs/evidencias/prompt-renata-n8n.md) — um agente cujo valor é
  seguir o SOP à risca não deve rodar no default do provider.

Configuração via .env:
    OPENROUTER_API_KEY=sk-or-...       # API key do OpenRouter
    OPENROUTER_MODEL=anthropic/...     # Modelo principal
    CONTEXT_STRATEGY=trim              # trim | summarize | none
    TRIM_KEEP_TURNS=5                  # Turnos a manter (trim)
    SUMMARIZE_TRIGGER_TOKENS=4000      # Tokens antes de sumarizar
    SUMMARIZE_KEEP_MESSAGES=10         # Mensagens após sumarização
    SUMMARIZE_MODEL=anthropic/...      # Modelo para sumarização
"""

from langchain.agents import create_agent
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from whatsapp_langchain.agents.middleware import get_context_middleware
from whatsapp_langchain.agents.middleware.historico_legado import (
    criar_middleware_historico_legado,
)
from whatsapp_langchain.shared.llm import create_chat_model

from .contexto import criar_middleware_contexto
from .prompts import SYSTEM_PROMPT
from .tools import TOOLS_ELEVEC


def build_graph(
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
):
    """Constrói o agente elevec_sdr (Renata).

    O agente usa middleware de contexto configurável via CONTEXT_STRATEGY:
    - trim: Remove mensagens antigas (custo zero, perde contexto)
    - summarize: Sumariza mensagens antigas (custo extra, preserva contexto)
    - none: Sem gerenciamento de contexto

    As sete tools (`calendar_get_many`, `calendar_agendar`,
    `calendar_update`, `calendar_delete`, `calendar_get_event`,
    `update_crm`, `human_handover`) entram sempre: elas resolvem credencial
    e lead em tempo de chamada e devolvem mensagem — nunca exceção — quando
    algo falta, então ligar condicionalmente só esconderia a causa do
    agente.

    Não usa memória semântica — paridade com o n8n, que não tem memória
    cross-thread.

    Args:
        checkpointer: Checkpointer para persistência de estado.
                      None em dev (in-memory), PostgresSaver em prod.
        store: Store para memória semântica cross-thread. Recebido por
               compatibilidade com o loader, mas não usado — a Renata
               não tem tools de memória.

    Returns:
        CompiledStateGraph: Agente compilado pronto para uso.
    """
    # Modelo principal com rate limiter centralizado (shared/llm.py).
    # temperature=0.3 replica a configuração do nó AI Agent no n8n.
    model = create_chat_model(temperature=0.3)

    # Middleware de contexto baseado em CONTEXT_STRATEGY + histórico legado do
    # Supabase (Fase 4, Task 5) + o contexto do lead, que reinterpola
    # {nome}/{origem}/{telefone}/{data_hoje} a cada chamada ao modelo (ver
    # contexto.py). Ordem importa, e os `@before_model` desta lista RODAM NA
    # ORDEM EM QUE APARECEM AQUI (confirmado com um probe de dois
    # `@before_model` reais) -- trim/summarize primeiro, histórico legado
    # depois. No turno 1 de uma thread nova isso não muda nada na prática:
    # quando o trim roda, `state["messages"]` só tem a mensagem que acabou
    # de chegar (nada para cortar); o histórico legado roda em seguida e
    # injeta os até 12 turnos ANTES dela (ver FIX ROUND 2 em
    # `historico_legado.py` -- o middleware reordena o próprio state,
    # não depende de rodar antes ou depois do trim para isso). A partir do
    # turno 2, o histórico já injetado faz parte do state persistido no
    # checkpoint, e o trim passa a tratá-lo como qualquer turno anterior,
    # normalmente. `criar_middleware_contexto` é `@dynamic_prompt` (outra
    # camada, não concorre por posição no encadeamento de `before_model`).
    middleware = [
        *get_context_middleware(),
        criar_middleware_historico_legado(),
        criar_middleware_contexto(),
    ]

    return create_agent(
        model=model,
        tools=TOOLS_ELEVEC,
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        checkpointer=checkpointer,
        store=store,
    )
