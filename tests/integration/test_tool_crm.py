"""`update_crm` e `human_handover` contra o Postgres de verdade.

Estas duas tools **só existem pelo efeito no banco**: mover o lead de fase e
desligar o agente. Testá-las com a camada de persistência monkeypatchada
seria testar as strings que elas devolvem — a Task 5 já pagou esse preço uma
vez, com 42 testes verdes enquanto o `UPDATE` era um no-op.

Por isso aqui o banco é o real (porta do `DATABASE_URL`, `leads_crm`), cada
teste confere o **estado da linha depois** e a camada de gravação trata
`rowcount == 0` como falha.

Dublados: o Pipedrive (é o CRM de produção da empresa — **nenhum card real é
movido em teste**), a agenda do Google e o cliente de saída do WhatsApp.
"""

import asyncio
from datetime import datetime
from typing import Any

import pytest
from langchain_core.runnables.config import var_child_runnable_config

from whatsapp_langchain.agents.catalog.elevec_sdr.tools import agenda, crm, handover
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.crm import update_crm
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.handover import human_handover
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.interno import PREFIXO_INTERNO
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.google_calendar import FUSO

CANONICO = "551155552222"
E164 = "+5511955552222"

DEAL = "7788"

# Como o operador digita e como o envio sai: `canonicalizar` tira o 9º
# dígito, a mesma normalização que o outbound do harness aplica ao lead.
NOTIFY_CONFIGURADO = "+5511977776666"
NOTIFY_ENVIADO = "+551177776666"

TERCA = datetime(2026, 2, 10, 10, 0, tzinfo=FUSO)


# --- Banco ------------------------------------------------------------------


@pytest.fixture
async def limpar():
    async def apagar():
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = %s", (CANONICO,))

    await apagar()
    yield
    await apagar()


async def criar_lead(**campos: Any) -> None:
    base: dict[str, Any] = {
        "pipedriveid": DEAL,
        "phase": "iniciou_conversa",
        "followup_active": True,
        "agent_active": True,
        "email": None,
        "faturamento_mensal": None,
        "google_event_id": None,
    }
    base.update(campos)
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm"
            " (phone, name, pipedriveid, email, faturamento_mensal,"
            "  source, phase, followup_active, agent_active, google_event_id)"
            " values (%s, 'Ana', %s, %s, %s, 'whatsapp_direct',"
            "         %s::lead_phase, %s, %s, %s)",
            (
                CANONICO,
                base["pipedriveid"],
                base["email"],
                base["faturamento_mensal"],
                base["phase"],
                base["followup_active"],
                base["agent_active"],
                base["google_event_id"],
            ),
        )


async def ler_lead() -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select phase, followup_active, agent_active, email,"
            "       faturamento_mensal, name, google_event_id"
            " from leads_crm where phone = %s",
            (CANONICO,),
        )
        linha = await cur.fetchone()
    if linha is None:
        return None
    return {
        "phase": linha[0],
        "followup_active": linha[1],
        "agent_active": linha[2],
        "email": linha[3],
        "faturamento_mensal": linha[4],
        "name": linha[5],
        "google_event_id": linha[6],
    }


# --- Dublês -----------------------------------------------------------------


class PipedriveFalso:
    """Registra as chamadas. Nenhum card real é tocado."""

    def __init__(self, erro: Exception | None = None):
        self.movidos: list[tuple[str, int]] = []
        self.erro = erro

    async def mover_card(self, deal_id: str, stage_id: int) -> None:
        self.movidos.append((deal_id, stage_id))
        if self.erro is not None:
            raise self.erro


class EvolutionFalso:
    def __init__(self, erro: Exception | None = None):
        self.enviados: list[dict[str, Any]] = []
        self.erro = erro

    async def send_message(self, to, body, token=None, delay_ms=0):
        if self.erro is not None:
            raise self.erro
        self.enviados.append({"to": to, "body": body})
        return "id-1"


@pytest.fixture
def turno():
    token = var_child_runnable_config.set(  # type: ignore[arg-type]
        {"configurable": {"user_id": E164}}
    )
    yield
    var_child_runnable_config.reset(token)


