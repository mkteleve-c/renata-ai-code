"""`POST /webhook/chatwoot` — a etiqueta `pausar_agente` desliga a Renata.

É o mecanismo pelo qual um humano assume uma conversa: aplica a etiqueta no
ChatWoot e o agente para de responder aquele lead. Sem esta rota, aplicar a
etiqueta **não fazia nada** — a Renata continuava respondendo por cima do
atendimento humano, e o único sinal era ela falando junto.

O contrato veio do workflow `#1.1 Handover to Human via ChatWoot com
Etiqueta` do n8n (nó `Code in JavaScript`), não de suposição:

    body.meta.sender.identifier  -> "5551999999999@s.whatsapp.net"
    body.labels                  -> ["pausar_agente", ...] ou []

Não são três eventos, é um só: o ChatWoot manda a lista COMPLETA de
etiquetas a cada mudança, então adicionar e remover são presença e ausência
no mesmo array.

Uma correção deliberada sobre o n8n: lá o telefone entrava no `WHERE` com os
dígitos crus (`5551999999999`, com o 9º dígito). No harness `leads_crm.phone`
é canônico, garantido por CHECK — o `UPDATE` do n8n nunca casaria aqui.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from whatsapp_langchain.server.main import app
from whatsapp_langchain.shared.db import get_pool

TELEFONE_JID = "5511977776666@s.whatsapp.net"
CANONICO = "551177776666"


@pytest.fixture
async def lead_ativo():
    pool = await get_pool()

    async def limpa():
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = %s", (CANONICO,))

    await limpa()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, name, agent_active, followup_active) "
            "values (%s, 'Lead Real', true, true)",
            (CANONICO,),
        )
    yield
    await limpa()


async def _estado():
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active, followup_active, agent_reactivate_at "
            "from leads_crm where phone = %s",
            (CANONICO,),
        )
        return await cur.fetchone()


def _payload(labels: list[str], identifier: str = TELEFONE_JID) -> dict:
    return {
        "event": "conversation_updated",
        "labels": labels,
        "meta": {"sender": {"identifier": identifier}},
    }


async def _post(payload: dict) -> int:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as cliente:
        r = await cliente.post("/webhook/chatwoot", json=payload)
        return r.status_code


async def test_etiqueta_pausar_agente_desliga_a_renata(lead_ativo):
    assert await _post(_payload(["pausar_agente"])) == 200

    agent_active, followup_active, reactivate_at = await _estado()
    assert agent_active is False
    assert followup_active is False
    assert reactivate_at is None


async def test_tirar_a_etiqueta_religa_o_agente_mas_nao_a_regua(lead_ativo):
    """Assimetria deliberada, copiada do n8n e coerente com a doutrina do
    harness: desligar mexe em três colunas, religar mexe em uma.

    Religar `followup_active` junto faria a régua voltar a perseguir alguém
    que uma pessoa pausou — o mesmo motivo pelo qual `update_crm` usa
    `followup_active and not %s` (`tools/crm.py`) e `_vencedor_pausa` existe
    (`shared/leads.py`). Errar para o lado de não mandar mensagem é
    recuperável.
    """
    await _post(_payload(["pausar_agente"]))

    assert await _post(_payload([])) == 200

    agent_active, followup_active, _ = await _estado()
    assert agent_active is True, "o agente não voltou"
    assert followup_active is False, "a régua não pode religar sozinha"


async def test_outras_etiquetas_nao_pausam(lead_ativo):
    """Só `pausar_agente` pausa. Qualquer outra etiqueta do time — `vip`,
    `urgente` — não pode desligar o agente por acidente."""
    assert await _post(_payload(["vip", "urgente"])) == 200

    agent_active, followup_active, _ = await _estado()
    assert agent_active is True
    assert followup_active is True


async def test_telefone_e_canonicalizado(lead_ativo):
    """O ChatWoot manda o JID com o 9º dígito; `leads_crm.phone` é canônico.

    O n8n fazia `WHERE phone = '<dígitos crus>'` e no harness isso não
    casaria nunca — a etiqueta pareceria funcionar (200, sem erro) e não
    pausaria ninguém.
    """
    assert (
        await _post(_payload(["pausar_agente"], "5511977776666@s.whatsapp.net")) == 200
    )

    agent_active, _, _ = await _estado()
    assert agent_active is False, (
        "o telefone com 9º dígito não alcançou o lead canônico"
    )


async def test_lead_inexistente_responde_200_sem_criar_nada():
    """Reentrega não pode virar loop: erro de configuração responde 200,
    mesma doutrina de `webhook_evolution`. E a etiqueta nunca cria lead."""
    pool = await get_pool()
    assert (
        await _post(_payload(["pausar_agente"], "5511900001111@s.whatsapp.net")) == 200
    )

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", ("551190001111",)
        )
        assert (await cur.fetchone())[0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"labels": ["pausar_agente"]},
        {"meta": {}},
        {"meta": {"sender": {}}},
        {"meta": {"sender": {"identifier": ""}}},
        {"meta": {"sender": {"identifier": "@g.us"}}},
    ],
)
async def test_payload_incompleto_responde_200_e_nao_estoura(payload):
    """4xx faria o ChatWoot reentregar em loop por um payload que nenhuma
    reentrega conserta."""
    assert await _post(payload) == 200
