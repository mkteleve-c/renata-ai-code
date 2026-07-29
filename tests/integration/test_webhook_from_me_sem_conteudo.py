"""Nem todo `fromMe` é o atendente assumindo a conversa.

O handover manual da Evolution é implícito: quando alguém responde pelo
número comercial, o `fromMe` chega e o gate desliga a Renata para aquele
lead (`agent_active=false, followup_active=false, agent_reactivate_at=null`).
É o único handover automático do sistema, e é irreversível por qualquer
caminho automático — `_SQL_REATIVAR` exige `agent_reactivate_at < now()`, e
o gate grava `null`.

Por isso o gatilho precisa ser uma mensagem DE VERDADE. Reagir com 👍 a uma
mensagem do lead, ou apagar a própria mensagem, não é assumir atendimento —
e não pode custar o desligamento permanente do agente para aquela pessoa.
"""

import pytest

from whatsapp_langchain.server.routes.webhook_evolution import _processar_mensagem
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate

TELEFONE = "5511955554444"
CANONICO = "551155554444"


@pytest.fixture
async def lead_ativo():
    pool = await get_pool()

    async def limpa():
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = %s", (CANONICO,))
            await conn.execute(
                "delete from message_queue where phone_number = %s", (f"+{CANONICO}",)
            )

    await limpa()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, name, agent_active, followup_active) "
            "values (%s, 'Lead Ativo', true, true)",
            (CANONICO,),
        )
    yield
    await limpa()


async def _estado():
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active, followup_active from leads_crm where phone = %s",
            (CANONICO,),
        )
        return await cur.fetchone()


def _payload(message_type: str, extra: dict, message_id: str) -> dict:
    return {
        "key": {
            "remoteJid": f"{TELEFONE}@s.whatsapp.net",
            "fromMe": True,
            "id": message_id,
        },
        "messageType": message_type,
        "message": extra,
        "pushName": "Atendente",
    }


@pytest.mark.parametrize(
    ("message_type", "corpo", "descricao"),
    [
        (
            "reactionMessage",
            {"reactionMessage": {"text": "👍", "key": {"id": "X"}}},
            "atendente reage com emoji",
        ),
        (
            "protocolMessage",
            {"protocolMessage": {"type": "REVOKE", "key": {"id": "X"}}},
            "atendente apaga a própria mensagem",
        ),
    ],
)
async def test_from_me_sem_conteudo_nao_desliga_o_agente(
    lead_ativo, message_type, corpo, descricao
):
    """Reação e mensagem apagada não são atendimento humano.

    Antes desta correção, o guard de conteúdo tinha `and not eh_from_me`, o
    que fazia TODO evento `fromMe` escapar do corte e chegar ao gate — que
    executava o handover. Um emoji desligava a Renata para o lead, para
    sempre, com log em INFO e nenhum sinal de erro.
    """
    await _processar_mensagem(
        _payload(message_type, corpo, f"wamid.{message_type}"),
        agent="elevec_sdr",
        instance="instancia-apioficial",
    )

    agent_active, followup_active = await _estado()
    assert agent_active is True, f"{descricao} desligou o agente"
    assert followup_active is True, f"{descricao} desligou a régua"


async def test_from_me_com_texto_continua_desligando_o_agente(lead_ativo):
    """Contraprova, e é o caso que importa preservar: o atendente responder
    de verdade PRECISA pausar a Renata, senão ela fala por cima dele."""
    pool = await get_pool()
    r = await aplicar_gate(
        pool,
        {"remoteJid": f"{TELEFONE}@s.whatsapp.net", "fromMe": True},
        push_name=None,
    )
    assert r.motivo == "from_me"

    agent_active, followup_active = await _estado()
    assert agent_active is False, "resposta manual do atendente não pausou a Renata"
    assert followup_active is False
