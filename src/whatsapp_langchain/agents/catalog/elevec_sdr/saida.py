"""Converte a resposta final do agente em balões de WhatsApp.

O n8n usa um output parser estruturado sobre o TEXTO FINAL, depois que o ciclo
de tools terminou — não `response_format` nativo. Replicamos o mesmo mecanismo:
estruturar a resposta e chamar ferramentas no mesmo turno faz o modelo devolver
o JSON em vez de chamar a tool, ou quebrar o schema quando há tool call pendente.

Nunca devolve lista vazia: perder a resposta é pior que perder a formatação.
"""

import json
import re

CERCA = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def extrair_baloes(texto: str) -> list[str]:
    bruto = (texto or "").strip()
    if not bruto:
        return [""]

    candidato = bruto
    if cerca := CERCA.search(bruto):
        candidato = cerca.group(1).strip()

    try:
        dados = json.loads(candidato)
    except (ValueError, TypeError):
        return [bruto]

    if not isinstance(dados, dict):
        return [bruto]

    mensagens = dados.get("messages")
    if not isinstance(mensagens, list):
        return [bruto]

    baloes = [m.strip() for m in mensagens if isinstance(m, str) and m.strip()]
    return baloes or [bruto]
