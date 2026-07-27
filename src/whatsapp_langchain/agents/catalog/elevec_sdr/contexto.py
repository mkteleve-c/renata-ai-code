"""Contexto do lead injetado no prompt da Renata a cada turno.

O n8n resolvia `{{ $('Fields').item.json.pushName }}` e companhia a cada
execução do workflow. Aqui o equivalente é um middleware: `load_graph` roda
por mensagem, mas o telefone só existe em tempo de invoke (no `configurable`),
não na construção do grafo — então quem interpola é o middleware, que enxerga
o `configurable` do turno.

Mecanismo: `@dynamic_prompt`, que é `wrap_model_call` — troca o
`system_message` da requisição ao modelo, sem gravar nada no state. O contexto
chega como instrução (igual ao n8n), não como mensagem no histórico, e não
sobra no checkpointer para o turno seguinte.

`@before_model` não serviria: ele só devolve state, e o `system_prompt` que o
`create_agent` injeta continuaria com `{nome}`/`{origem}`/`{telefone}`/
`{data_hoje}` crus na frente do modelo — o lead veria os placeholders
literais coexistindo com uma segunda mensagem trazendo os valores reais.

Interpolação é `str.replace` nos quatro tokens exatos, nunca `.format()`:
o SOP tem a chave literal `{Nome}` (maiúsculo) no script da Fase 1 ("Oi,
{Nome}!"), que é conteúdo do prompt e faria `.format()` estourar com
`KeyError: 'Nome'`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_core.runnables.config import var_child_runnable_config
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.phone import from_e164

from .prompts import SYSTEM_PROMPT

logger = structlog.get_logger()

FUSO = ZoneInfo("America/Sao_Paulo")

# Os quatro placeholders do prompt. `{Nome}` NÃO está aqui de propósito.
CAMPOS = ("nome", "origem", "telefone", "data_hoje")

# `%A` depende do locale do processo — no n8n o `toFormat('EEEE')` saía no
# locale do container. Fixar em português aqui torna a saída determinística e
# coerente com o resto do prompt.
DIAS_DA_SEMANA = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def formatar_data_hoje(agora: datetime | None = None) -> str:
    """`dd/MM/yyyy HH:mm:ss (dia da semana)` no fuso de São Paulo.

    No n8n o campo era a concatenação de duas expressões — `toFormat(
    'dd/MM/yyyy HH:mm:ss')` e `toFormat('EEEE')`, ambas em
    `America/Sao_Paulo`. As duas colapsaram num único `{data_hoje}`, então o
    valor precisa carregar data, hora E dia da semana: a Renata sugere
    horários de agenda a partir daqui.
    """
    momento = agora.astimezone(FUSO) if agora else datetime.now(FUSO)
    return (
        f"{momento.strftime('%d/%m/%Y %H:%M:%S')} ({DIAS_DA_SEMANA[momento.weekday()]})"
    )


def contexto_vazio(telefone: str = "") -> dict[str, str]:
    """Contexto sem lead: nada de placeholder cru sobrando no prompt."""
    return {
        "nome": "",
        "origem": "",
        "telefone": telefone,
        "data_hoje": formatar_data_hoje(),
    }


async def carregar_contexto(
    pool: AsyncConnectionPool, phone_e164: str
) -> dict[str, str]:
    """Lê nome e origem do lead em `leads_crm`. Nunca levanta.

    `phone_e164` é a representação do harness (`+551155554444`, em
    `message_queue.phone_number` e no `thread_id`); `leads_crm.phone` é a
    canônica só com dígitos. `from_e164` faz a conversão — fatiar string aqui
    seria reinventar a regra do 9º dígito pela metade.

    Lead inexistente devolve strings vazias: o turno vale mais que o
    contexto, e o SOP já lida com um lead sem nome (Fase 1 pede o primeiro
    nome do que a pessoa escrever).
    """
    canonico = from_e164(phone_e164)
    contexto = contexto_vazio(canonico)

    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select name, source from leads_crm where phone = %s",
                (canonico,),
            )
            linha = await cur.fetchone()
    except Exception as err:
        logger.warning("contexto_lead_falhou", phone=canonico, error=str(err))
        return contexto

    if linha is None:
        logger.info("contexto_lead_ausente", phone=canonico)
        return contexto

    nome, origem = linha
    contexto["nome"] = nome or ""
    contexto["origem"] = origem or ""
    return contexto


def interpolar(prompt: str, contexto: dict[str, str]) -> str:
    """Troca os quatro placeholders pelos valores do turno.

    `str.replace` token a token, nunca `.format()` — ver o docstring do
    módulo sobre a chave literal `{Nome}`.
    """
    texto = prompt
    for campo in CAMPOS:
        texto = texto.replace("{" + campo + "}", contexto.get(campo, ""))
    return texto


def _configurable() -> dict[str, Any]:
    config = var_child_runnable_config.get(None)
    if isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict):
            return configurable
    return {}


def _telefone_do_turno() -> str | None:
    """Telefone em E.164 a partir do `configurable` do invoke.

    O worker manda `user_id = phone_number`; o `thread_id`
    (`"{phone}:{agent_id}"`) é o fallback para quem invoca sem `user_id`,
    como o LangGraph Studio.
    """
    configurable = _configurable()

    user_id = configurable.get("user_id")
    if user_id:
        return str(user_id)

    thread_id = configurable.get("thread_id")
    if thread_id:
        telefone = str(thread_id).rsplit(":", 1)[0]
        if telefone:
            return telefone

    return None


def criar_middleware_contexto():
    """Middleware que reinterpola o system prompt a cada chamada ao modelo."""

    @dynamic_prompt
    async def contexto_do_lead(request: ModelRequest) -> str:
        base = request.system_prompt or SYSTEM_PROMPT

        telefone = _telefone_do_turno()
        if telefone is None:
            logger.warning("contexto_sem_telefone_no_config")
            return interpolar(base, contexto_vazio())

        pool = await get_pool()
        contexto = await carregar_contexto(pool, telefone)
        return interpolar(base, contexto)

    return contexto_do_lead
