"""Quando o modelo erra o schema, o lead não pode ver JSON.

`extrair_baloes` espera `{"messages": ["...", "..."]}`. Todo caminho de
falha caía em `return [bruto]` — o texto cru — o que significa que um slip
de schema plausível vira `{"messages": "Oi! Tudo bem?"}` na tela de uma
pessoa de verdade.

O texto está lá; só está numa forma vizinha da esperada. Estes testes
fixam que a forma vizinha é resgatada, e que o dump cru continua sendo o
último recurso — para quando realmente não houver texto a recuperar.
"""

import pytest

from whatsapp_langchain.agents.catalog.elevec_sdr.saida import extrair_baloes


@pytest.mark.parametrize(
    ("bruto", "esperado", "descricao"),
    [
        (
            '{"messages": "Oi! Tudo bem?"}',
            ["Oi! Tudo bem?"],
            "messages como string em vez de lista",
        ),
        (
            '{"messages": ["Oi!", "Tudo bem?"]}',
            ["Oi!", "Tudo bem?"],
            "o formato correto continua funcionando",
        ),
        (
            '["Oi!", "Tudo bem?"]',
            ["Oi!", "Tudo bem?"],
            "array direto, sem o envelope",
        ),
        (
            '{"message": ["Oi!", "Tudo bem?"]}',
            ["Oi!", "Tudo bem?"],
            "chave no singular",
        ),
        (
            '{"message": "Oi! Tudo bem?"}',
            ["Oi! Tudo bem?"],
            "singular e string",
        ),
        (
            'Claro! {"messages": ["Oi!", "Tudo bem?"]}',
            ["Oi!", "Tudo bem?"],
            "preâmbulo sem cerca de markdown",
        ),
        (
            '{"resposta": "Oi! Tudo bem?"}',
            ["Oi! Tudo bem?"],
            "chave inesperada, valor único e string",
        ),
    ],
)
def test_slip_de_schema_nao_vira_json_na_tela_do_lead(bruto, esperado, descricao):
    baloes = extrair_baloes(bruto)

    assert baloes == esperado, descricao
    for balao in baloes:
        assert "{" not in balao and "[" not in balao, (
            f"{descricao}: JSON vazou para o lead -> {balao!r}"
        )


def test_texto_puro_continua_passando_intacto():
    """O que não é JSON nunca foi problema — e não pode virar um."""
    assert extrair_baloes("Oi! Tudo bem?") == ["Oi! Tudo bem?"]


def test_json_sem_texto_nenhum_cai_no_dump_cru():
    """Último recurso preservado: se não há texto a resgatar, mandar o cru é
    melhor que mandar vazio — pelo menos alguém percebe no log e no chat."""
    baloes = extrair_baloes('{"status": 200, "ok": true}')

    assert baloes == ['{"status": 200, "ok": true}']
