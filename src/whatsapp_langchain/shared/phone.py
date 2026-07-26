"""Canonicalização de telefone brasileiro e resolução de JID do WhatsApp.

Duas representações convivem no projeto:

- canônico: só dígitos, brasileiro SEM o 9º dígito (`551187654321`) —
  usado em leads_crm, blocklist e legacy_chat_history.
- E.164 do harness: canônico com "+" (`+551187654321`) — usado em
  message_queue.phone_number e no thread_id.

A regra do 9º dígito só se aplica a números brasileiros. Estrangeiros
passam apenas com os dígitos preservados.

Duas deformações do mesmo telefone entram por aqui e precisam convergir
para a canônica, não virar identidade nova:

- sufixo de aparelho do Baileys (`551187654321:3@s.whatsapp.net`), que
  colado aos dígitos vira um número inexistente e manda outbound para ele;
- 0 de tronco na frente do DDD (`011987654321`, `55011987654321`) — perfil
  dos ~26 registros malformados da base legada.

O que se declara brasileiro — por DDI 55 ou por 0 de tronco — e ainda assim
não fecha com nenhuma forma válida é recusado com `None`, não aceito como
identidade. Ver `canonicalizar`.

Endereçamento que não é telefone (`@g.us` de grupo, `@lid` do LinkedID) é
recusado antes de qualquer normalização: um LID pode ter o formato de um
telefone brasileiro e colidir com um lead real.

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

# `551187654321:3` — o `:3` é o aparelho pareado. A âncora à esquerda não é
# decoração: com `:\d+$` solto, `whatsapp:5511987654321` (o `From` do Twilio
# sem o `+`) casava inteiro e sobrava "whatsapp", virando None. Só corta
# quando o que está à esquerda dos dois-pontos é o número todo.
SUFIXO_DE_APARELHO = re.compile(r"^(\+?\d+):\d+$")

# Endereçamentos do WhatsApp que não carregam telefone. `@lid` é o LinkedID
# que o WhatsApp está distribuindo no lugar do número: identificador opaco
# que pode ter o mesmo comprimento de um telefone e casar com as regex
# daqui. `551188654321@lid` vira exatamente a chave primária de um lead real
# — duas pessoas distintas colidindo na mesma linha de leads_crm. Não há
# como distinguir depois, então a recusa é na entrada.
SERVIDORES_SEM_TELEFONE = ("@g.us", "@lid")


def _e_endereco_sem_telefone(bruto: str) -> bool:
    baixa = bruto.lower()
    return any(servidor in baixa for servidor in SERVIDORES_SEM_TELEFONE)


def _digitos_do_jid(bruto: str) -> str:
    """Dígitos do telefone, sem o servidor (`@...`) nem o aparelho (`:N`)."""
    local = bruto.split("@", 1)[0]
    if m := SUFIXO_DE_APARELHO.match(local):
        local = m.group(1)
    return re.sub(r"\D", "", local)


def _sem_zero_de_tronco(digitos: str) -> str:
    """Tira o 0 de discagem nacional colado no DDD.

    E.164 nunca começa com 0 e nenhum DDD brasileiro começa com 0, então
    `011...` e `55011...` só podem ser o telefone certo com o prefixo de
    tronco grudado.
    """
    if digitos.startswith("550"):
        return "55" + digitos[2:].lstrip("0")
    if digitos.startswith("0"):
        return digitos.lstrip("0")
    return digitos


def canonicalizar(bruto: str | None) -> str | None:
    """Reduz qualquer forma de telefone à representação canônica.

    Devolve `None` quando a entrada se declara brasileira (DDI 55) e não
    fecha com nenhuma forma válida. A alternativa — devolver os dígitos
    crus, como antes — fazia lixo virar chave primária em leads_crm: o
    CHECK da tabela é só `^[0-9]{8,15}$` e não recusa nada depois. Quem
    chama registra o descarte.
    """
    if not bruto or _e_endereco_sem_telefone(bruto):
        return None

    crus = _digitos_do_jid(bruto)
    digitos = _sem_zero_de_tronco(crus)
    # Quem trouxe 0 de tronco se declarou discagem nacional brasileira, do
    # mesmo jeito que o DDI 55 — E.164 nunca começa com 0 e estrangeiro não
    # chega com prefixo de tronco do Brasil.
    declarou_brasil = digitos != crus

    if not 8 <= len(digitos) <= 15:
        return None

    if m := LOCAL_COM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"
    if m := LOCAL_SEM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"
    if m := BR_COM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"
    if BR_SEM_9.match(digitos):
        return digitos

    # Nenhum país tem DDI 550–559: 55 aqui só pode ser Brasil malformado.
    # Estrangeiro segue passando pelos dígitos.
    #
    # `declarou_brasil` fecha o mesmo furo pelo outro lado: `011187654321`
    # perde o 0, vira `11187654321` e não começa mais com 55 — não casa
    # nenhuma forma (11 dígitos brasileiros são DDD + 9XXXXXXXX, e o
    # assinante aqui começa com 1) e escapava como identidade nova. Não há
    # conserto sem adivinhar qual dígito sobra; o 0 já era a única correção
    # inequívoca. Vai para leads_descartados.
    if digitos.startswith("55") or declarou_brasil:
        return None

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
    Grupos (`@g.us`), LIDs (`@lid`) e JIDs fora do tamanho esperado são
    rejeitados.

    O corte por tamanho é feito sobre os dígitos já sem o sufixo de
    aparelho: `551187654321:3` tem 12 dígitos de telefone, não 13. Ele pega
    a maioria dos LIDs por acidente, mas não todos — a recusa por `@lid` é
    quem garante.
    """
    valor = key.get("remoteJid")
    if not valor:
        return None

    texto = str(valor)
    if _e_endereco_sem_telefone(texto):
        return None

    digitos = _digitos_do_jid(texto)
    if not 12 <= len(digitos) <= 14:
        return None

    return canonicalizar(digitos)


def to_e164(canonico: str) -> str:
    return canonico if canonico.startswith("+") else f"+{canonico}"


def from_e164(e164: str) -> str:
    return e164.lstrip("+")
