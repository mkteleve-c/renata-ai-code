"""Agente elevec_sdr - Renata, assistente de pré-vendas da EleveC.

Portado do workflow n8n `#1 Agente SDR | 10/02/26 | V2.2`. Qualifica leads
por WhatsApp e agenda a Consultoria de Alavancagem de Carreira com Silvio
Hirata seguindo o SOP de 8 fases (ver docs/evidencias/prompt-renata-n8n.md).

Uso:
    from whatsapp_langchain.agents.catalog.elevec_sdr import build_graph

    agent = build_graph()
    result = agent.invoke({"messages": [{"role": "user", "content": "Olá!"}]})
"""

from .agent import build_graph
from .prompts import SYSTEM_PROMPT

__all__ = ["build_graph", "SYSTEM_PROMPT"]
