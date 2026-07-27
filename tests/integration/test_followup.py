"""Régua de follow-up: reivindicação atômica, escada ancorada no inbound e janela de
24h.

**Um envio indevido aqui é WhatsApp para gente de verdade** — por isso os
testes de risco mais alto (concorrência real e janela fechada) são
propositalmente difíceis de escrever de forma honesta. Ver os comentários em
cada um.
"""

import asyncio

from whatsapp_langchain.agents.catalog.elevec_sdr.tools.crm import (
    reverter_fase_apos_cancelamento,
)
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.worker.evolution_client import EvolutionSendError
from whatsapp_langchain.worker.followup import (
    LeadReivindicado,
    _enviar_reivindicados,
    _reivindicar_na_conexao,
    ainda_vale_enviar,
    montar_mensagem,
    primeiro_nome,
    reivindicar,
    rodada,
)

# Round-trip por `canonicalizar`: "55" + DDD(11) + 8 dígitos, sem inserir o
# 9º dígito — permanece igual a si mesmo depois de canonicalizado. Usado nos
# testes que chamam uma tool real (`reverter_fase_apos_cancelamento`), que
# resolve o telefone com `canonico_do_lead` em vez de casar a string crua.
TELEFONE_CANONICO_ESTAVEL = "551190000040"


async def _reivindicar(**kwargs) -> list[LeadReivindicado]:
    return await reivindicar(await get_pool(), **kwargs)


async def _followup_count(phone: str) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select followup_count from leads_crm where phone = %s", (phone,)
        )
        linha = await cur.fetchone()
    assert linha is not None
    return linha[0]


class _ClienteMudo:
    async def send_message(self, to, body, **kwargs):
        raise AssertionError("cliente mudo não deveria ser chamado neste teste")


async def test_os_tres_degraus_ancoram_no_inbound(lead_factory):
    await lead_factory("5511900000001", followup_count=0, minutos_desde_inbound=10)
    await lead_factory("5511900000002", followup_count=0, minutos_desde_inbound=20)
    await lead_factory("5511900000003", followup_count=1, minutos_desde_inbound=80)
    await lead_factory(
        "5511900000004", followup_count=2, minutos_desde_inbound=23 * 60 + 5
    )

    por_telefone = {r.phone: r.nivel for r in await _reivindicar()}

    assert "5511900000001" not in por_telefone, "10 min não vence o degrau 1"
    assert por_telefone["5511900000002"] == 1
    assert por_telefone["5511900000003"] == 2
    assert por_telefone["5511900000004"] == 3


async def test_degrau_2_nao_acumula_sobre_o_degrau_1(lead_factory):
    """A âncora é o inbound, não o envio anterior.

    Lead que recebeu o degrau 1 há muito tempo mas falou há 30 min ainda não
    venceu o degrau 2 (75 min desde o inbound).
    """
    await lead_factory(
        "5511900000005",
        followup_count=1,
        minutos_desde_interacao=600,
        minutos_desde_inbound=30,
    )
    assert await _reivindicar() == []


async def test_lead_sem_last_inbound_at_nunca_e_reivindicado(lead_factory):
    """NULL é seguro por construção — leads importados não recebem nada."""
    await lead_factory(
        "5511900000006",
        followup_count=0,
        minutos_desde_interacao=600,
        minutos_desde_inbound=None,
    )
    assert await _reivindicar() == []


async def test_janela_fechada_nao_reivindica_e_nao_queima_nivel(lead_factory):
    await lead_factory(
        "5511900000020", followup_count=2, minutos_desde_inbound=25 * 60
    )  # janela fechou
    irmao = await lead_factory(
        "5511900000021", followup_count=2, minutos_desde_inbound=23 * 60 + 5
    )  # dentro

    reivindicados = {r.phone for r in await _reivindicar()}

    assert reivindicados == {irmao}, (
        "o de fora da janela não pode entrar, e o de dentro TEM que entrar — "
        "sem o irmão, este teste passaria com uma implementação que não "
        "reivindica ninguém"
    )
    assert await _followup_count("5511900000020") == 2


async def test_fase_terminal_nunca_e_perseguida(lead_factory):
    for i, fase in enumerate(
        ["agendou_sessao", "qualificado", "desqualificado", "perdido"]
    ):
        await lead_factory(
            f"551190000003{i}", phase=fase, followup_count=0, minutos_desde_inbound=60
        )
    assert await _reivindicar() == []


async def test_agente_pausado_nao_e_reivindicado(lead_factory):
    """`agent_active=False` (handover humano em curso) não pode ser perseguido,
    mesmo com fase e relógio elegíveis para o degrau."""
    await lead_factory(
        "5511900000035",
        agent_active=False,
        followup_count=0,
        minutos_desde_inbound=60,
    )
    assert await _reivindicar() == []


