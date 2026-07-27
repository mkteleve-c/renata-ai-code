"""Marcador das strings de tool que **nunca** podem chegar ao lead.

Toda tool devolve string, e essa string entra na conversa como resultado de
tool — texto que o modelo lê e pode, sem nenhuma regra impedindo, repetir
para o lead. Parte desse texto é vocabulário interno: "Pipedrive",
"cadastro do lead", "acione o human_handover", "event_id". Um lead da EleveC
recebendo "ATENÇÃO: o card no Pipedrive não foi movido" é vazamento de
processo interno pelo WhatsApp.

O prefixo existe para que a regra no prompt seja **uma linha verificável**
em vez de uma lista de frases que envelhece a cada edição de tool:

> Resultado de tool que começa com `[sistema]` é instrução para você.
> Nunca repita, cite ou traduza esse texto para o lead.

Onde o prefixo é aplicado hoje:

- `crm.py` e `handover.py`: **todos** os retornos. Nenhuma das duas tools
  produz conteúdo que o lead precise ouvir — fase de funil, card, flags e
  cadastro são todos assunto interno. Sendo 100% dos retornos, a regra do
  prompt não precisa de nenhuma lista.
- `agenda.py`: **ainda não**, e de propósito. Ali a marcação teria de ser
  seletiva — as saídas de `calendar_get_many` e a confirmação de
  `calendar_agendar` carregam fato que o lead precisa (horários livres, o
  slot marcado), e marcar 100% delas diluiria o sinal até ele não distinguir
  nada. As que **precisam** do prefixo estão listadas no relatório da Task 6;
  aplicá-las é trabalho de quem estiver com o arquivo na mão, para não
  colidir com a rodada de correção que mexe nele em paralelo.
"""

from __future__ import annotations

PREFIXO_INTERNO = "[sistema]"


def interno(texto: str) -> str:
    """Marca o texto como instrução para o agente, nunca para o lead."""
    return f"{PREFIXO_INTERNO} {texto}"
