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

from datetime import datetime
from typing import Any

import pytest
from langchain_core.runnables.config import var_child_runnable_config

from whatsapp_langchain.agents.catalog.elevec_sdr.tools import agenda, crm, handover
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.crm import update_crm
from whatsapp_langchain.agents.catalog.elevec_sdr.tools.handover import human_handover
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.google_calendar import FUSO

CANONICO = "551155552222"
E164 = "+5511955552222"

DEAL = "7788"
NOTIFY = "+5511977776666"

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
    }
    base.update(campos)
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm"
            " (phone, name, pipedriveid, email, faturamento_mensal,"
            "  source, phase, followup_active, agent_active)"
            " values (%s, 'Ana', %s, %s, %s, 'whatsapp_direct',"
            "         %s::lead_phase, %s, %s)",
            (
                CANONICO,
                base["pipedriveid"],
                base["email"],
                base["faturamento_mensal"],
                base["phase"],
                base["followup_active"],
                base["agent_active"],
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
    monkeypatch.setattr(crm.settings, "pipedrive_stage_qualificado", 12)
    monkeypatch.setattr(crm.settings, "pipedrive_stage_agendado", 13)
    return falso


@pytest.fixture
def evolution(monkeypatch):
    falso = EvolutionFalso()
    monkeypatch.setattr(handover, "obter_cliente", lambda: falso)
    monkeypatch.setattr(handover.settings, "handover_notify_phone", NOTIFY)
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


async def test_gravar_fase_sem_lead_no_banco_devolve_false(limpar):
    # Sem checar `rowcount`, o UPDATE que não casa nenhuma linha é
    # indistinguível do que casou — e a fase "gravada" só existe na frase
    # que a tool devolve ao agente.
    assert await crm.gravar_fase(E164, "qualificado") is False


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
    assert "Não consegui registrar" in saida
    assert "human_handover" in saida
    assert "crm_lead_inexistente_na_gravacao" in capsys.readouterr().out


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
    assert evolution.enviados[0]["to"] == NOTIFY
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
            return {"id": event_id}

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
