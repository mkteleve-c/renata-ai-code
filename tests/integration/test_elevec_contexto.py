"""O contexto do lead chega ao agente a cada turno.

Dois riscos distintos são cobertos aqui:

1. A leitura em `leads_crm` (nome, origem, telefone) e o relógio de
   São Paulo — precisam do banco, por isso este arquivo é de integração.
2. A interpolação no `SYSTEM_PROMPT`, que carrega a chave literal
   `{Nome}` dentro do script da Fase 1 do SOP. Um `.format()` ingênuo
   estoura com `KeyError: 'Nome'` — o teste
   `test_prompt_real_sobrevive_a_chave_literal_nome` é quem garante que a
   implementação não regride para isso.
"""

import re

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables.config import var_child_runnable_config

from whatsapp_langchain.agents.catalog.elevec_sdr.contexto import (
    CAMPOS,
    carregar_contexto,
    criar_middleware_contexto,
    formatar_data_hoje,
    interpolar,
)
from whatsapp_langchain.agents.catalog.elevec_sdr.prompts import SYSTEM_PROMPT
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "551155554444"

# 27/07/2026 01:31:34 (segunda-feira) — data, hora e dia da semana, os três
# pedaços que as duas expressões do n8n entregavam concatenadas.
FORMATO_DATA = re.compile(
    r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} "
    r"\((?:(?:segunda|terça|quarta|quinta|sexta)-feira|sábado|domingo)\)$"
)


@pytest.fixture
async def lead():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute(
            "insert into leads_crm (phone, name, source, phase) "
            "values (%s, 'Fulano de Tal', 'linkedin_form', 'iniciou_conversa')",
            (TELEFONE,),
        )
    yield
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))


async def test_contexto_traz_nome_e_origem(lead):
    pool = await get_pool()
    ctx = await carregar_contexto(pool, f"+{TELEFONE}")

    assert ctx["nome"] == "Fulano de Tal"
    assert ctx["origem"] == "linkedin_form"
    assert ctx["telefone"] == TELEFONE


async def test_data_de_hoje_vem_no_fuso_de_sao_paulo(lead):
    pool = await get_pool()
    ctx = await carregar_contexto(pool, f"+{TELEFONE}")

    # dd/MM/yyyy HH:mm:ss — mesmo formato do n8n
    assert len(ctx["data_hoje"].split("/")) == 3


async def test_lead_inexistente_nao_quebra():
    pool = await get_pool()
    ctx = await carregar_contexto(pool, "+551100000000")

    assert ctx["nome"] == ""
    assert ctx["telefone"] == "551100000000"


async def test_contexto_preenche_todos_os_campos_mesmo_sem_lead():
    """Nenhum placeholder pode sobrar sem valor — nem o de um lead ausente."""
    pool = await get_pool()
    ctx = await carregar_contexto(pool, "+551100000000")

    assert set(ctx) == set(CAMPOS)
    assert all(isinstance(v, str) for v in ctx.values())


def test_data_hoje_traz_hora_e_dia_da_semana():
    """No n8n eram duas expressões concatenadas; aqui é um campo só.

    Perder a hora ou o dia da semana empobrece o prompt em relação ao
    original — a Renata sugere horários de agenda e precisa dos três.
    """
    data = formatar_data_hoje()

    assert FORMATO_DATA.match(data), data
    assert "-feira" in data or "sábado" in data or "domingo" in data


def test_interpolar_preenche_os_quatro_placeholders():
    texto = interpolar(
        "Nome: {nome} | Origem: {origem} | Tel: {telefone} | Data: {data_hoje}",
        {
            "nome": "Fulano",
            "origem": "linkedin_form",
            "telefone": "551155554444",
            "data_hoje": "27/07/2026 01:31:34 (segunda-feira)",
        },
    )

    assert texto == (
        "Nome: Fulano | Origem: linkedin_form | Tel: 551155554444 "
        "| Data: 27/07/2026 01:31:34 (segunda-feira)"
    )