@pytest.fixture
def pipedrive(monkeypatch):
    falso = PipedriveFalso()
    monkeypatch.setattr(crm, "obter_cliente", lambda: falso)
    monkeypatch.setattr(crm.settings, "pipedrive_api_token", "tok-de-teste")
    monkeypatch.setattr(crm.settings, "pipedrive_stage_qualificado", 12)
    monkeypatch.setattr(crm.settings, "pipedrive_stage_agendado", 13)
    return falso


@pytest.fixture
def evolution(monkeypatch):
    falso = EvolutionFalso()
    monkeypatch.setattr(handover, "obter_cliente", lambda: falso)
    monkeypatch.setattr(handover.settings, "handover_notify_phone", NOTIFY_CONFIGURADO)
    return falso


# --- update_crm: a fase muda de verdade -------------------------------------


async def test_qualificado_grava_a_fase_e_move_o_card(limpar, turno, pipedrive):
    await criar_lead()

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    lido = await ler_lead()
    assert lido["phase"] == "qualificado"
    assert pipedrive.movidos == [(DEAL, 12)]
    assert "qualificado" in saida


async def test_agendou_sessao_move_para_o_estagio_13(limpar, turno, pipedrive):
    await criar_lead(phase="qualificado")

    await update_crm.ainvoke({"phase": "agendou_sessao"})

    assert (await ler_lead())["phase"] == "agendou_sessao"
    assert pipedrive.movidos == [(DEAL, 13)]


# --- Fase igual: nem banco nem CRM ------------------------------------------


async def test_fase_igual_nao_reescreve_nada(limpar, turno, pipedrive):
    # `followup_active=True` num lead já em `agendou_sessao` é o estado que
    # sobra quando um humano reativa a cobrança à mão. Reescrever a mesma
    # fase o desligaria de novo, por cima da decisão da pessoa.
    await criar_lead(phase="agendou_sessao", followup_active=True)

    saida = await update_crm.ainvoke({"phase": "agendou_sessao"})

    lido = await ler_lead()
    assert lido["phase"] == "agendou_sessao"
    assert lido["followup_active"] is True, "a mesma fase não pode reescrever a linha"
    assert pipedrive.movidos == [], "a mesma fase não pode mover o card de novo"
    assert "já está" in saida


async def test_fase_igual_nao_persiste_nem_email(limpar, turno, pipedrive):
    await criar_lead(phase="qualificado", email="antigo@exemplo.com")

    await update_crm.ainvoke({"phase": "qualificado", "email": "novo@exemplo.com"})

    assert (await ler_lead())["email"] == "antigo@exemplo.com"


# --- Follow-up --------------------------------------------------------------


async def test_agendou_sessao_desliga_o_followup(limpar, turno, pipedrive):
    await criar_lead(phase="qualificado", followup_active=True)

    await update_crm.ainvoke({"phase": "agendou_sessao"})

    assert (await ler_lead())["followup_active"] is False


async def test_desqualificado_desliga_o_followup(limpar, turno, pipedrive):
    await criar_lead(phase="qualificado", followup_active=True)

    await update_crm.ainvoke({"phase": "desqualificado"})

    assert (await ler_lead())["followup_active"] is False


async def test_qualificado_mantem_o_followup_ligado(limpar, turno, pipedrive):
    await criar_lead(followup_active=True)

    await update_crm.ainvoke({"phase": "qualificado"})

    assert (await ler_lead())["followup_active"] is True


async def test_qualificado_nao_religa_followup_desligado(limpar, turno, pipedrive):
    # O lead pode estar com a cobrança desligada por um `human_handover`
    # anterior. Um `followup_active = true` cru religaria a régua num lead
    # que uma pessoa pausou.
    await criar_lead(followup_active=False)

    await update_crm.ainvoke({"phase": "qualificado"})

    assert (await ler_lead())["followup_active"] is False


# --- Pipedrive: quando NÃO chamar -------------------------------------------


async def test_desqualificado_nao_move_card(limpar, turno, pipedrive):
    # O funil comercial não tem estágio de descarte: o card fica onde está.
    await criar_lead(phase="qualificado", pipedriveid=DEAL)

    await update_crm.ainvoke({"phase": "desqualificado"})

    assert (await ler_lead())["phase"] == "desqualificado"
    assert pipedrive.movidos == []


