"""`ALLOWLIST_PHONES` — trava de janela de teste.

A blocklist responde "quem NÃO pode receber". A allowlist responde a
pergunta oposta e muito mais restritiva: "quem é a ÚNICA pessoa que pode".
Existe para uma janela de teste em produção, onde a Renata já está ligada
no número comercial de verdade mas só pode falar com o telefone do teste.

Vazia = desligada. É esse o default, e precisa ser: uma allowlist que
liga sozinha silenciaria o sistema inteiro.
"""

import pytest

from whatsapp_langchain.shared.config import Settings


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_vazia_por_padrao_significa_desligada():
    s = _s()
    assert s.allowlist_phones == []
    assert s.allowlist_ativa is False


@pytest.mark.parametrize(
    "bruto",
    [
        "5581991013614",
        " 5581991013614 ",
        "+55 81 99101-3614",
        "5581991013614@s.whatsapp.net",
    ],
)
def test_normaliza_as_formas_que_um_humano_digita(bruto):
    """O valor é digitado à mão no painel do Railway sob pressão, no meio de
    um cutover. `+`, espaço, hífen e até um JID colado do WhatsApp precisam
    convergir para a mesma chave — senão a allowlist fica ligada e vazia de
    quem deveria estar nela, e o sistema inteiro emudece."""
    s = _s(allowlist_phones=bruto)
    assert s.allowlist_ativa is True
    assert s.permite("5581991013614") is True


def test_aceita_lista_separada_por_virgula():
    s = _s(allowlist_phones="5581991013614, 5511988887777")
    assert len(s.allowlist_phones) == 2
    assert s.permite("5581991013614") is True
    assert s.permite("5511988887777") is True


def test_permite_as_duas_formas_do_mesmo_numero():
    """Cadastrado com o 9º dígito, precisa permitir a forma sem — e
    vice-versa. É a mesma dualidade que o gate e a blocklist já tratam:
    gravar uma forma e consultar a outra deixa o número de fora."""
    com_9 = _s(allowlist_phones="5581991013614")
    assert com_9.permite("558191013614") is True

    sem_9 = _s(allowlist_phones="558191013614")
    assert sem_9.permite("5581991013614") is True


def test_quem_nao_esta_na_lista_nao_passa():
    s = _s(allowlist_phones="5581991013614")
    assert s.permite("5511999998888") is False


def test_desligada_permite_todo_mundo():
    """Sem allowlist configurada, `permite` não pode virar um `False` geral
    — seria o sistema inteiro mudo por omissão de config."""
    s = _s()
    assert s.permite("5511999998888") is True
    assert s.permite("5581991013614") is True


def test_entrada_invalida_nao_liga_a_allowlist_pela_metade():
    """Lixo que não canonicaliza é descartado, mas se sobrar ao menos um
    número válido a trava continua valendo para ele. O modo de falha a
    evitar é o oposto: uma linha inválida derrubar o boot no meio do
    cutover."""
    s = _s(allowlist_phones="5581991013614, nao-e-telefone, ")
    assert s.allowlist_phones == ["558191013614"]
    assert s.permite("5581991013614") is True
    assert s.permite("5511999998888") is False


def test_so_lixo_deixa_a_allowlist_desligada():
    s = _s(allowlist_phones="nao-e-telefone")
    assert s.allowlist_ativa is False
    assert s.permite("5511999998888") is True
