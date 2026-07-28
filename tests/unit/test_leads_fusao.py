"""Testes das regras de fusão de `shared/leads.py` — puros, sem banco.

`_vencedor_pausa`, `_fundir`, `_rank_fase`/`_mais_avancada` e
`_fundir_metadata` continuam existindo e sendo chamados por `aplicar_gate`
(`leads.py:150,190`), mas o CHECK `leads_crm_phone_canonico_check`
(migração 014) tornou o cenário que os aciona -- duas linhas físicas para
o mesmo telefone em `leads_crm` -- irrepresentável em produção. Os testes
de integração que exercitavam essas regras via `aplicar_gate` real foram
removidos de `test_gate_ingestao.py` pela mesma razão (não dá mais para
inserir a fixture que os provocava).

Isso não torna a regra descartável de testar: `_vencedor_pausa`/`_fundir`
são a última linha de defesa se alguém enfraquecer o CHECK no futuro (ou se
outro caminho de escrita passar a bypassar o gate), e continuam sendo a
REFERÊNCIA que a migração 014 reimplementa em PL/pgSQL -- se a regra em
Python mudar sem a cópia em SQL mudar junto (ou vice-versa), é aqui que uma
mutação pegaria a divergência primeiro, sem precisar de banco.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from whatsapp_langchain.shared.leads import (
    _fundir,
    _fundir_metadata,
    _mais_avancada,
    _mais_recente,
    _rank_fase,
    _vencedor_pausa,
)

_AGORA = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _linha(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "phone": "551100000000",
        "pipedriveid": None,
        "name": None,
        "username": None,
        "email": None,
        "faturamento_mensal": None,
        "qualificacao_notas": None,
        "google_event_id": None,
        "source": None,
        "phase": None,
        "followup_count": 0,
        "followup_active": True,
        "agent_active": True,
        "agent_reactivate_at": None,
        "last_interaction_at": _AGORA,
        "metadata": {},
    }
    base.update(overrides)
    return base


def test_rank_fase_agendou_sessao_vence_tudo():
    """Fato verificável (evento no calendário) vence julgamento — mesmo
    perdido/desqualificado, mesmo sendo a fase mais recente."""
    assert _rank_fase("agendou_sessao") > _rank_fase("perdido")
    assert _rank_fase("agendou_sessao") > _rank_fase("desqualificado")
    assert _rank_fase("agendou_sessao") > _rank_fase("qualificado")


def test_rank_fase_ordem_completa():
    ordem = [
        "formulario_preenchido",
        "iniciou_conversa",
        "qualificado",
        "desqualificado",
        "agendou_sessao",
    ]
    ranks = [_rank_fase(f) for f in ordem]
    assert ranks == sorted(ranks), "a ordem declarada tem que ser crescente"


def test_rank_fase_perdido_e_desqualificado_empatam():
    assert _rank_fase("perdido") == _rank_fase("desqualificado")


def test_rank_fase_desconhecida_fica_acima_de_none_abaixo_do_resto():
    """Fase nova (enum que este código ainda não conhece) usa rank 0 — tem
    que continuar vencendo `None`, para uma fase nova nunca ser apagada por
    uma linha sem fase nenhuma, mas nunca vencer uma fase real conhecida."""
    desconhecida = _rank_fase("fase_que_nao_existe_ainda")
    assert desconhecida > _rank_fase(None)
    assert desconhecida < _rank_fase("formulario_preenchido")


def test_mais_avancada_nunca_regride_mesmo_com_a_antiga_mais_recente():
    """`_mais_avancada` decide por RANK, nunca por recência — é o oposto de
    `_mais_recente`. Sem essa distinção, um grupo {agendou_sessao,
    iniciou_conversa} com `iniciou_conversa` mais recente consolidaria para
    `iniciou_conversa` — que não é fase terminal, e a régua voltaria a
    perseguir quem já agendou."""
    assert _mais_avancada("agendou_sessao", "iniciou_conversa") == "agendou_sessao"
    assert _mais_avancada("iniciou_conversa", "agendou_sessao") == "agendou_sessao"


def test_mais_avancada_none_nunca_apaga_fase_real():
    assert _mais_avancada(None, "qualificado") == "qualificado"
    assert _mais_avancada("qualificado", None) == "qualificado"


def test_mais_recente_cai_para_o_nao_nulo():
    a = _linha(last_interaction_at=None)
    b = _linha(last_interaction_at=_AGORA)
    assert _mais_recente(a, b) is b
    assert _mais_recente(b, a) is b


def test_mais_recente_desempata_por_timestamp():
    antiga = _linha(last_interaction_at=_AGORA - timedelta(hours=3))
    recente = _linha(last_interaction_at=_AGORA)
    assert _mais_recente(antiga, recente) is recente
    assert _mais_recente(recente, antiga) is recente


def test_vencedor_pausa_ambos_pausados_desempate_por_recencia():
    """Entre dois lados pausados, o mais recente decide — não há "mais
    pausado", só "mais atual"."""
    antiga = _linha(agent_active=False, last_interaction_at=_AGORA - timedelta(hours=3))
    recente = _linha(agent_active=False, last_interaction_at=_AGORA)
    assert _vencedor_pausa(antiga, recente) is recente
    assert _vencedor_pausa(recente, antiga) is recente