@pytest.mark.parametrize("sem_deal", [None, "", "   "])
async def test_sem_pipedriveid_nao_chama_o_pipedrive(
    limpar, turno, pipedrive, sem_deal
):
    await criar_lead(pipedriveid=sem_deal)

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    assert pipedrive.movidos == []
    assert (await ler_lead())["phase"] == "qualificado"
    # Lead sem card é rotina (importação legada), não incidente.
    assert "ATENÇÃO" not in saida


async def test_sem_token_do_pipedrive_nao_e_incidente(
    limpar, turno, pipedrive, monkeypatch
):
    # Deploy sem Pipedrive é escolha válida. Sem a checagem do token, o
    # ValueError do construtor cairia no except genérico e TODA transição
    # devolveria "o card não foi movido — avise o time comercial",
    # indistinguível de CRM fora do ar.
    monkeypatch.setattr(crm.settings, "pipedrive_api_token", "")
    await criar_lead()

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    assert (await ler_lead())["phase"] == "qualificado"
    # Sem token não se tenta a chamada — nem para colher o erro.
    assert pipedrive.movidos == []
    assert "ATENÇÃO" not in saida
    assert "Card movido" not in saida


# --- Concorrência: a guarda é o UPDATE, não uma comparação em Python --------


async def test_duas_chamadas_para_a_mesma_fase_movem_o_card_uma_vez_so(
    limpar, turno, pipedrive
):
    """O caso que a guarda existe para resolver.

    É também o mais provável: o modelo emite a mesma `tool_call` duas vezes
    na mesma mensagem (o ToolNode do LangGraph as executa em paralelo), ou
    duas réplicas do worker pegam turnos do mesmo lead — o `claim_next` usa
    `FOR UPDATE SKIP LOCKED` sem serializar por telefone.

    Com a comparação de fase feita em Python (ler → comparar → gravar), as
    duas chamadas passavam pelo curto-circuito e **as duas** moviam o card.
    Com o predicado no `where`, o Postgres serializa: a segunda transação
    reavalia depois do commit da primeira, casa zero linhas — e quem casa
    zero linhas não move card.
    """
    await criar_lead(phase="iniciou_conversa")

    resultados = await asyncio.gather(
        update_crm.ainvoke({"phase": "qualificado"}),
        update_crm.ainvoke({"phase": "qualificado"}),
    )

    assert (await ler_lead())["phase"] == "qualificado"
    assert pipedrive.movidos == [(DEAL, 12)]
    # A outra sai por "nada a atualizar" em vez de fingir que gravou.
    assert len([s for s in resultados if "Fase atualizada" in s]) == 1


async def test_alvos_concorrentes_nunca_terminam_com_o_funil_para_tras(
    limpar, turno, pipedrive
):
    """Alvos diferentes concorrentes nunca desfazem `agendou_sessao`.

    As duas chamadas são transições legítimas e distintas, então as duas
    gravam e as duas movem card — isso está certo, o lead transicionou duas
    vezes. O que **não** pode acontecer é o funil andar para trás: sem a
    recusa de retrocesso, em 1 de 5 rodadas medidas o lead terminava em
    `qualificado` com a reunião marcada na agenda do Silvio.

    Vale a mesma doutrina de `shared/leads.py`: reunião marcada é fato
    verificável, `qualificado` é julgamento — o fato vence.

    O lead entra com `google_event_id` preenchido porque é assim que ele
    chega em `agendou_sessao` na vida real: `calendar_agendar` cria o evento
    e grava o vínculo antes de qualquer `update_crm`. É esse vínculo que
    sustenta a recusa — sem evento não há fato a preservar (coberto por
    `test_update_crm_aceita_qualificado_quando_nao_ha_evento`).

    O que a guarda no `where` garante além disso é que **não existe
    movimento de card sem gravação correspondente** — invariante do BANCO.
    Os dois `PUT` do Pipedrive saem depois dos commits e sem ordenação entre
    si, então o estágio final do card não é aferido aqui.
    """
    await criar_lead(phase="iniciou_conversa", google_event_id="ev-vivo")

    resultados = await asyncio.gather(
        update_crm.ainvoke({"phase": "qualificado"}),
        update_crm.ainvoke({"phase": "agendou_sessao"}),
    )

    gravacoes = [s for s in resultados if "Fase atualizada" in s]
    assert len(pipedrive.movidos) == len(gravacoes)
    assert (await ler_lead())["phase"] == "agendou_sessao"


