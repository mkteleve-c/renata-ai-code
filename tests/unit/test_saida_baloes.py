"""Parser da saída em balões da Renata.

Fix round 1/5 (revisão): o parser original descartava itens não-string da
lista `messages` silenciosamente — bug crítico de perda parcial (o lead
recebe metade da resposta sem log nenhum). A partir daqui, qualquer desvio
de schema (item não-string, `messages` que não é lista, JSON inválido, etc.)
cai inteiro para o texto bruto — nunca uma resposta mutilada — e loga um
warning com o motivo, porque o fallback manda JSON cru pro WhatsApp do lead
e isso precisa ser visível no log, não descoberto por reclamação do cliente.
"""

from structlog.testing import capture_logs

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


def test_espaco_em_branco_nao_vira_balao():
    assert extrair_baloes('{"messages": ["oi", "   ", "tchau"]}') == ["oi", "tchau"]


# --- Fix round 1: perda parcial silenciosa (Crítico) ---


def test_item_nao_string_no_meio_cai_pro_bruto():
    """Substitui test_itens_nao_string_sao_descartados.

    Antes: descartava 42 e null silenciosamente e devolvia ["oi", "tchau"] —
    parece uma resposta completa, mas não é: se o desvio de schema cortar um
    balão de verdade (não só lixo tipo 42/null), o lead recebe metade da
    conversa sem ninguém saber. Correção: QUALQUER item não-string invalida
    a lista inteira — cai para o texto bruto, não para uma lista mutilada.
    """
    bruto = '{"messages": ["oi", 42, null, "tchau"]}'
    with capture_logs() as logs:
        assert extrair_baloes(bruto) == [bruto]
    assert any(log["event"] == "extrair_baloes_item_nao_string" for log in logs)


def test_item_lista_aninhada_cai_pro_bruto():
    """Caso do revisor: [["oi"], "tchau"] virava ['tchau'] — metade some."""
    bruto = '{"messages": [["oi"], "tchau"]}'
    assert extrair_baloes(bruto) == [bruto]


# --- Fix round 1: observabilidade no fallback (Importante 1) ---


def test_fallback_json_invalido_loga_warning():
    """Aspas curvas (typographic quotes) quebram json.loads — fallback deve logar."""
    bruto = "{“messages”: [“oi”]}"
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)
    assert resultado == [bruto]
    assert any(log["event"] == "extrair_baloes_json_invalido" for log in logs)


def test_messages_como_string_cai_pro_bruto_e_loga():
    bruto = '{"messages": "oi"}'
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)
    assert resultado == [bruto]
    assert any(log["event"] == "extrair_baloes_sem_lista_messages" for log in logs)


def test_json_dentro_de_prosa_cai_pro_bruto_e_loga():
    bruto = 'Aqui está: {"messages": ["oi"]}'
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)
    assert resultado == [bruto]
    assert any(log["event"] == "extrair_baloes_json_invalido" for log in logs)


def test_cerca_nao_fechada_cai_pro_bruto_e_loga():
    bruto = '```json\n{"messages": ["oi"]}'
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)
    assert resultado == [bruto]
    assert any(log["event"] == "extrair_baloes_json_invalido" for log in logs)


def test_lista_no_topo_cai_pro_bruto_e_loga():
    bruto = '["oi", "tchau"]'
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)
    assert resultado == [bruto]
    assert any(log["event"] == "extrair_baloes_nao_e_objeto" for log in logs)


def test_output_aninhado_sem_messages_cai_pro_bruto_e_loga():
    bruto = '{"output": {"messages": ["oi"]}}'
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)
    assert resultado == [bruto]
    assert any(log["event"] == "extrair_baloes_sem_lista_messages" for log in logs)


def test_todos_em_branco_cai_pro_bruto_e_loga():
    """Distinto de messages: [] — aqui a lista tem itens, mas todos viram
    string vazia depois do strip. Também é fallback, também precisa logar."""
    bruto = '{"messages": ["   ", "\\n"]}'
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)
    assert resultado == [bruto]
    assert any(log["event"] == "extrair_baloes_todos_vazios" for log in logs)


# --- Fix round 1: cerca de markdown (Menores) ---


def test_cerca_json_maiusculo_e_reconhecida():
    bruto = '```JSON\n{"messages": ["oi"]}\n```'
    assert extrair_baloes(bruto) == ["oi"]