def test_vencedor_pausa_canonica_pausada_vence_mesmo_sendo_mais_antiga():
    """False vence sempre — errar para o lado de não mandar mensagem é
    recuperável, mandar para quem pediu silêncio não é."""
    canonica_pausada = _linha(
        agent_active=False, last_interaction_at=_AGORA - timedelta(hours=5)
    )
    legada_ativa = _linha(agent_active=True, last_interaction_at=_AGORA)
    assert _vencedor_pausa(canonica_pausada, legada_ativa) is canonica_pausada


def test_vencedor_pausa_legada_pausada_vence_mesmo_sendo_mais_antiga():
    canonica_ativa = _linha(agent_active=True, last_interaction_at=_AGORA)
    legada_pausada = _linha(
        agent_active=False, last_interaction_at=_AGORA - timedelta(hours=5)
    )
    assert _vencedor_pausa(canonica_ativa, legada_pausada) is legada_pausada


def test_vencedor_pausa_ambos_ativos_desempate_por_recencia():
    antiga = _linha(agent_active=True, last_interaction_at=_AGORA - timedelta(hours=3))
    recente = _linha(agent_active=True, last_interaction_at=_AGORA)
    assert _vencedor_pausa(antiga, recente) is recente


def test_fundir_metadata_precedencia_do_topo_por_chave():
    """Chave em comum: o mais recente (`recente`, o "topo") vence. Chave só
    da antiga sobrevive."""
    resultado = _fundir_metadata(
        recente={"utm": "nova", "origem": "linkedin"},
        antiga={"utm": "antiga"},
        telefones=set(),
    )
    assert resultado["utm"] == "nova"
    assert resultado["origem"] == "linkedin"


def test_fundir_metadata_acumula_linhas_fundidas_de_fusoes_encadeadas():
    """Uma fusão em cima de outra (a linha canônica já carrega
    `linhas_fundidas` de uma consolidação anterior) tem que ACUMULAR, não
    sobrescrever — é a única prova de que as linhas absorvidas existiram."""
    resultado = _fundir_metadata(
        recente={"linhas_fundidas": ["5511900000001"]},
        antiga={},
        telefones={"5511900000002", "5511900000003"},
    )
    assert set(resultado["linhas_fundidas"]) == {
        "5511900000001",
        "5511900000002",
        "5511900000003",
    }


