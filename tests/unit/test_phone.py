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


def test_conversao_e164_ida_e_volta():
    assert to_e164("551187654321") == "+551187654321"
    assert from_e164("+551187654321") == "551187654321"
    assert from_e164("551187654321") == "551187654321"
