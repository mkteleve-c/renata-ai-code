"""Parser da saída em balões da Renata."""

from whatsapp_langchain.agents.catalog.elevec_sdr.saida import extrair_baloes


def test_json_valido_vira_lista():
    assert extrair_baloes('{"messages": ["oi", "tudo bem?"]}') == ["oi", "tudo bem?"]


def test_json_dentro_de_cerca_markdown():
    bruto = '```json\n{"messages": ["oi"]}\n```'
    assert extrair_baloes(bruto) == ["oi"]


def test_texto_solto_vira_balao_unico():
    assert extrair_baloes("desculpa, tive um problema") == [
        "desculpa, tive um problema"
    ]


def test_json_sem_a_chave_messages_vira_balao_unico():
    bruto = '{"resposta": "oi"}'
    assert extrair_baloes(bruto) == [bruto]


def test_lista_vazia_nao_devolve_nada_vazio():
    assert extrair_baloes('{"messages": []}') == ['{"messages": []}']


def test_itens_nao_string_sao_descartados():
    assert extrair_baloes('{"messages": ["oi", 42, null, "tchau"]}') == ["oi", "tchau"]


def test_espaco_em_branco_nao_vira_balao():
    assert extrair_baloes('{"messages": ["oi", "   ", "tchau"]}') == ["oi", "tchau"]