async def test_qualificado_nao_desfaz_uma_sessao_agendada(limpar, turno, pipedrive):
    # Sequencial, sem corrida: a recusa não é um detalhe de concorrência, é
    # regra de funil. O `google_event_id` é o que a sustenta — é ele que
    # torna "reunião marcada" um fato verificável, e não um julgamento.
    await criar_lead(
        phase="agendou_sessao", google_event_id="ev-vivo", followup_active=False
    )

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    assert (await ler_lead())["phase"] == "agendou_sessao"
    assert pipedrive.movidos == []
    assert "tem sessão agendada" in saida


async def test_desqualificado_pode_sobrescrever_sessao_agendada(
    limpar, turno, pipedrive
):
    # `desqualificado` não é estágio anterior, é saída — um lead pode
    # revelar um desqualificador depois de agendar, e o time precisa ver
    # isso no funil.
    await criar_lead(phase="agendou_sessao")

    await update_crm.ainvoke({"phase": "desqualificado"})

    assert (await ler_lead())["phase"] == "desqualificado"


# --- Pipedrive: quando falha ------------------------------------------------


async def test_falha_no_card_nao_impede_a_gravacao_da_fase(
    limpar, turno, pipedrive, capsys
):
    await criar_lead()
    pipedrive.erro = RuntimeError("pipedrive fora do ar")

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    # O funil interno alimenta a régua de follow-up e o gate de ingestão —
    # ele não pode ficar refém do CRM externo estar de pé.
    assert (await ler_lead())["phase"] == "qualificado"
    assert "ATENÇÃO" in saida
    logs = capsys.readouterr().out
    assert "crm_card_nao_movido" in logs
    assert DEAL in logs


# --- Zero linhas afetadas é falha -------------------------------------------


async def test_gravar_fase_sem_lead_no_banco_reporta_ausente(limpar):
    # Sem checar o que voltou, o UPDATE que não casa nenhuma linha é
    # indistinguível do que casou — e a fase "gravada" só existe na frase
    # que a tool devolve ao agente.
    assert await crm.gravar_fase(E164, "qualificado") == "ausente"


async def test_gravar_fase_distingue_ausente_de_ja_aplicada(limpar):
    # Os dois casos casam zero linhas e são incidentes diferentes: lead que
    # sumiu do cadastro vs. outro turno que chegou primeiro.
    await criar_lead(phase="qualificado")

    assert await crm.gravar_fase(E164, "qualificado") == "inalterada"
    assert await crm.gravar_fase(E164, "agendou_sessao") == "gravou"


async def test_lead_apagado_entre_a_leitura_e_a_gravacao_reporta_falha(
    limpar, turno, pipedrive, monkeypatch, capsys
):
    # A corrida real: o lead existe quando a fase atual é lida e some antes
    # do UPDATE (consolidação de duplicata, limpeza manual). Só a leitura é
    # instrumentada — o caminho de escrita continua sendo o SQL de verdade.
    await criar_lead()
    original = crm.carregar_estado

    async def apagando(telefone: str):
        estado = await original(telefone)
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = %s", (CANONICO,))
        return estado

    monkeypatch.setattr(crm, "carregar_estado", apagando)

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    assert await ler_lead() is None
    assert "sumiu do cadastro" in saida
    assert "human_handover" in saida
    assert "crm_lead_inexistente_na_gravacao" in capsys.readouterr().out
    # O card não pode se mover para um lead que não está mais no cadastro.
    assert pipedrive.movidos == []


# --- Persistência de e-mail e faturamento -----------------------------------


async def test_persiste_email_e_faturamento(limpar, turno, pipedrive):
    await criar_lead()

    await update_crm.ainvoke(
        {
            "phase": "qualificado",
            "email": "ana@exemplo.com",
            "faturamento_mensal": "uns 30 mil",
        }
    )

    lido = await ler_lead()
    assert lido["email"] == "ana@exemplo.com"
    assert lido["faturamento_mensal"] == "uns 30 mil"


