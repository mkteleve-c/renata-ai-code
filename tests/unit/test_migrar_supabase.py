"""Testes de `normalizar_telefone` -- puro, sem I/O, sem rede.

`test_saida_sempre_satisfaz_o_check_do_banco` é o mais importante do
arquivo: amarra a saída do script ao CHECK real de `leads_crm.phone`,
extraído do texto das migrações 007 e 014 -- nunca copiado à mão -- para
que este teste acompanhe se a constraint mudar de novo.
"""

import re
from pathlib import Path

import pytest

from scripts.migrar_supabase import Normalizado, normalizar_telefone

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
