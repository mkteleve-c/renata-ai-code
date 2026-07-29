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


async def test_followup_nao_dispara_para_quem_esta_de_fora(allowlist_ligada):
    """A régua fala sem o lead ter falado — é o disparo mais perigoso numa
    janela de teste, e alcança leads importados que nunca viram o gate."""
    pool = await get_pool()
    enviou = await followup_mod.ainda_vale_enviar(pool, DE_FORA_CANONICO, nivel=1)

    assert enviou is False