async def test_campos_vazios_nao_apagam_o_cadastro(limpar, turno, pipedrive):
    await criar_lead(email="ana@exemplo.com", faturamento_mensal="30 mil")

    await update_crm.ainvoke({"phase": "qualificado"})

    lido = await ler_lead()
    assert lido["email"] == "ana@exemplo.com"
    assert lido["faturamento_mensal"] == "30 mil"


async def test_nao_escreve_o_nome_do_lead(limpar, turno, pipedrive):
    # `leads_crm.name` entra no system prompt da Renata. A tool não expõe
    # caminho de escrita nele de propósito — ver o docstring de crm.py.
    await criar_lead()

    assert "name" not in update_crm.args
    await update_crm.ainvoke({"phase": "qualificado"})

    assert (await ler_lead())["name"] == "Ana"


# --- Entradas que a tool recusa ---------------------------------------------


@pytest.mark.parametrize("invalida", ["perdido", "iniciou_conversa", "", "QUALIFICADA"])
async def test_fase_fora_do_sop_e_recusada_sem_tocar_no_banco(
    limpar, turno, pipedrive, invalida
):
    await criar_lead()

    saida = await update_crm.ainvoke({"phase": invalida})

    assert (await ler_lead())["phase"] == "iniciou_conversa"
    assert pipedrive.movidos == []
    assert "não existe" in saida


async def test_lead_ausente_devolve_frase_e_nao_move_card(limpar, turno, pipedrive):
    saida = await update_crm.ainvoke({"phase": "qualificado"})

    assert await ler_lead() is None
    assert pipedrive.movidos == []
    assert "human_handover" in saida


async def test_sem_telefone_no_turno_nao_atualiza_ninguem(limpar, pipedrive):
    await criar_lead()

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    assert (await ler_lead())["phase"] == "iniciou_conversa"
    assert pipedrive.movidos == []
    assert "human_handover" in saida


# --- human_handover ---------------------------------------------------------


async def test_handover_zera_os_dois_flags(limpar, turno, evolution):
    await criar_lead(agent_active=True, followup_active=True)

    saida = await human_handover.ainvoke({"motivo": "lead pediu um humano"})

    lido = await ler_lead()
    assert lido["agent_active"] is False
    assert lido["followup_active"] is False
    assert "desligado" in saida


async def test_handover_avisa_o_responsavel_com_motivo_e_link(limpar, turno, evolution):
    await criar_lead()

    await human_handover.ainvoke({"motivo": "tentativa de jailbreak"})

    assert len(evolution.enviados) == 1
    assert evolution.enviados[0]["to"] == NOTIFY_ENVIADO
    corpo = evolution.enviados[0]["body"]
    assert "tentativa de jailbreak" in corpo
    assert "https://wa.me/5511955552222" in corpo


async def test_handover_nao_levanta_quando_o_envio_falha(
    limpar, turno, evolution, capsys
):
    await criar_lead()
    evolution.erro = RuntimeError("evolution fora do ar")

    # O desligamento é o que importa. Uma exceção aqui derrubaria o turno e
    # deixaria o agente ligado num lead que precisa de humano.
    saida = await human_handover.ainvoke({"motivo": "erro técnico persistente"})

    lido = await ler_lead()
    assert lido["agent_active"] is False
    assert lido["followup_active"] is False
    assert "desligado" in saida
    assert "handover_aviso_nao_enviado" in capsys.readouterr().out


async def test_handover_sem_numero_configurado_ainda_desliga(
    limpar, turno, evolution, monkeypatch
):
    monkeypatch.setattr(handover.settings, "handover_notify_phone", "")
    await criar_lead()

    saida = await human_handover.ainvoke({"motivo": "erro"})

    assert (await ler_lead())["agent_active"] is False
    assert evolution.enviados == []
    assert "desligado" in saida


async def test_handover_com_lead_ausente_nao_diz_que_desligou(limpar, turno, evolution):
    saida = await human_handover.ainvoke({"motivo": "erro"})

    # Zero linhas afetadas: o agente continua ligado. Dizer o contrário
    # deixaria o lead conversando com um robô que deveria ter parado.
    assert "ATENÇÃO" in saida
    assert "não consegui desligar" in saida
    # O responsável é avisado mesmo assim — aqui ele é mais necessário.
    assert len(evolution.enviados) == 1
    assert "nao consegui pausar" in evolution.enviados[0]["body"]