def test_cerca_dentro_do_conteudo_nao_quebra_parse():
    """```código``` dentro de um balão não pode ser confundido com a cerca
    externa — a cerca só conta se envolver o texto INTEIRO."""
    bruto = '{"messages": ["use ```codigo``` assim", "ok?"]}'
    assert extrair_baloes(bruto) == ["use ```codigo``` assim", "ok?"]


# --- Fix round 1: teto de balões (Importante 2) ---


def test_teto_de_baloes_concatena_o_resto():
    itens = [f"balao {i}" for i in range(15)]
    bruto = '{"messages": ' + str(itens).replace("'", '"') + "}"
    with capture_logs() as logs:
        resultado = extrair_baloes(bruto)

    assert len(resultado) == 10
    assert resultado[:9] == itens[:9]
    assert resultado[9] == "\n\n".join(itens[9:])
    assert any(log["event"] == "extrair_baloes_teto_excedido" for log in logs)


# --- Fix round 1: defensividade de tipo (Menores) ---


def test_texto_como_content_blocks_nao_quebra():
    """BaseMessage.content pode vir list[str | dict] (multimodal); .strip()
    direto nisso quebraria antes de qualquer parsing."""
    conteudo = [{"type": "text", "text": '{"messages": ["oi"]}'}]
    assert extrair_baloes(conteudo) == ["oi"]


# --- Fix round 2: a cerca ancorada regrediu o caso mais comum (Importante) ---
#
# Ancorar a cerca (^...$) resolveu o balão com ``` embutido, mas quebrou
# preâmbulo/epílogo ao redor da cerca — que é bem mais frequente (LLM
# escrevendo "Aqui está:" antes ou "Espero ter ajudado!" depois do bloco
# ```json). Correção: tenta a cerca ancorada primeiro (mais segura), depois
# o texto bruto direto (cobre o ``` embutido, que já é JSON válido sem
# stripping), e só por último a cerca livre/não-ancorada (cobre
# preâmbulo/epílogo, com o risco de falso positivo que só vale correr por
# último). Os três casos abaixo precisam funcionar ao mesmo tempo.


def test_preambulo_antes_da_cerca_e_ignorado():
    bruto = 'Aqui está:\n```json\n{"messages": ["oi"]}\n```'
    assert extrair_baloes(bruto) == ["oi"]


def test_epilogo_depois_da_cerca_e_ignorado():
    bruto = '```json\n{"messages": ["oi"]}\n```\nEspero ter ajudado!'
    assert extrair_baloes(bruto) == ["oi"]


def test_preambulo_e_epilogo_ao_redor_da_cerca():
    bruto = 'Aqui está:\n```json\n{"messages": ["oi"]}\n```\nEspero ter ajudado!'
    assert extrair_baloes(bruto) == ["oi"]


def test_cerca_embutida_continua_funcionando_com_a_cerca_livre():
    """Não pode regredir: o caso que motivou a âncora continua correto agora
    que a cerca livre é só a terceira tentativa, não a única."""
    bruto = '{"messages": ["use ```codigo``` assim", "ok?"]}'
    assert extrair_baloes(bruto) == ["use ```codigo``` assim", "ok?"]


# --- Fix round 2: buraco 1 — descarte silencioso em _texto_de_conteudo ---


def test_content_block_nao_string_e_descartado_com_log():
    """Item de content block que não é str nem dict com "text" era
    descartado em silêncio — mesmo padrão do Crítico, um nível acima."""
    conteudo = [{"type": "text", "text": '{"messages": ["oi"]}'}, {"type": "image"}]
    with capture_logs() as logs:
        resultado = extrair_baloes(conteudo)
    assert resultado == ["oi"]
    assert any(
        log["event"] == "extrair_baloes_content_block_descartado" for log in logs
    )


# --- Fix round 2: buraco 2 — texto vazio sem log e sem fallback visível ---


def test_texto_vazio_loga_e_nao_manda_corpo_vazio():
    """[""] vira send_message(body=""), que provedores rejeitam — queimando
    3 tentativas de retry à toa (a causa é determinística, não passageira).
    Devolve um texto de fallback visível em vez de string vazia."""
    with capture_logs() as logs:
        resultado = extrair_baloes("")
    assert resultado != [""]
    assert resultado[0]
    assert any(log["event"] == "extrair_baloes_texto_vazio" for log in logs)