def test_fundir_metadata_ignora_valor_nao_dict():
    """`metadata` pode chegar `None` do banco — não pode estourar."""
    resultado = _fundir_metadata(recente=None, antiga=None, telefones={"5511900000001"})
    assert resultado["linhas_fundidas"] == ["5511900000001"]


def test_fundir_agent_reactivate_at_segue_o_vencedor_da_pausa_nao_e_coalescido():
    """O Importante que ficou descoberto depois da remoção dos testes de
    integração: as duas linhas têm `agent_active=True` (mensagem seria
    aceita), então a fusão É persistida — mas a linha vencedora (mais
    recente) tem `agent_reactivate_at=NULL`, e a antiga carrega um valor
    obsoleto. `COALESCE(recente, antiga)` ressuscitaria o valor obsoleto;
    o resultado tem que ser `NULL`, porque ali `NULL` é estado significativo
    ("sem reativação agendada"), não ausência de dado a preencher."""
    canonica = _linha(
        phone="551100000010",
        agent_active=True,
        agent_reactivate_at=None,
        last_interaction_at=_AGORA,
    )
    legada = _linha(
        phone="5511900000010",
        agent_active=True,
        agent_reactivate_at=_AGORA + timedelta(days=3),
        last_interaction_at=_AGORA - timedelta(hours=5),
    )
    mesclado = _fundir(canonica, legada)
    assert mesclado["agent_reactivate_at"] is None, (
        "coalesce ressuscitaria o valor obsoleto da linha antiga"
    )


def test_fundir_agent_reactivate_at_do_lado_pausado_sobrevive():
    """Espelho do teste acima: quando quem vence É o lado com
    `agent_reactivate_at` preenchido (por estar pausado), o valor tem que
    sobreviver — não é apagado só porque a fusão em geral prefere `NULL`."""
    canonica = _linha(
        phone="551100000011",
        agent_active=False,
        agent_reactivate_at=_AGORA + timedelta(days=1),
        last_interaction_at=_AGORA - timedelta(hours=1),
    )
    legada = _linha(
        phone="5511900000011",
        agent_active=True,
        agent_reactivate_at=None,
        last_interaction_at=_AGORA,
    )
    mesclado = _fundir(canonica, legada)
    assert mesclado["agent_reactivate_at"] == canonica["agent_reactivate_at"]
    assert mesclado["agent_active"] is False


def test_fundir_phase_e_a_mais_avancada_mesmo_vindo_da_linha_mais_antiga():
    canonica = _linha(phone="551100000012", phase="perdido", last_interaction_at=_AGORA)
    legada = _linha(
        phone="5511900000012",
        phase="agendou_sessao",
        last_interaction_at=_AGORA - timedelta(days=30),
    )
    mesclado = _fundir(canonica, legada)
    assert mesclado["phase"] == "agendou_sessao", (
        "agendou_sessao vence mesmo sendo a linha bem mais antiga"
    )


def test_fundir_followup_count_e_o_maior_nao_o_do_vencedor_de_recencia():
    canonica = _linha(
        phone="551100000013", followup_count=0, last_interaction_at=_AGORA
    )
    legada = _linha(
        phone="5511900000013",
        followup_count=2,
        last_interaction_at=_AGORA - timedelta(days=10),
    )
    mesclado = _fundir(canonica, legada)
    assert mesclado["followup_count"] == 2


def test_fundir_campos_de_conteudo_vem_do_mais_recente_cai_para_o_outro_quando_nulo():
    canonica = _linha(
        phone="551100000014",
        email=None,
        name="Nome Recente",
        last_interaction_at=_AGORA,
    )
    legada = _linha(
        phone="5511900000014",
        email="antigo@x.com",
        name="Nome Antigo",
        last_interaction_at=_AGORA - timedelta(days=5),
    )
    mesclado = _fundir(canonica, legada)
    assert mesclado["name"] == "Nome Recente", "não-nulo do mais recente vence"
    assert mesclado["email"] == "antigo@x.com", "nulo no mais recente cai para o outro"