async def test_handover_colapsa_quebras_de_linha_do_motivo(limpar, turno, evolution):
    await criar_lead()

    await human_handover.ainvoke({"motivo": "linha 1\nlinha 2\n\nlinha 3"})

    corpo = evolution.enviados[0]["body"]
    assert "Motivo: linha 1 linha 2 linha 3" in corpo
    # Uma quebra crua no motivo faria o link e o estado migrarem de linha.
    assert corpo.count("\n") == 3


async def test_handover_corta_motivo_gigante(limpar, turno, evolution):
    await criar_lead()

    await human_handover.ainvoke({"motivo": "x" * 5000})

    corpo = evolution.enviados[0]["body"]
    assert len(corpo) < 500
    assert "https://wa.me/" in corpo


# --- Telefone do responsável ------------------------------------------------


async def test_numero_de_aviso_digitado_por_humano_chega_ao_destino(
    limpar, turno, evolution, monkeypatch
):
    # `"11 97777-6666"` sobrevive a qualquer checagem de "está preenchido" e
    # só morre no envio, dentro do except que existe para não derrubar o
    # desligamento: handover silencioso COM a variável preenchida.
    monkeypatch.setattr(handover.settings, "handover_notify_phone", "11 97777-6666")
    await criar_lead()

    await human_handover.ainvoke({"motivo": "erro"})

    assert len(evolution.enviados) == 1
    assert evolution.enviados[0]["to"] == NOTIFY_ENVIADO


async def test_numero_de_aviso_irreconhecivel_nao_tenta_enviar(
    limpar, turno, evolution, monkeypatch, capsys
):
    monkeypatch.setattr(handover.settings, "handover_notify_phone", "ramal 42")
    await criar_lead()

    saida = await human_handover.ainvoke({"motivo": "erro"})

    assert (await ler_lead())["agent_active"] is False
    assert evolution.enviados == []
    assert "handover_numero_de_aviso_invalido" in capsys.readouterr().out
    assert "Não consegui avisar o responsável" in saida


# --- Texto interno nunca é conteúdo para o lead -----------------------------


async def test_todo_retorno_das_duas_tools_vem_marcado(
    limpar, turno, pipedrive, evolution
):
    """Cada string destas tools nomeia máquina interna.

    "Pipedrive", "cadastro do lead", "human_handover", "follow-up": nada
    disso pode chegar ao WhatsApp de um lead da EleveC. O prefixo é o que
    permite ao prompt ter UMA regra verificável em vez de uma lista de
    frases que envelhece a cada edição de tool.
    """
    await criar_lead()

    saidas = [
        await update_crm.ainvoke({"phase": "fase_que_nao_existe"}),
        await update_crm.ainvoke({"phase": "qualificado"}),
        await update_crm.ainvoke({"phase": "qualificado"}),
        await update_crm.ainvoke({"phase": "agendou_sessao"}),
        await human_handover.ainvoke({"motivo": "erro técnico"}),
    ]

    for saida in saidas:
        assert saida.startswith(PREFIXO_INTERNO), saida


async def test_marcador_sobrevive_aos_caminhos_de_falha(limpar, turno, evolution):
    saidas = [
        await update_crm.ainvoke({"phase": "qualificado"}),  # lead ausente
        await human_handover.ainvoke({"motivo": "erro"}),  # lead ausente
    ]

    for saida in saidas:
        assert saida.startswith(PREFIXO_INTERNO), saida


# --- Coordenação com a Task 5: a coluna passa a vencer o argumento ----------


