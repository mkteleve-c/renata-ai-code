"""Marcador das strings de tool que **nunca** podem chegar ao lead.

Toda tool devolve string, e essa string entra na conversa como resultado de
tool — texto que o modelo lê e pode, sem nenhuma regra impedindo, repetir
para o lead. Parte desse texto é vocabulário interno: "Pipedrive",
"cadastro do lead", "acione o human_handover", "event_id". Um lead da EleveC
recebendo "ATENÇÃO: o card no Pipedrive não foi movido" é vazamento de
processo interno pelo WhatsApp.

O prefixo existe para que a regra no prompt seja **uma linha verificável**
em vez de uma lista de frases que envelhece a cada edição de tool. A regra
mora no bloco `### Resultado de tool (texto interno)` do `SYSTEM_PROMPT`:

> Resultado de tool que começa com `[sistema]` é instrução para VOCÊ.
> Nunca repita, cite, traduza nem resuma esse texto para o lead.

**O marcador sozinho não protege ninguém.** Sem a regra no prompt ele é só
mais um pedaço de texto que o modelo repassa junto com o resto. As duas
metades andam juntas, e `test_prompt_proibe_repassar_texto_interno_de_tool_
ao_lead` é o que impede uma de sumir sem a outra.

Onde o prefixo é aplicado:

- `crm.py` e `handover.py`: **todos** os retornos. Nenhuma das duas tools
  produz conteúdo que o lead precise ouvir — fase de funil, card, flags e
  cadastro são todos assunto interno. Sendo 100% dos retornos, a regra do
  prompt não precisa de nenhuma lista.
- `agenda.py`: **seletivamente**, e é o único jeito correto ali. As saídas
  de `calendar_get_many` e a confirmação de `calendar_agendar` carregam
  fato que o lead precisa (horários livres, o slot marcado); marcar 100%
  delas diluiria o sinal até ele não distinguir nada. Recebem o prefixo as
  que nomeiam ferramenta (`human_handover`, `calendar_*`), cadastro,
  `event_id`, ou mandam o agente voltar a uma fase do SOP. Ficam sem ele
  as que são fato: disponibilidade, slot confirmado, "está ocupado", "não
  entendi a data", "fora da grade".

A fronteira é conferida por teste, não por revisão de olho:
`test_saida_fixa_com_vocabulario_interno_vem_marcada` e
`test_nenhum_desfecho_de_agendar_vaza_vocabulario_interno`, em
tests/unit/test_tool_agenda.py.
"""

from __future__ import annotations

PREFIXO_INTERNO = "[sistema]"


def interno(texto: str) -> str:
    """Marca o texto como instrução para o agente, nunca para o lead."""
    return f"{PREFIXO_INTERNO} {texto}"