async def test_lead_que_voltou_de_uma_reuniao_cancelada_nao_e_perseguido(lead_factory):
    """Passa pela função de verdade da Fase 2, não por estado montado à mão."""
    await lead_factory(
        TELEFONE_CANONICO_ESTAVEL,
        phase="agendou_sessao",
        followup_count=2,
        minutos_desde_inbound=23 * 60 + 5,
    )

    await reverter_fase_apos_cancelamento(TELEFONE_CANONICO_ESTAVEL)

    assert await _reivindicar() == [], (
        "reverter_fase_apos_cancelamento religa followup_active e devolve o "
        "lead a 'qualificado', que o filtro exclui — se este teste quebrar, "
        "alguém tirou 'qualificado' do filtro e leads reais vão receber "
        "mensagem indevida"
    )


async def test_duas_transacoes_realmente_sobrepostas_nao_pegam_o_mesmo_lead(
    lead_factory,
):
    """Sem barreira, asyncio.gather não garante sobreposição: a primeira pode
    commitar antes de a segunda abrir, e o teste passaria sem SKIP LOCKED."""
    for i in range(6):
        await lead_factory(
            f"55119000005{i:02d}", followup_count=0, minutos_desde_inbound=30
        )

    segurando = asyncio.Event()
    pode_soltar = asyncio.Event()

    async def primeira():
        pool = await get_pool()
        async with pool.connection() as conn:
            resultado = await _reivindicar_na_conexao(conn, limite=3)
            segurando.set()
            await pode_soltar.wait()  # segura a transação aberta
            await conn.commit()
        return resultado

    async def segunda():
        await segurando.wait()  # só entra com a primeira ainda aberta
        try:
            return await _reivindicar(limite=3)
        finally:
            pode_soltar.set()

    a, b = await asyncio.gather(primeira(), segunda())
    telefones = [r.phone for r in a] + [r.phone for r in b]
    assert len(telefones) == len(set(telefones)), "o mesmo lead saiu duas vezes"
    assert len(telefones) == 6


async def test_lead_que_falou_entre_o_claim_e_o_envio_nao_recebe(lead_factory):
    """O claim commita e só então o HTTP acontece. Nesse intervalo o lead pode
    ter escrito — e mandar "Fulano?" em cima da mensagem dele é o pior caso."""
    await lead_factory("5511900000060", followup_count=0, minutos_desde_inbound=30)

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    reivindicados = await _reivindicar()
    assert len(reivindicados) == 1

    # o lead escreve agora, entre o claim e o envio
    await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511900000060@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )

    await _enviar_reivindicados(reivindicados, ClienteQueRegistra())
    assert enviados == [], "não pode enviar por cima de quem acabou de falar"


async def test_contador_sobe_antes_do_envio(lead_factory):
    """Divergência consciente do n8n: falha de envio pula um nível, e é o certo.

    Mandar a mesma mensagem duas vezes é pior que perder um follow-up.
    """
    await lead_factory("5511900000070", followup_count=0, minutos_desde_inbound=30)

    class ClienteQueFalha:
        async def send_message(self, to, body, **kwargs):
            raise EvolutionSendError(500, "boom")

    resumo = await rodada(await get_pool(), ClienteQueFalha())
    assert resumo["falhas"] == 1
    assert await _followup_count("5511900000070") == 1


async def test_rodada_conta_os_bloqueados_por_janela(lead_factory):
    """Sem esta métrica, 'a régua morreu' e 'não havia ninguém' são idênticos."""
    await lead_factory("5511900000080", followup_count=2, minutos_desde_inbound=25 * 60)

    resumo = await rodada(await get_pool(), _ClienteMudo())
    assert resumo["bloqueados_por_janela"] == 1


async def test_ainda_vale_enviar_falso_para_lead_inexistente():
    assert await ainda_vale_enviar(await get_pool(), "5511900000099", 1) is False


def test_nivel_1_usa_so_o_primeiro_nome():
    assert montar_mensagem(1, "Fulano de Tal") == "Fulano?"


def test_nivel_1_sem_nome_vira_oi():
    assert montar_mensagem(1, None) == "Oi?"


def test_nome_ausente_nunca_vaza_o_marcador_do_contexto():
    """sanitizar_nome devolve a string 'não informado' quando não há nome.

    Reaproveitá-la aqui sem cuidado manda 'não informado?' para uma pessoa.
    """
    for entrada in (None, "", "   ", "não informado"):
        for nivel in (1, 3):
            texto = montar_mensagem(nivel, entrada)
            assert "não informado" not in texto.lower(), (nivel, entrada, texto)
            assert "None" not in texto


def test_nivel_3_sem_nome_nao_deixa_virgula_orfa():
    texto = montar_mensagem(3, None)
    assert not texto.startswith(","), texto
    assert (
        texto == "Tudo bem? Ainda faz sentido falarmos sobre o seu momento de carreira?"
    )


def test_primeiro_nome_de_nome_composto():
    assert primeiro_nome("Fulano de Tal") == "Fulano"


def test_primeiro_nome_de_none_e_none():
    assert primeiro_nome(None) is None
