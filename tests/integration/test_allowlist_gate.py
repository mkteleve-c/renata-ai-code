"""A allowlist nos dois caminhos que falam com o lead.

`test_allowlist.py` (unit) prova a normalização. Aqui prova-se o que
importa em produção: com a allowlist ligada, o gate **não escreve nada**
para quem está de fora, e a régua de follow-up **não dispara**.

Os dois caminhos precisam de teste separado porque só um deles passa pelo
gate: a régua fala com leads importados do Supabase que nunca passaram por
`aplicar_gate` na vida.
"""

import pytest

from whatsapp_langchain.shared import config as config_mod
from whatsapp_langchain.shared import leads as leads_mod
from whatsapp_langchain.shared.config import Settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.worker import followup as followup_mod

PERMITIDO = "5581991013614"
PERMITIDO_CANONICO = "558191013614"
DE_FORA = "5511977776666"
DE_FORA_CANONICO = "551177776666"


@pytest.fixture
def allowlist_ligada(monkeypatch):
    """Liga a allowlist nos três módulos que leem `settings`.

    `settings` é um singleton de módulo importado por nome (`from ...
    import settings`), então trocar só o objeto em `config` não alcança
    quem já importou — daí o patch nos três pontos de uso.
    """
    s = Settings(_env_file=None, allowlist_phones=PERMITIDO)
    for mod in (config_mod, leads_mod, followup_mod):
        monkeypatch.setattr(mod, "settings", s, raising=True)
    return s


@pytest.fixture
async def limpar():
    pool = await get_pool()
    alvos = [PERMITIDO_CANONICO, DE_FORA_CANONICO]

    async def limpa():
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = any(%s)", (alvos,))
            await conn.execute(
                "delete from leads_descartados where phone_original = any(%s)", (alvos,)
            )

    await limpa()
    yield
    await limpa()


def _jid(phone: str) -> dict:
    return {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False}


async def test_gate_aceita_quem_esta_na_allowlist(allowlist_ligada, limpar):
    pool = await get_pool()
    r = await aplicar_gate(pool, _jid(PERMITIDO), push_name="Teste")

    assert r.aceito is True
    assert r.canonico == PERMITIDO_CANONICO


async def test_gate_descarta_quem_esta_de_fora(allowlist_ligada, limpar):
    pool = await get_pool()
    r = await aplicar_gate(pool, _jid(DE_FORA), push_name="Estranho")

    assert r.aceito is False
    assert r.motivo == "fora_da_allowlist"


async def test_descarte_nao_deixa_rastro_nenhum(allowlist_ligada, limpar):
    """Nem lead criado, nem linha em `leads_descartados`.

    O rastro importa: `monitorar_cutover.py` conta leads novos e descartes
    na primeira hora, e tráfego de terceiros barrado pela janela de teste
    não é sinal nenhum — é ruído que dispara limiar de atenção à toa.
    """
    pool = await get_pool()
    await aplicar_gate(pool, _jid(DE_FORA), push_name="Estranho")

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (DE_FORA_CANONICO,)
        )
        assert (await cur.fetchone())[0] == 0

        cur = await conn.execute(
            "select count(*) from leads_descartados where phone_original = %s",
            (DE_FORA_CANONICO,),
        )
        assert (await cur.fetchone())[0] == 0


async def test_desligada_deixa_todo_mundo_passar(limpar):
    """Sem `allowlist_ligada`: o default do repo não pode barrar ninguém."""
    pool = await get_pool()
    r = await aplicar_gate(pool, _jid(DE_FORA), push_name="Qualquer Um")

    assert r.aceito is True


