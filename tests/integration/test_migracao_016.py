"""A blocklist importada do n8n (`016_blocklist_opt_out_n8n.sql`).

O que estes testes protegem não é a contagem de linhas — é a propriedade
que fez a migração existir: **quem pediu para parar de receber não recebe,
não importa em qual das duas formas o telefone chegue**. O n8n bloqueava
por sufixo, então as duas formas casavam com a mesma entrada de graça.
Aqui o casamento é exato, e é a migração que precisa gravar as duas.

Por isso os testes vão até `aplicar_gate` em vez de conferir a tabela: o
que importa é o bloqueio acontecer no caminho real, não a linha existir.
"""

import re

import pytest

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.shared.phone import canonicalizar, variacoes

MOTIVO = "opt-out importado do n8n (Filtro e Permissao v002)"

# Três dos 26, escolhidos por FORMA de origem e não por acaso:
#   - 4598179085  veio com 10 dígitos (DDD + 8), sem o 9º
#   - 21996182653 veio com 11 dígitos (DDD + 9)
#   - 1188671528  é o último da lista — pega truncamento no fim do INSERT
ORIGENS = ["4598179085", "21996182653", "1188671528"]


@pytest.fixture
async def pool():
    return await get_pool()


@pytest.fixture
async def sem_lead_residual():
    """Se o bloqueio falhar, `aplicar_gate` CRIA o lead que não deveria
    existir — e ele sobrevive para a próxima rodada, mascarando o mesmo
    erro (o segundo run acha o lead pronto e segue por outro ramo). Limpa
    dos dois lados."""
    canonicos = [canonicalizar("55" + o) for o in ORIGENS]
    todas = [f for c in canonicos for f in variacoes(c)]
    pool = await get_pool()

    async def limpar():
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = any(%s)", (todas,))

    await limpar()
    yield
    await limpar()


async def test_as_52_linhas_existem_e_respeitam_o_check(pool):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select phone from blocklist where motivo = %s", (MOTIVO,)
        )
        telefones = [linha[0] for linha in await cur.fetchall()]

    assert len(telefones) == 52, "26 números únicos × 2 variações"
    assert len(set(telefones)) == 52, "sem duplicata"
    fora = [t for t in telefones if not re.match(r"^[0-9]{8,15}$", t)]
    assert fora == [], f"violam o CHECK da tabela: {fora}"


async def test_toda_entrada_tem_as_duas_variacoes(pool):
    """A propriedade central: nenhuma pessoa gravada pela metade.

    Uma única forma gravada bloquearia por um caminho e deixaria passar
    pelo outro — exatamente o modo de falha silenciosa que a migração
    existe para fechar.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select phone from blocklist where motivo = %s", (MOTIVO,)
        )
        gravados = {linha[0] for linha in await cur.fetchall()}

    for phone in gravados:
        com_9, sem_9 = variacoes(canonicalizar(phone))
        assert com_9 in gravados, f"{phone}: falta a forma com o 9º dígito"
        assert sem_9 in gravados, f"{phone}: falta a forma sem o 9º dígito"


@pytest.mark.parametrize("origem", ORIGENS)
async def test_gate_descarta_nas_duas_formas(pool, origem, sem_lead_residual):
    """Chega pelo webhook nas duas formas — as duas são descartadas."""
    canonico = canonicalizar("55" + origem)
    for forma in variacoes(canonico):
        jid = {"remoteJid": f"{forma}@s.whatsapp.net", "fromMe": False}
        r = await aplicar_gate(pool, jid, push_name="Não Deveria Passar")

        assert r.aceito is False, f"{forma} passou pelo gate"
        assert r.motivo == "blocklist"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (canonico,)
        )
        assert (await cur.fetchone())[0] == 0, "blocklist descarta antes de criar lead"


async def test_reaplicar_a_migracao_nao_falha_nem_duplica(pool):
    """A migração roda no startup de toda API — reencontrar as linhas que
    ela mesma gravou (ou que a importação do Supabase gravou antes) não
    pode levantar erro nem multiplicar entrada."""
    sql = open("db/migrations/016_blocklist_opt_out_n8n.sql").read()
    async with pool.connection() as conn:
        await conn.execute(sql)
        cur = await conn.execute(
            "select count(*) from blocklist where motivo = %s", (MOTIVO,)
        )
        assert (await cur.fetchone())[0] == 52