async def test_email_gravado_pelo_update_crm_vence_o_argumento_do_agendar(
    limpar, turno, pipedrive, monkeypatch
):
    """O portão da "sequência INVIOLÁVEL" deixa de depender do modelo.

    Antes desta task nenhuma tool persistia `email`/`faturamento_mensal`, e
    `calendar_agendar` aceitava os dois por argumento — o modelo satisfazia
    a sequência na mesma chamada. Com `update_crm` gravando, o
    `_preferir_coluna` de `agenda.py` passa a ter o valor real do lead para
    preferir, e um argumento divergente no meio do turno não o sobrepõe.
    """

    class AgendaFalsa:
        def __init__(self):
            self.participantes: list[str] = []

        async def listar_eventos(self, inicio, fim, max_results=250):
            return []

        async def criar_evento(
            self, summary, inicio, fim, participantes=None, event_id=None, **kwargs
        ):
            self.participantes = list(participantes or [])
            # Corpo realista: o cliente devolve o evento com `start` e
            # `status`, e `calendar_agendar` confere os dois antes de
            # confirmar (o insert pode ter batido em 409 e devolvido um
            # evento cancelado ou de outro horário). `attendees` volta
            # porque o Event criado os traz — é o que prova que o convite
            # saiu de verdade neste turno (ver `agenda.convite_para`).
            return {
                "id": event_id,
                "status": "confirmed",
                "start": {"dateTime": inicio.isoformat()},
                "end": {"dateTime": fim.isoformat()},
                "attendees": [{"email": email} for email in self.participantes],
            }

    falsa = AgendaFalsa()
    monkeypatch.setattr(agenda, "obter_cliente", lambda: falsa)
    monkeypatch.setattr(agenda, "agora_sp", lambda: TERCA)

    await criar_lead()
    await update_crm.ainvoke(
        {
            "phase": "qualificado",
            "email": "real@exemplo.com",
            "faturamento_mensal": "30 mil",
        }
    )

    saida = await agenda.calendar_agendar.ainvoke(
        {"inicio": "2026-02-12T13:00", "email": "inventado@exemplo.com"}
    )

    assert falsa.participantes == ["real@exemplo.com"]
    assert "real@exemplo.com" in saida
    assert "inventado@exemplo.com" not in saida


# --- O ciclo agenda → cancela → reagenda ------------------------------------
#
# A guarda de retrocesso da fix round 1 fechou a volta legítima: `agenda.py`
# não escrevia `phase` em lugar nenhum, então uma reunião cancelada deixava o
# lead preso em `agendou_sessao` com o follow-up morto. Duas correções, e as
# duas precisam de teste porque cobrem caminhos diferentes:
#
# 1. `calendar_delete` devolve a fase (garantido em código, não depende do
#    modelo lembrar de chamar `update_crm`);
# 2. `update_crm` volta a aceitar `qualificado` quando não há
#    `google_event_id` — o cancelamento feito fora do sistema não vira beco.


class AgendaFalsa:
    """Cria e apaga evento. Nenhuma chamada ao Google."""

    def __init__(self):
        self.criados: list[str] = []
        self.deletados: list[str] = []

    async def listar_eventos(self, inicio, fim, max_results=250):
        return []

    async def criar_evento(
        self, summary, inicio, fim, participantes=None, event_id=None, **kwargs
    ):
        self.criados.append(event_id)
        return {
            "id": event_id,
            "status": "confirmed",
            "start": {"dateTime": inicio.isoformat()},
            "end": {"dateTime": fim.isoformat()},
            "attendees": [{"email": e} for e in (participantes or [])],
        }

    async def deletar_evento(self, event_id, notificar=True):
        self.deletados.append(event_id)


@pytest.fixture
def agenda_falsa(monkeypatch):
    falsa = AgendaFalsa()
    monkeypatch.setattr(agenda, "obter_cliente", lambda: falsa)
    monkeypatch.setattr(agenda, "agora_sp", lambda: TERCA)
    return falsa


