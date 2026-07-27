"""Converte a resposta final do agente em balões de WhatsApp.

O n8n usa um output parser estruturado sobre o TEXTO FINAL, depois que o ciclo
de tools terminou — não `response_format` nativo. Replicamos o mesmo mecanismo:
estruturar a resposta e chamar ferramentas no mesmo turno faz o modelo devolver
o JSON em vez de chamar a tool, ou quebrar o schema quando há tool call pendente.

Nunca devolve lista vazia: perder a resposta é pior que perder a formatação.

Qualquer desvio de schema — JSON inválido, `messages` que não é lista, item
que não é string, lista vazia depois de descartar espaço em branco — cai para
o TEXTO BRUTO inteiro, nunca para uma lista mutilada com só parte dos itens.
O n8n tinha retry no parser estruturado quando o schema não batia; aqui não
tem, então a escolha "menos pior" é preservar a resposta inteira (ainda que
crua) em vez de arriscar entregar metade dela ao lead sem ninguém perceber.
Por isso todo fallback loga um warning com uma prévia do texto — o fallback é
degradação visível para o lead (ele recebe o JSON cru), e precisa ser visível
no log também, não descoberto por reclamação do cliente.
"""

import json
import re
from typing import Any

import structlog

from whatsapp_langchain.shared.config import settings

logger = structlog.get_logger()

# A cerca só conta se envolver o texto INTEIRO (do começo ao fim, depois do
# strip) — ancorada com ^...$ e re.S para o `.` cruzar linhas. Sem âncora, um
# balão que contém ```código``` no meio do próprio conteúdo (ex.: a Renata
# ensinando alguém a formatar texto) seria capturado como se fosse a cerca
# externa, quebrando o parse de um JSON que era válido. re.I porque modelos
# variam entre ```json e ```JSON.
CERCA = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S | re.I)

# Tamanho da prévia de texto nos logs de fallback — o suficiente para
# diagnosticar sem inflar o log com a resposta inteira.
PREVIEW_LEN = 200


def _preview(bruto: str) -> str:
    return bruto[:PREVIEW_LEN]


def _texto_de_conteudo(conteudo: Any) -> str:
    """Normaliza `AIMessage.content` (str ou list[str | dict]) para string.

    `BaseMessage.content` no langchain_core é tipado como `str | list[str |
    dict]` — alguns modelos/providers devolvem content blocks (lista de
    dicts com chave "text", formato multimodal) em vez de string simples.
    Chamar `.strip()` direto nisso quebraria com AttributeError antes de
    qualquer parsing.
    """
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        partes = []
        for item in conteudo:
            if isinstance(item, str):
                partes.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                partes.append(item["text"])
        return "\n".join(partes)
    return "" if conteudo is None else str(conteudo)


def extrair_baloes(texto: Any) -> list[str]:
    bruto = _texto_de_conteudo(texto).strip()
    if not bruto:
        return [""]

    candidato = bruto
    if cerca := CERCA.match(bruto):
        candidato = cerca.group(1).strip()

    try:
        dados = json.loads(candidato)
    except (ValueError, TypeError):
        logger.warning("extrair_baloes_json_invalido", preview=_preview(bruto))
        return [bruto]

    if not isinstance(dados, dict):
        logger.warning(
            "extrair_baloes_nao_e_objeto",
            tipo=type(dados).__name__,
            preview=_preview(bruto),
        )
        return [bruto]

    mensagens = dados.get("messages")
    if not isinstance(mensagens, list):
        logger.warning(
            "extrair_baloes_sem_lista_messages",
            tipo=type(mensagens).__name__,
            preview=_preview(bruto),
        )
        return [bruto]

    # Qualquer item que não seja string invalida a lista INTEIRA — não
    # descartamos item a item. Descartar item a item (comportamento antigo)
    # devolvia uma lista que parecia completa mas podia estar faltando um
    # balão de verdade (ex.: um item que virou lista aninhada por engano do
    # modelo), sem log nenhum: perda parcial silenciosa.
    if not all(isinstance(m, str) for m in mensagens):
        logger.warning(
            "extrair_baloes_item_nao_string",
            tipos=[type(m).__name__ for m in mensagens],
            preview=_preview(bruto),
        )
        return [bruto]

    baloes = [m.strip() for m in mensagens if m.strip()]
    if not baloes:
        logger.warning("extrair_baloes_todos_vazios", preview=_preview(bruto))
        return [bruto]

    teto = settings.balao_max_count
    if teto > 0 and len(baloes) > teto:
        logger.warning(
            "extrair_baloes_teto_excedido",
            total=len(baloes),
            teto=teto,
        )
        resto = "\n\n".join(baloes[teto - 1 :])
        baloes = baloes[: teto - 1] + [resto]

    return baloes