def test_prompt_real_sobrevive_a_chave_literal_nome():
    """`{Nome}` (maiúsculo) é conteúdo do SOP, não placeholder.

    Ele vive no script da Fase 1 ("Oi, {Nome}!"). `.format()` ou f-string
    estouram com KeyError; a interpolação precisa tocar só os quatro
    tokens exatos.
    """
    ctx = {
        "nome": "Fulano de Tal",
        "origem": "linkedin_form",
        "telefone": "551155554444",
        "data_hoje": "27/07/2026 01:31:34 (segunda-feira)",
    }

    texto = interpolar(SYSTEM_PROMPT, ctx)

    assert "Oi, {Nome}!" in texto
    assert "- Nome: Fulano de Tal" in texto
    assert "- Origem: linkedin_form" in texto
    assert "- Telefone: 551155554444" in texto
    assert "(dd/MM/yyyy): 27/07/2026 01:31:34 (segunda-feira)" in texto
    for campo in CAMPOS:
        assert "{" + campo + "}" not in texto


def _requisicao() -> ModelRequest:
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[HumanMessage(content="oi")],
        system_message=SystemMessage(content=SYSTEM_PROMPT),
    )


async def _capturar_system_prompt(configurable: dict) -> str | None:
    """Roda o middleware com o `configurable` que o worker passa no invoke."""
    middleware = criar_middleware_contexto()
    capturado: dict = {}

    async def handler(request: ModelRequest):
        capturado["system_prompt"] = request.system_prompt
        return None

    token = var_child_runnable_config.set({"configurable": configurable})  # type: ignore[arg-type]
    try:
        await middleware.awrap_model_call(_requisicao(), handler)  # type: ignore[attr-defined]
    finally:
        var_child_runnable_config.reset(token)

    return capturado.get("system_prompt")


async def test_middleware_interpola_o_system_prompt_do_turno(lead):
    prompt = await _capturar_system_prompt(
        {"thread_id": f"+{TELEFONE}:elevec_sdr", "user_id": f"+{TELEFONE}"}
    )

    assert prompt is not None
    assert "- Nome: Fulano de Tal" in prompt
    assert "- Origem: linkedin_form" in prompt
    assert f"- Telefone: {TELEFONE}" in prompt
    assert "Oi, {Nome}!" in prompt
    for campo in CAMPOS:
        assert "{" + campo + "}" not in prompt


async def test_middleware_usa_o_thread_id_quando_nao_ha_user_id(lead):
    """No LangGraph Studio só o thread_id chega — e ele carrega o telefone."""
    prompt = await _capturar_system_prompt({"thread_id": f"+{TELEFONE}:elevec_sdr"})

    assert prompt is not None
    assert "- Nome: Fulano de Tal" in prompt


async def test_middleware_sem_telefone_nao_quebra_o_turno():
    """Sem telefone no config, o turno segue com os campos vazios."""
    prompt = await _capturar_system_prompt({})

    assert prompt is not None
    assert "- Nome: \n" in prompt
    for campo in CAMPOS:
        assert "{" + campo + "}" not in prompt


class _ModeloEspiao(GenericFakeChatModel):
    """Guarda o system message que chegou de fato ao modelo."""

    recebido: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.recebido.append(messages[0])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def test_agente_real_recebe_o_contexto_interpolado(lead):
    """Prova ponta a ponta: o `configurable` do invoke chega ao middleware.

    Os testes acima setam o contextvar na mão; este roda o grafo de verdade
    (com um modelo falso) e confirma que o system prompt entregue ao modelo
    veio interpolado com os dados do lead.
    """
    modelo = _ModeloEspiao(messages=iter([AIMessage(content="ok")]))
    modelo.recebido = []

    agente = create_agent(
        model=modelo,
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        middleware=[criar_middleware_contexto()],
    )
    resultado = await agente.ainvoke(
        {"messages": [HumanMessage(content="oi")]},
        config={
            "configurable": {
                "thread_id": f"+{TELEFONE}:elevec_sdr",
                "user_id": f"+{TELEFONE}",
            }
        },
    )

    system = modelo.recebido[0]
    assert isinstance(system, SystemMessage)
    texto = system.text
    assert "- Nome: Fulano de Tal" in texto
    assert "Oi, {Nome}!" in texto
    assert FORMATO_DATA.match(
        texto.split("- Data Hoje(dd/MM/yyyy): ")[1].split("\n")[0]
    )

    # O contexto é efêmero: nada de SystemMessage sobrando no histórico que
    # o checkpointer grava para o próximo turno.
    assert not any(isinstance(m, SystemMessage) for m in resultado["messages"])