async def test_ciclo_completo_agenda_cancela_reagenda(
    limpar, turno, pipedrive, agenda_falsa
):
    """O caminho que a guarda de retrocesso tinha fechado, ponta a ponta."""
    await criar_lead(email="ana@exemplo.com", faturamento_mensal="30 mil")
    await update_crm.ainvoke({"phase": "qualificado"})

    # --- agenda ---
    await agenda.calendar_agendar.ainvoke({"inicio": "2026-02-12T13:00"})
    await update_crm.ainvoke({"phase": "agendou_sessao"})

    marcado = await ler_lead()
    assert marcado["phase"] == "agendou_sessao"
    assert marcado["followup_active"] is False
    assert marcado["google_event_id"] is not None

    # --- cancela ---
    await agenda.calendar_delete.ainvoke({})

    cancelado = await ler_lead()
    assert cancelado["google_event_id"] is None
    assert cancelado["phase"] == "qualificado", "o funil precisa voltar junto"
    assert cancelado["followup_active"] is True, "o lead volta para a régua"

    # --- reagenda ---
    await agenda.calendar_agendar.ainvoke({"inicio": "2026-02-13T14:00"})
    saida = await update_crm.ainvoke({"phase": "agendou_sessao"})

    remarcado = await ler_lead()
    assert remarcado["phase"] == "agendou_sessao"
    assert remarcado["google_event_id"] is not None
    assert "Fase atualizada" in saida
    assert len(agenda_falsa.criados) == 2
    assert len(agenda_falsa.deletados) == 1
    # 12 (qualificado) → 13 (agendou) → 13 de novo, depois do ciclo.
    assert pipedrive.movidos == [(DEAL, 12), (DEAL, 13), (DEAL, 13)]


async def test_cancelamento_nao_religa_followup_de_lead_pausado(
    limpar, turno, agenda_falsa
):
    # `human_handover` zerou `agent_active`. Religar a cobrança aqui
    # desfaria a decisão de uma pessoa.
    await criar_lead(
        phase="agendou_sessao",
        google_event_id="ev-1",
        followup_active=False,
        agent_active=False,
    )

    await agenda.calendar_delete.ainvoke({})

    lido = await ler_lead()
    assert lido["phase"] == "qualificado"
    assert lido["followup_active"] is False


async def test_cancelamento_nao_promove_lead_desqualificado(
    limpar, turno, agenda_falsa
):
    # Tinha reunião marcada e foi desqualificado depois. Cancelar o evento
    # não pode devolvê-lo ao funil como se ainda fosse oportunidade.
    await criar_lead(phase="desqualificado", google_event_id="ev-1")

    await agenda.calendar_delete.ainvoke({})

    lido = await ler_lead()
    assert lido["phase"] == "desqualificado"
    assert lido["google_event_id"] is None


async def test_reverter_e_idempotente(limpar, turno):
    await criar_lead(phase="agendou_sessao", followup_active=False)

    assert await crm.reverter_fase_apos_cancelamento(E164) == "revertida"
    assert await crm.reverter_fase_apos_cancelamento(E164) == "nao_aplicavel"
    assert (await ler_lead())["phase"] == "qualificado"


# --- A guarda de retrocesso, agora condicionada ao evento -------------------


async def test_update_crm_aceita_qualificado_quando_nao_ha_evento(
    limpar, turno, pipedrive
):
    """Cancelamento feito fora do sistema não pode virar beco sem saída.

    A doutrina de `shared/leads.py` diz que `agendou_sessao` vence porque
    "reunião marcada é fato verificável (existe evento no Google Calendar)".
    Sem `google_event_id` não há fato a preservar — e `qualificado` deixa de
    ser retrocesso.
    """
    await criar_lead(
        phase="agendou_sessao", google_event_id=None, followup_active=False
    )

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    lido = await ler_lead()
    assert lido["phase"] == "qualificado"
    assert lido["followup_active"] is True, "voltar para qualificado religa a régua"
    assert "Fase atualizada" in saida


async def test_update_crm_recusa_qualificado_com_evento_ainda_vinculado(
    limpar, turno, pipedrive
):
    await criar_lead(phase="agendou_sessao", google_event_id="ev-vivo")

    saida = await update_crm.ainvoke({"phase": "qualificado"})

    assert (await ler_lead())["phase"] == "agendou_sessao"
    assert pipedrive.movidos == []
    # A instrução da recusa precisa apontar para um caminho que existe.
    assert "calendar_delete" in saida
    assert "já devolve o lead" in saida


async def test_volta_para_qualificado_nao_religa_followup_de_lead_pausado(
    limpar, turno, pipedrive
):
    await criar_lead(
        phase="agendou_sessao",
        google_event_id=None,
        followup_active=False,
        agent_active=False,
    )

    await update_crm.ainvoke({"phase": "qualificado"})

    lido = await ler_lead()
    assert lido["phase"] == "qualificado"
    assert lido["followup_active"] is False
