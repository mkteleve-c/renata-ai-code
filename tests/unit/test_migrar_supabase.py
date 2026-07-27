"""Testes de `normalizar_telefone` -- puro, sem I/O, sem rede.

`test_saida_sempre_satisfaz_o_check_do_banco` é o mais importante do
arquivo: amarra a saída do script ao CHECK real de `leads_crm.phone`,
extraído do texto das migrações 007 e 014 -- nunca copiado à mão -- para
que este teste acompanhe se a constraint mudar de novo.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.migrar_supabase import (
    Descarte,
    LinhaFundida,
    LinhaOrigem,
    Normalizado,
    agrupar_por_canonico,
    fundir_grupo,
    fundir_todos,
    gerar_relatorio,
    normalizar_telefone,
)

_RAIZ = Path(__file__).resolve().parents[2]
_MIGRACAO_007 = _RAIZ / "db" / "migrations" / "007_elevec.sql"
_MIGRACAO_014 = _RAIZ / "db" / "migrations" / "014_uma_linha_por_pessoa.sql"


def _regras_do_check_leads_crm() -> list[tuple[re.Pattern[str], bool]]:
    """Extrai as regras do CHECK de `leads_crm.phone` direto das migrações.

    Não copia a regex para o teste: lê o SQL real de 007 (a regra de
    base, `phone ~ '^[0-9]{8,15}$'`) e de 014 (as três regras que travam a
    forma canônica) e monta os predicados a partir do texto. Se a
    constraint mudar de novo -- ela já levou quatro rodadas de revisão --
    este teste acompanha sem precisar ser editado à mão.
    """
    predicados: list[tuple[re.Pattern[str], bool]] = []

    texto_007 = _MIGRACAO_007.read_text()
    bloco_leads_crm = re.search(
        r"CREATE TABLE IF NOT EXISTS leads_crm \((.*?)\n\);", texto_007, re.DOTALL
    )
    assert bloco_leads_crm, "não achei a definição de leads_crm em 007_elevec.sql"

    texto_014 = _MIGRACAO_014.read_text()
    bloco_check = re.search(
        r"ADD CONSTRAINT leads_crm_phone_canonico_check\s*CHECK \((.*?)\);",
        texto_014,
        re.DOTALL,
    )
    assert bloco_check, "não achei o CHECK novo em 014_uma_linha_por_pessoa.sql"

    for bloco in (bloco_leads_crm.group(1), bloco_check.group(1)):
        for operador, regex in re.findall(r"phone\s*(!?~)\s*'([^']+)'", bloco):
            predicados.append((re.compile(regex), operador == "~"))

    # Guarda contra extração silenciosamente vazia: se a migração mudar de
    # forma (menos ou mais cláusulas), o teste tem que acusar em vez de
    # passar trivialmente com uma lista vazia de predicados.
    assert len(predicados) == 4, (
        f"esperava 4 predicados (1 de 007 + 3 de 014), achei {len(predicados)} "
        "-- o CHECK mudou e este teste precisa de atenção, não só passar"
    )
    return predicados


def _satisfaz_check_do_banco(valor: str) -> bool:
    return all(
        bool(regex.search(valor)) == deve_casar
        for regex, deve_casar in _regras_do_check_leads_crm()
    )


def test_o_extrator_do_check_bate_com_exemplos_conhecidos():
    """Sanity check do extrator em si, antes de usá-lo contra o script."""
    assert _satisfaz_check_do_banco("551187654321")  # BR canônico
    assert _satisfaz_check_do_banco("258864038352")  # Moçambique, 12 dígitos
    assert not _satisfaz_check_do_banco("5511987654321")  # com o 9º dígito
    assert not _satisfaz_check_do_banco("55011987654321")  # zero de tronco
    assert not _satisfaz_check_do_banco("14242123771")  # 11 dígitos -- colide
    assert not _satisfaz_check_do_banco("1234567890")  # 10 dígitos -- colide
    assert not _satisfaz_check_do_banco("1234567")  # 7 dígitos -- curto demais


@pytest.mark.parametrize(
    "bruto,esperado",
    [
        ("+5511987654321", "551187654321"),  # BR com 9 e com +
        ("5511987654321", "551187654321"),
        ("551187654321", "551187654321"),  # já canônico
        ("11987654321", "551187654321"),  # local com 9
        ("1187654321", "551187654321"),  # local sem 9
        ("55011987654321", "551187654321"),  # zero de tronco
        ("(11) 98765-4321", "551187654321"),  # máscara
    ],
)
def test_formas_brasileiras_convergem(bruto, esperado):
    resultado = normalizar_telefone(bruto)
    assert resultado.canonico == esperado
    assert resultado.motivo is None


@pytest.mark.parametrize(
    "bruto,motivo",
    [
        ("null", "telefone_ausente"),
        ("", "telefone_ausente"),
        (None, "telefone_ausente"),
        ("NULL", "telefone_ausente"),  # variação de caixa
        ("   ", "telefone_ausente"),  # só espaços
        ("+14242123771", "colide_com_forma_local_br"),  # EUA, 11 dígitos
        ("519985344", "digitos_insuficientes"),
        ("5511666666665", "sequencia_implausivel"),
    ],
)
def test_descartes_tem_motivo_nomeado(bruto, motivo):
    resultado = normalizar_telefone(bruto)
    assert resultado.canonico is None
    assert resultado.motivo == motivo


def test_estrangeiro_de_12_digitos_passa_intacto():
    """Moçambique e Portugal cabem no CHECK; EUA não, por colisão de tamanho."""
    assert normalizar_telefone("258864038352").canonico == "258864038352"
    assert normalizar_telefone("351914355881").canonico == "351914355881"


def test_forma_local_br_de_10_e_11_digitos_e_descartada():
    """Sem DDI, 10 ou 11 dígitos é ambíguo -- e o CHECK bane os dois."""
    assert normalizar_telefone("1187654321").canonico == "551187654321"  # sem 9: BR
    # Um "sem DDI" de 11 dígitos que TAMBÉM não bate o padrão LOCAL_COM_9
    # (9 fora da posição certa) não converge para BR -- e cai exatamente
    # no comprimento que colide com estrangeiro.
    r = normalizar_telefone("12345678901")
    assert r.canonico is None
    assert r.motivo == "colide_com_forma_local_br"


@pytest.mark.parametrize(
    "bruto",
    [
        None,
        "",
        "   ",
        "null",
        "NULL",
        "519985344",
        "5511666666665",
        "12345",
        "+14242123771",
        "1234567890",
    ],
)
def test_descarte_nunca_preenche_os_dois_campos(bruto):
    """`canonico is None` sse `motivo` está preenchido -- nunca os dois juntos."""
    resultado = normalizar_telefone(bruto)
    assert isinstance(resultado, Normalizado)
    assert (resultado.canonico is None) != (resultado.motivo is None)


# Amostra representativa das formas medidas na base legada em 27/07/2026
# (não são as 3.373 linhas reais -- a credencial do Supabase é segredo e
# esta suíte não usa rede -- mas cobre cada categoria da tabela do plano:
# BR com/sem 9º dígito, zero de tronco, local sem DDI, máscara, os três
# estrangeiros achados em produção, ausência e lixo implausível).
_FORMAS_REAIS_REPRESENTATIVAS = [
    "+5511987654321",
    "5511987654321",
    "551187654321",
    "11987654321",
    "1187654321",
    "55011987654321",
    "(11) 98765-4321",
    "+5521998887766",
    "5521987654321",
    "552199988776",
    "21987654321",
    "5531988776655",
    "553187654321",
    "5541987654321",
    "554187654321",
    "+5561999998888",
    "5561988776655",
    "556187654321",
    "55031987654321",  # zero de tronco, outro DDD
    "05531987654321",  # zero de tronco em outra forma
    "3187654321",  # local sem DDI, sem 9
    "8532165498",  # local sem DDI, sem 9, outro DDD
    "258864038352",  # Moçambique -- qualificado, ativo
    "351914355881",  # Portugal -- follow-up ativo
    "+14242123771",  # EUA -- colide com forma local BR
    None,
    "",
    "   ",
    "null",
    "NULL",
    "519985344",
    "5511666666665",
    "12345",
    "1234567890",
    "12345678901",
]


def test_saida_sempre_satisfaz_o_check_do_banco():
    """Qualquer canônico devolvido tem que poder ser inserido em leads_crm.

    Este é o teste que importa mais: se `normalizar_telefone` devolver
    algo que o CHECK recusa, a importação real quebra no meio, com parte
    dos 3.373 leads dentro e parte fora.
    """
    algum_canonico_produzido = False
    for bruto in _FORMAS_REAIS_REPRESENTATIVAS:
        resultado = normalizar_telefone(bruto)
        if resultado.canonico is None:
            continue
        algum_canonico_produzido = True
        assert _satisfaz_check_do_banco(resultado.canonico), (
            f"{bruto!r} normalizou para {resultado.canonico!r}, "
            "que o CHECK de leads_crm.phone recusaria"
        )

    # Guarda contra um bug que descartasse tudo silenciosamente e fizesse
    # o loop acima nunca exercitar a asserção de verdade.
    assert algum_canonico_produzido


# =============================================================================
# Fusão de duplicatas e relatório (Task 3)
# =============================================================================
#
# Timestamps NUNCA idênticos entre linhas do mesmo grupo -- duas tasks da
# Fase 3 produziram testes autoconfirmatórios exatamente com timestamp
# igual entre linhas do mesmo grupo, e os dois vícios só foram pegos por
# revisão humana, nunca pela suíte. `_t()` sempre desloca por um número
# diferente de minutos a partir de uma base fixa.

_BASE = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _t(minutos: int) -> datetime:
    return _BASE + timedelta(minutes=minutos)


def _linha(
    phone: str | None,
    *,
    phase: str | None = None,
    created_at: datetime | None = None,
    last_interaction_at: datetime | None = None,
    pipedriveid: str | None = None,
    email: str | None = None,
    name: str | None = None,
    username: str | None = None,
    source: str | None = None,
    followup_count: int | None = 0,
    agent_active: bool | None = True,
    followup_active: bool | None = True,
    agent_reactivate_at: datetime | None = None,
    metadata: dict | None = None,
) -> LinhaOrigem:
    return LinhaOrigem(
        phone=phone,
        phase=phase,
        created_at=created_at,
        last_interaction_at=last_interaction_at,
        pipedriveid=pipedriveid,
        email=email,
        name=name,
        username=username,
        source=source,
        followup_count=followup_count,
        agent_active=agent_active,
        followup_active=followup_active,
        agent_reactivate_at=agent_reactivate_at,
        metadata=metadata,
    )


def _secao(texto: str, titulo: str) -> str:
    """Extrai o corpo de uma seção `## {titulo}` até o próximo `## ` (ou o fim)."""
    marcador = f"## {titulo}"
    inicio = texto.index(marcador) + len(marcador)
    resto = texto[inicio:]
    fim = resto.find("\n## ")
    return resto if fim == -1 else resto[:fim]


# --- fundir_grupo -----------------------------------------------------------


def test_par_campo_coalescivel_vence_por_recencia_nao_por_fase():
    """Divergência resolvida conscientemente (ver docstring de `fundir_grupo`):
    o plano descreve "priorizando a fase mais avançada" para os campos
    coalescíveis, mas `shared/leads.py::_fundir` e a etapa 2 da 014 (as
    duas cópias que já rodam em produção/consolidação) usam RECÊNCIA, não
    rank de fase. Este teste amarra o comportamento à recência: a linha
    mais antiga tem fase mais avançada, mas seu `pipedriveid` perde para o
    da linha mais recente.
    """
    antiga = _linha(
        "551187654321",
        last_interaction_at=_t(0),
        phase="qualificado",
        pipedriveid="ANTIGO",
    )
    recente = _linha(
        "5511987654321",
        last_interaction_at=_t(10),
        phase="iniciou_conversa",
        pipedriveid="NOVO",
    )
    fundida = fundir_grupo("551187654321", [antiga, recente])

    assert fundida.pipedriveid == "NOVO"
    assert fundida.origem_por_campo["pipedriveid"] == "5511987654321"
    # phase continua sendo a mais avançada -- essa regra não muda.
    assert fundida.phase == "qualificado"


def test_trio_followup_count_pega_o_maior_nao_o_menor():
    """A escada de follow-up já percorrida não pode ser esquecida."""
    l1 = _linha(
        "p1", last_interaction_at=_t(0), phase="formulario_preenchido", followup_count=1
    )
    l2 = _linha("p2", last_interaction_at=_t(5), phase="qualificado", followup_count=5)
    l3 = _linha(
        "p3", last_interaction_at=_t(10), phase="iniciou_conversa", followup_count=2
    )

    fundida = fundir_grupo("canonico", [l1, l2, l3])

    assert fundida.followup_count == 5
    assert fundida.followup_count != min(1, 5, 2)
    assert fundida.origem_por_campo["followup_count"] == "p2"
    assert fundida.phase == "qualificado"
    assert set(fundida.telefones_origem) == {"p1", "p2", "p3"}


def test_grupo_com_um_lado_pausado_false_vence_mesmo_sendo_mais_antigo():
    """`False` vence sempre -- errar pra não mandar mensagem é recuperável."""
    antiga_pausada = _linha(
        "p1",
        last_interaction_at=_t(0),
        agent_active=False,
        followup_active=False,
    )
    recente_ativa = _linha(
        "p2",
        last_interaction_at=_t(100),
        agent_active=True,
        followup_active=True,
    )

    fundida = fundir_grupo("canonico", [antiga_pausada, recente_ativa])

    assert fundida.agent_active is False
    assert fundida.followup_active is False
    assert fundida.origem_por_campo["agent_active"] == "p1"


def test_agent_reactivate_at_da_linha_perdedora_nao_ressuscita():
    """`agent_reactivate_at` está FORA do coalesce -- acompanha só quem ganhou
    `agent_active`. Mesmo a linha perdedora (ativa, mais recente) tendo uma
    data de reativação, ela não pode vazar pro resultado: o vencedor é a
    linha pausada, e ela não tem reativação agendada (`None`).
    """
    antiga_pausada = _linha(
        "p1",
        last_interaction_at=_t(0),
        agent_active=False,
        followup_active=False,
        agent_reactivate_at=None,
    )
    recente_ativa_com_reativacao = _linha(
        "p2",
        last_interaction_at=_t(100),
        agent_active=True,
        followup_active=True,
        agent_reactivate_at=_t(200),
    )

    fundida = fundir_grupo("canonico", [antiga_pausada, recente_ativa_com_reativacao])

    assert fundida.agent_reactivate_at is None


def test_agent_reactivate_at_do_vencedor_nao_e_sobrescrito_pelo_perdedor():
    """O inverso do teste acima: o vencedor (pausado) TEM reativação agendada
    -- o coalesce não pode substituí-la pelo valor (diferente) da linha
    perdedora, mesmo que a perdedora seja mais recente.
    """
    antiga_pausada_com_reativacao = _linha(
        "p1",
        last_interaction_at=_t(0),
        agent_active=False,
        followup_active=False,
        agent_reactivate_at=_t(300),
    )
    recente_ativa = _linha(
        "p2",
        last_interaction_at=_t(100),
        agent_active=True,
        followup_active=True,
        agent_reactivate_at=_t(5),
    )

    fundida = fundir_grupo("canonico", [antiga_pausada_com_reativacao, recente_ativa])

    assert fundida.agent_reactivate_at == _t(300)
    assert fundida.origem_por_campo["agent_reactivate_at"] == "p1"


def test_fase_mais_avancada_na_linha_mais_antiga_nao_e_enterrada():
    """`agendou_sessao` vence `perdido` mesmo `perdido` sendo a linha mais
    recente -- reunião marcada é fato verificável, perdido é julgamento.
    """
    antigo_agendou = _linha("p1", last_interaction_at=_t(0), phase="agendou_sessao")
    recente_perdido = _linha("p2", last_interaction_at=_t(500), phase="perdido")

    fundida = fundir_grupo("canonico", [antigo_agendou, recente_perdido])

    assert fundida.phase == "agendou_sessao"
    assert fundida.origem_por_campo["phase"] == "p1"
    assert fundida.mudou_phase is True


def test_fase_vencedora_e_por_rank_nao_pela_linha_mais_recente():
    """Guarda de mutação: se `_fase_vencedora` fosse trocada por "pega a
    fase da linha mais recente", este teste cai -- a linha mais recente
    (`perdido`, rank 4) perderia pra `agendou_sessao` (rank 5), que é a
    mais antiga do grupo.
    """
    antiga = _linha("p1", last_interaction_at=_t(0), phase="agendou_sessao")
    meio = _linha("p2", last_interaction_at=_t(10), phase="qualificado")
    nova = _linha("p3", last_interaction_at=_t(20), phase="perdido")

    fundida = fundir_grupo("canonico", [antiga, meio, nova])

    assert fundida.phase == "agendou_sessao"


def test_empate_de_rank_desqualificado_perdido_desempata_por_recencia():
    l1 = _linha("p1", last_interaction_at=_t(0), phase="desqualificado")
    l2 = _linha("p2", last_interaction_at=_t(10), phase="perdido")

    fundida = fundir_grupo("canonico", [l1, l2])

    assert fundida.phase == "perdido"


def test_created_at_minimo_e_last_interaction_at_maximo_com_procedencia():
    l1 = _linha("p1", created_at=_t(-100), last_interaction_at=_t(0))
    l2 = _linha("p2", created_at=_t(-50), last_interaction_at=_t(30))

    fundida = fundir_grupo("canonico", [l1, l2])

    assert fundida.created_at == _t(-100)
    assert fundida.origem_por_campo["created_at"] == "p1"
    assert fundida.last_interaction_at == _t(30)
    assert fundida.origem_por_campo["last_interaction_at"] == "p2"


def test_metadata_mescla_chave_a_chave_e_registra_linhas_fundidas():
    l1 = _linha("p1", last_interaction_at=_t(0), metadata={"a": 1, "b": 1})
    l2 = _linha("p2", last_interaction_at=_t(10), metadata={"b": 2, "c": 3})

    fundida = fundir_grupo("canonico", [l1, l2])

    assert fundida.metadata["a"] == 1
    assert fundida.metadata["b"] == 2  # mais recente vence em conflito de chave
    assert fundida.metadata["c"] == 3
    assert fundida.metadata["linhas_fundidas"] == ["p1", "p2"]


def test_grupo_vazio_levanta_erro():
    with pytest.raises(ValueError):
        fundir_grupo("canonico", [])


def test_singleton_passa_intacto_e_nao_marca_mudanca():
    linha = _linha(
        "551187654321", last_interaction_at=_t(0), phase="qualificado", pipedriveid="X"
    )

    fundida = fundir_grupo("551187654321", [linha])

    assert fundida.phase == "qualificado"
    assert fundida.pipedriveid == "X"
    assert fundida.telefones_origem == ("551187654321",)
    assert fundida.mudou_phase is False
    assert fundida.mudou_agent_active is False


# --- agrupar_por_canonico / fundir_todos ------------------------------------


def test_agrupar_por_canonico_junta_duplicatas_e_descarta_o_resto():
    linhas = [
        _linha("+5511987654321", last_interaction_at=_t(0)),
        _linha("11987654321", last_interaction_at=_t(10)),
        _linha("5521987654321", last_interaction_at=_t(0)),
        _linha("null"),
        _linha("+14242123771"),
    ]

    grupos, descartes = agrupar_por_canonico(linhas)

    assert set(grupos.keys()) == {"551187654321", "552187654321"}
    assert len(grupos["551187654321"]) == 2
    assert len(grupos["552187654321"]) == 1
    assert len(descartes) == 2
    assert {d.motivo for d in descartes} == {
        "telefone_ausente",
        "colide_com_forma_local_br",
    }
    assert all(isinstance(d, Descarte) for d in descartes)


def test_fundir_todos_aplica_fundir_grupo_em_cada_grupo():
    grupos = {
        "551187654321": [_linha("551187654321", last_interaction_at=_t(0))],
        "552187654321": [
            _linha("552187654321", last_interaction_at=_t(0), pipedriveid="A"),
            _linha("5521987654321", last_interaction_at=_t(10), pipedriveid="B"),
        ],
    }

    fundidas = fundir_todos(grupos)

    assert set(fundidas.keys()) == {"551187654321", "552187654321"}
    assert all(isinstance(f, LinhaFundida) for f in fundidas.values())
    assert fundidas["552187654321"].pipedriveid == "B"


# --- gerar_relatorio ---------------------------------------------------------


def _cenario_relatorio():
    """Cenário completo: dois grupos fundidos (um que muda estado, um que não
    muda nada), um lead estrangeiro migrado, um lead comum e dois descartes
    (um deles o clássico "colide com forma local BR"). Passa pelo pipeline
    real (`agrupar_por_canonico` -> `fundir_todos`), não fabrica
    `LinhaFundida` à mão -- assim o teste do relatório também exercita a
    integração entre as duas etapas.
    """
    linhas = [
        # grupo fundido comum -- não muda phase nem agent_active
        _linha(
            "551187654321", last_interaction_at=_t(0), phase="formulario_preenchido"
        ),
        _linha(
            "5511987654321", last_interaction_at=_t(10), phase="formulario_preenchido"
        ),
        # grupo fundido cuja fusão muda phase E agent_active
        _linha(
            "552187654321",
            last_interaction_at=_t(0),
            phase="agendou_sessao",
            agent_active=False,
            followup_active=False,
        ),
        _linha(
            "5521987654321",
            last_interaction_at=_t(50),
            phase="perdido",
            agent_active=True,
            followup_active=True,
        ),
        # lead estrangeiro, singleton, qualificado e ativo (perfil do caso
        # medido de Moçambique -- mas sem hardcode do dígito exato)
        _linha("258864038352", last_interaction_at=_t(0), phase="qualificado"),
        # lead comum, singleton
        _linha("552199988776", last_interaction_at=_t(0), phase="iniciou_conversa"),
        # descartes
        _linha("null"),
        _linha("+14242123771"),
    ]
    grupos, descartes = agrupar_por_canonico(linhas)
    fundidas = fundir_todos(grupos)
    return len(linhas), grupos, fundidas, descartes


def test_relatorio_soma_fecha_e_resumo_bate():
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    texto = gerar_relatorio(total_origem, grupos, fundidas, descartes)

    assert "Total na origem: **8**" in texto
    assert "Migrados (linhas de origem com telefone canônico): **6**" in texto
    assert "Descartados: **2**" in texto
    assert "6 + 2 = 8 == 8 (fecha)" in texto
    assert "Leads finais após fusão: **4**" in texto
    assert "2 grupo(s) com mais de uma linha física" in texto


def test_relatorio_levanta_erro_quando_soma_nao_fecha():
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    with pytest.raises(ValueError):
        gerar_relatorio(total_origem + 1, grupos, fundidas, descartes)


def test_relatorio_nao_omite_nenhum_grupo_fundido():
    """Guarda de mutação: cada canônico com mais de uma linha física precisa
    ter sua própria seção `### {canonico}` -- se o loop de geração pular um
    grupo, este teste cai.
    """
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    texto = gerar_relatorio(total_origem, grupos, fundidas, descartes)
    secao_grupos = _secao(texto, "Grupos fundidos")

    grupos_multiplos = {c for c, linhas in grupos.items() if len(linhas) > 1}
    assert grupos_multiplos == {"551187654321", "552187654321"}
    for canonico in grupos_multiplos:
        assert f"### {canonico}" in secao_grupos


def test_relatorio_destaca_lead_estrangeiro_migrado():
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    texto = gerar_relatorio(total_origem, grupos, fundidas, descartes)
    secao_destaques = _secao(texto, "Decisão humana necessária")

    assert "258864038352" in secao_destaques
    assert "estrangeiro" in secao_destaques.lower()


def test_relatorio_destaca_descarte_que_colide_com_forma_local_br():
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    texto = gerar_relatorio(total_origem, grupos, fundidas, descartes)
    secao_destaques = _secao(texto, "Decisão humana necessária")

    assert "+14242123771" in secao_destaques
    assert "colidir com forma local br" in secao_destaques.lower()


def test_relatorio_destaca_grupo_cuja_fusao_mudou_phase_e_agent_active():
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    texto = gerar_relatorio(total_origem, grupos, fundidas, descartes)
    secao_destaques = _secao(texto, "Decisão humana necessária")

    assert "552187654321" in secao_destaques
    assert "phase" in secao_destaques
    assert "agent_active" in secao_destaques


def test_relatorio_nao_destaca_grupo_que_nao_mudou_nada():
    """Guarda contra falso positivo: o grupo `551187654321` foi fundido mas
    não mudou `phase` nem `agent_active` -- não pode aparecer na seção de
    decisão humana, só na de grupos fundidos.
    """
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    texto = gerar_relatorio(total_origem, grupos, fundidas, descartes)
    secao_destaques = _secao(texto, "Decisão humana necessária")
    secao_grupos = _secao(texto, "Grupos fundidos")

    assert "551187654321" not in secao_destaques
    assert "### 551187654321" in secao_grupos


def test_relatorio_lista_todos_os_descartes_com_motivo():
    total_origem, grupos, fundidas, descartes = _cenario_relatorio()

    texto = gerar_relatorio(total_origem, grupos, fundidas, descartes)
    secao_descartes = _secao(texto, "Descartes")

    assert "telefone_ausente" in secao_descartes
    assert "colide_com_forma_local_br" in secao_descartes
    assert "+14242123771" in secao_descartes