async def test_handover_manual_pausa_o_agente_mesmo_fora_da_allowlist(
    allowlist_ligada, limpar
):
    """O `fromMe` é o único handover humano automático do sistema.

    Durante a janela de teste, a equipe responde na mão TODO lead fora da
    lista — é o comportamento padrão, não caso de borda. Se a allowlist
    barrar o `fromMe`, `agent_active` fica `true` e, no dia em que a lista
    for esvaziada, a Renata entra por cima de conversas que humanos
    assumiram. Falha silenciosa e irreversível: ninguém percebe até a
    Renata falar por cima.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, name, agent_active, followup_active) "
            "values (%s, 'Lead Real', true, true)",
            (DE_FORA_CANONICO,),
        )

    r = await aplicar_gate(pool, {**_jid(DE_FORA), "fromMe": True}, push_name=None)
    assert r.motivo == "from_me", "o fromMe tem que vencer a allowlist"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active, followup_active from leads_crm where phone = %s",
            (DE_FORA_CANONICO,),
        )
        agent_active, followup_active = await cur.fetchone()

    assert agent_active is False, "handover humano não pausou o agente"
    assert followup_active is False, "handover humano não desligou a régua"


async def test_inbound_de_fora_da_allowlist_ainda_move_last_inbound_at(
    allowlist_ligada, limpar
):
    """`last_inbound_at` é fato sobre o WhatsApp, não sobre o nosso funil.

    Mesmo precedente do ramo `agente_desligado` (`leads.py:371`): o lead
    falou, e o relógio da janela de 24h da Cloud API precisa andar. Congelar
    esse instante durante a janela de teste faz o lead voltar, quando a
    allowlist for esvaziada, com um `last_inbound_at` mentindo sobre quando
    ele falou pela última vez.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, name, last_inbound_at) "
            "values (%s, 'Lead Real', now() - interval '3 hours')",
            (DE_FORA_CANONICO,),
        )

    r = await aplicar_gate(pool, _jid(DE_FORA), push_name=None)
    assert r.aceito is False
    assert r.motivo == "fora_da_allowlist"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select now() - last_inbound_at < interval '1 minute', followup_count "
            "from leads_crm where phone = %s",
            (DE_FORA_CANONICO,),
        )
        recente, followup_count = await cur.fetchone()

    assert recente is True, "last_inbound_at não andou"
    assert followup_count == 0, "o lead falou — o contador de follow-up zera"


async def test_regua_nao_reivindica_quem_esta_fora_da_allowlist(
    allowlist_ligada, limpar
):
    """O defeito que este teste protege é corrupção de contador, não envio.

    A allowlist checada só no último portão (`ainda_vale_enviar`) não impede
    o claim: `_SQL_AVANCAR` já incrementou `followup_count` e commitou antes
    do abort. Em ~15 min de régua ligada isso queima os três degraus de todo
    lead ativo, e como o predicado exige `followup_count <= 2`, eles nunca
    mais recebem follow-up. Irreversível sem UPDATE manual.

    O teste anterior aqui chamava `ainda_vale_enviar` para um telefone SEM
    linha em `leads_crm` — abortava por `lead_sumiu` e passava mesmo com a
    allowlist deletada. Este insere um lead genuinamente elegível.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm "
            "  (phone, name, phase, followup_active, agent_active, "
            "   followup_count, last_inbound_at) "
            "values (%s, 'Elegivel', 'iniciou_conversa', true, true, 0, "
            "        now() - interval '30 minutes')",
            (DE_FORA_CANONICO,),
        )

    reivindicados = await followup_mod.reivindicar(pool)

    assert all(r.phone != DE_FORA_CANONICO for r in reivindicados), (
        "lead fora da allowlist foi reivindicado"
    )

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select followup_count from leads_crm where phone = %s",
            (DE_FORA_CANONICO,),
        )
        assert (await cur.fetchone())[0] == 0, (
            "followup_count foi queimado sem nenhuma mensagem ter saído"
        )


async def test_regua_reivindica_normalmente_quem_esta_na_allowlist(
    allowlist_ligada, limpar
):
    """Contraprova do teste acima: a allowlist no predicado não pode
    esvaziar a régua para quem ESTÁ na lista."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm "
            "  (phone, name, phase, followup_active, agent_active, "
            "   followup_count, last_inbound_at) "
            "values (%s, 'Permitido', 'iniciou_conversa', true, true, 0, "
            "        now() - interval '30 minutes')",
            (PERMITIDO_CANONICO,),
        )

    reivindicados = await followup_mod.reivindicar(pool)

    assert any(r.phone == PERMITIDO_CANONICO for r in reivindicados), (
        "quem está na allowlist parou de ser reivindicado"
    )
