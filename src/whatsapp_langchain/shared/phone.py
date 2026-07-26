"""Canonicalização de telefone brasileiro e resolução de JID do WhatsApp.

Duas representações convivem no projeto:

- canônico: só dígitos, brasileiro SEM o 9º dígito (`551187654321`) —
  usado em leads_crm, blocklist e legacy_chat_history.
- E.164 do harness: canônico com "+" (`+551187654321`) — usado em
  message_queue.phone_number e no thread_id.

A regra do 9º dígito só se aplica a números brasileiros. Estrangeiros
passam apenas com os dígitos preservados.

Limitação conhecida e aceita: um número estrangeiro com exatamente 10 ou
11 dígitos e sem DDI é indistinguível de um brasileiro sem DDI, e ganharia
o prefixo 55. Medição na base de produção: 3.301 de 3.319 leads são
brasileiros válidos e nenhum é estrangeiro legítimo. Se isso mudar, a
entrada precisa trazer o DDI explícito.
"""

import re

BR_COM_9 = re.compile(r"^55(\d{2})9(\d{8})$")
BR_SEM_9 = re.compile(r"^55(\d{2})(\d{8})$")
LOCAL_COM_9 = re.compile(r"^(\d{2})9(\d{8})$")
LOCAL_SEM_9 = re.compile(r"^(\d{2})(\d{8})$")


def canonicalizar(bruto: str | None) -> str | None:
    """Reduz qualquer forma de telefone à representação canônica."""
    if not bruto:
        return None

    digitos = re.sub(r"\D", "", bruto.split("@", 1)[0])
    if not 8 <= len(digitos) <= 15:
        return None

    if m := LOCAL_COM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"
    if m := LOCAL_SEM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"
    if m := BR_COM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"

    return digitos


def variacoes(canonico: str) -> tuple[str, str]:
    """Devolve (com_9, sem_9). Para não-BR, ambos são o próprio número."""
    if m := BR_SEM_9.match(canonico):
        return f"55{m.group(1)}9{m.group(2)}", canonico
    return canonico, canonico


def resolver_telefone(key: dict) -> str | None:
    """Canonicaliza o remoteJid do payload da Evolution.

    Só `remoteJid` é lido: `remoteJidAlt` não é populado pela integração
    WHATSAPP-BUSINESS (verificado em 50 de 50 mensagens reais da instância).
    Grupos (@g.us) e JIDs fora do tamanho esperado são rejeitados.
    """
    valor = key.get("remoteJid")
    if not valor:
        return None

    texto = str(valor)
    if "@g.us" in texto:
        return None

    digitos = re.sub(r"\D", "", texto.split("@", 1)[0])
    if not 12 <= len(digitos) <= 14:
        return None

    return canonicalizar(digitos)


def to_e164(canonico: str) -> str:
    return canonico if canonico.startswith("+") else f"+{canonico}"


def from_e164(e164: str) -> str:
    return e164.lstrip("+")
