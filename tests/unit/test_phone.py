"""Testes do módulo de telefone — puros, sem I/O."""

import pytest

from whatsapp_langchain.shared.phone import (
    canonicalizar,
    from_e164,
    resolver_telefone,
    to_e164,
    variacoes,
)


@pytest.mark.parametrize(
    "bruto,esperado",
    [
        ("+5511987654321", "551187654321"),  # E.164 com + e com 9
        ("5511987654321", "551187654321"),  # dígitos com 9
        ("551187654321", "551187654321"),  # já canônico
        ("11987654321", "551187654321"),  # sem DDI
        ("1187654321", "551187654321"),  # sem DDI e sem 9
        ("(11) 98765-4321", "551187654321"),  # formatado
        ("5511987654321@s.whatsapp.net", "551187654321"),
    ],
)
def test_canonicaliza_numeros_brasileiros(bruto, esperado):
    assert canonicalizar(bruto) == esperado


def test_numero_estrangeiro_nao_perde_digito():
    # +1 415 555 0123 — o "9" brasileiro não pode ser aplicado aqui.
    assert canonicalizar("+14155550123") == "14155550123"


@pytest.mark.parametrize("bruto", [None, "", "abc", "123", "0" * 20])
def test_entrada_invalida_devolve_none(bruto):
    assert canonicalizar(bruto) is None


@pytest.mark.parametrize(
    "bruto",
    [
        "551187654321:3@s.whatsapp.net",  # aparelho pareado
        "551187654321:12",  # device de dois dígitos, sem servidor
        "55011987654321@s.whatsapp.net",  # 0 de tronco depois do DDI
        "011987654321@s.whatsapp.net",  # 0 de tronco sem DDI
        "5511987654321:5@s.whatsapp.net",  # device + 9º dígito
    ],
    ids=["device", "device2", "tronco_com_ddi", "tronco_sem_ddi", "device_com_9"],
)
def test_formas_malformadas_convergem_para_a_mesma_identidade(bruto):
    """Device pareado e 0 de tronco não podem virar leads fantasmas.

    Cada uma dessas formas produzia uma identidade canônica própria — quatro
    linhas em leads_crm para a mesma pessoa, e outbound para um número que
    não existe no caso do `:device`.
    """
    assert canonicalizar(bruto) == "551187654321"


def test_ddi_55_irreconhecivel_e_recusado():
    """15 dígitos com DDI 55 não é telefone brasileiro nenhum.

    Antes virava chave canônica e criava lead que o outbound nunca alcança.
    """
    assert canonicalizar("551187654321999") is None


@pytest.mark.parametrize(
    "bruto",
    [
        "274988654321@lid",  # LID que não parece telefone brasileiro
        "551188654321@lid",  # LID que casa com BR_SEM_9 — o caso que corrompe
        "274988654321:2@lid",  # LID com sufixo de aparelho
        "551188654321@LID",  # caixa alta
    ],
    ids=["lid", "lid_parece_br", "lid_com_device", "lid_caixa_alta"],
)
def test_lid_nao_vira_telefone(bruto):
    """`@lid` é identificador opaco, não número — aceitá-lo funde duas pessoas.

    Um LID de 12 dígitos começando em 55 casa com `BR_SEM_9` e vira
    exatamente a chave primária de um lead real.
    """
    assert canonicalizar(bruto) is None


def test_sufixo_de_aparelho_nao_engole_numero_com_prefixo():
    """`:\\d+$` sem âncora à esquerda comia o número inteiro do `From` do Twilio."""
    assert canonicalizar("whatsapp:5511987654321") == "551187654321"
    assert canonicalizar("whatsapp:+5511987654321") == "551187654321"


def test_tronco_que_nao_fecha_com_forma_brasileira_e_recusado():
    """O 0 declara discagem nacional, mas 11 dígitos sem o 9 não existem no BR.

    Antes o `lstrip("0")` devolvia `11187654321` — número que o outbound
    nunca alcança, virando chave primária em leads_crm.
    """
    assert canonicalizar("011187654321") is None
    assert canonicalizar("55011187654321") is None


def test_estrangeiro_continua_passando_por_digitos():
    """A recusa é só para o que se declara brasileiro e não fecha com nenhuma forma."""
    assert canonicalizar("14155550123") == "14155550123"
    assert canonicalizar("447911123456") == "447911123456"


def test_variacoes_brasileiras():
    assert variacoes("551187654321") == ("5511987654321", "551187654321")


def test_variacoes_estrangeiro_sao_iguais():
    assert variacoes("14155550123") == ("14155550123", "14155550123")


def test_resolve_jid_com_sufixo_whatsapp():
    key = {"remoteJid": "5511987654321@s.whatsapp.net", "fromMe": False}
    assert resolver_telefone(key) == "551187654321"


def test_resolve_jid_sem_sufixo():
    assert resolver_telefone({"remoteJid": "5511987654321"}) == "551187654321"


def test_resolver_ignora_grupo():
    assert resolver_telefone({"remoteJid": "1234-5678@g.us"}) is None


def test_resolver_sem_candidato_valido():
    assert resolver_telefone({"remoteJid": ""}) is None
    assert resolver_telefone({}) is None


def test_resolver_ignora_remote_jid_alt():
    """A integração WHATSAPP-BUSINESS não popula remoteJidAlt (50/50 ausente).

    Se um dia aparecer, não pode influenciar o resultado sem decisão explícita.
    """
    key = {"remoteJid": "5511987654321@s.whatsapp.net", "remoteJidAlt": "5599999999999"}
    assert resolver_telefone(key) == "551187654321"


@pytest.mark.parametrize(
    "remote_jid",
    [
        "551187654321:3@s.whatsapp.net",
        "55011987654321@s.whatsapp.net",
        "011987654321@s.whatsapp.net",
    ],
    ids=["device", "tronco_com_ddi", "tronco_sem_ddi"],
)
def test_resolver_converge_as_formas_malformadas(remote_jid):
    assert resolver_telefone({"remoteJid": remote_jid}) == "551187654321"


@pytest.mark.parametrize(
    "remote_jid",
    [
        "274988654321@lid",
        "551188654321@lid",
        "274988654321:2@lid",
    ],
    ids=["lid", "lid_parece_br", "lid_com_device"],
)
def test_resolver_recusa_lid(remote_jid):
    """O LID está em rollout no WhatsApp e não é telefone — vai para descarte."""
    assert resolver_telefone({"remoteJid": remote_jid}) is None


def test_resolver_recusa_jid_irreconhecivel():
    assert resolver_telefone({"remoteJid": "55118765432199@s.whatsapp.net"}) is None


def test_conversao_e164_ida_e_volta():
    assert to_e164("551187654321") == "+551187654321"
    assert from_e164("+551187654321") == "551187654321"
    assert from_e164("551187654321") == "551187654321"
