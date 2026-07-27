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

# Cerca ancorada: só conta se envolver o texto INTEIRO (do começo ao fim,
# depois do strip). Tentada primeiro porque é a mais segura — não corre risco
# de casar com um ``` que esteja dentro do conteúdo de um balão (ex.: a
# Renata ensinando alguém a formatar texto com ```código```).
CERCA_ANCORADA = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S | re.I)

# Cerca livre: primeira ocorrência de ``` em qualquer posição do texto — cobre
# preâmbulo/epílogo ao redor da cerca ("Aqui está:\n```json\n{...}\n```" ou
# "```json\n{...}\n```\nEspero ter ajudado!"), que é bem mais comum do que um
# balão com ``` embutido. Só é tentada como ÚLTIMO recurso, depois de tentar
# o texto bruto direto sem stripping nenhum — porque ela pode dar falso
# positivo no caso do balão com ``` embutido, e só vale correr esse risco
# depois de esgotar as opções mais seguras (ver `_candidatos_json`).
CERCA_LIVRE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)

# Tamanho da prévia de texto nos logs de fallback — o suficiente para
# diagnosticar sem inflar o log com a resposta inteira.
PREVIEW_LEN = 200

# Texto vazio nunca deveria acontecer (o agente sempre produz algum
# conteúdo), mas se acontecer, devolver [""] geraria um send_message(body="")
# — a maioria dos provedores de WhatsApp rejeita corpo vazio, e como a causa
# é determinística (não é uma falha de rede passageira), as 3 tentativas de
# retry seriam queimadas à toa no mesmo resultado. Preferimos um texto visível
# ao lead, mesmo genérico, a um envio fadado a falhar 3 vezes.
TEXTO_VAZIO_FALLBACK = "Desculpa, tive um problema para te responder agora."


def _preview(bruto: str) -> str:
    return bruto[:PREVIEW_LEN]


def _texto_de_conteudo(conteudo: Any) -> str:
    """Normaliza `AIMessage.content` (str ou list[str | dict]) para string.

    `BaseMessage.content` no langchain_core é tipado como `str | list[str |
    dict]` — alguns modelos/providers devolvem content blocks (lista de
    dicts com chave "text", formato multimodal) em vez de string simples.
    Chamar `.strip()` direto nisso quebraria com AttributeError antes de
    qualquer parsing.

    Blocos que não são string nem dict com "text" são descartados — mas,
    diferente da versão anterior, o descarte é logado. É o mesmo padrão do
    Crítico corrigido em `extrair_baloes` (item não-string em `messages`),
    um nível acima: perder conteúdo sem log é sempre um bug, mesmo quando o
    conteúdo perdido não é texto.
    """
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        partes = []
        descartados = []
        for item in conteudo:
            if isinstance(item, str):
                partes.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                partes.append(item["text"])
            else:
                descartados.append(type(item).__name__)
        if descartados:
            logger.warning("extrair_baloes_content_block_descartado", tipos=descartados)
        return "\n".join(partes)
    return "" if conteudo is None else str(conteudo)


def _candidatos_json(bruto: str) -> list[str]:
    """Ordem de tentativas de extração do JSON candidato.

    1. Cerca ancorada (o texto INTEIRO está dentro de um bloco ```): caso
       mais estrito, sem risco de falso positivo.
    2. O texto bruto direto, sem stripping de cerca nenhuma: cobre o caso de
       um balão que só PARECE ter cerca porque contém ``` no meio de uma
       string — aqui o texto inteiro já é JSON válido, e tentar isolar uma
       cerca primeiro pegaria o trecho errado.
    3. Cerca livre (primeira ocorrência de ``` em qualquer posição): cobre
       preâmbulo/epílogo ao redor da cerca. Só tentada por último porque,
       ao contrário da ancorada, pode casar com um ``` que esteja dentro do
       valor de um balão — um falso positivo que só vale correr depois de
       esgotar as opções 1 e 2.
    """
    candidatos: list[str] = []
    if ancorada := CERCA_ANCORADA.match(bruto):
        candidatos.append(ancorada.group(1).strip())
    candidatos.append(bruto)
    if livre := CERCA_LIVRE.search(bruto):
        conteudo = livre.group(1).strip()
        if conteudo not in candidatos:
            candidatos.append(conteudo)
    return candidatos


def extrair_baloes(texto: Any) -> list[str]:
    bruto = _texto_de_conteudo(texto).strip()
    if not bruto:
        logger.warning("extrair_baloes_texto_vazio")
        return [TEXTO_VAZIO_FALLBACK]

    dados: Any = None
    parseou = False
    for candidato in _candidatos_json(bruto):
        try:
            dados = json.loads(candidato)
        except (ValueError, TypeError):
            continue
        parseou = True
        break

    if not parseou:
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
