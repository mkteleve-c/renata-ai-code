"""Tools de agenda da Renata — sugerir horários e marcar a consultoria.

A política é a do workflow `#3 Agenda` do n8n: segunda a sexta, slots de 60
minutos em horas cheias, manhã `8-11`, tarde `13-17`, noite `18-21`, janela
de 4 dias corridos a partir de **D+1** (nunca hoje) e escassez de no máximo
2 dias com 2 horários cada.

**Ocupado é sobreposição de intervalo, nunca igualdade de hora.** A leitura
real da agenda na Task 4 trouxe eventos de duração quebrada e sobrepostos
entre si — `13:15–14:15`, `09:30–10:05` cruzando `10:00–10:30`. Um
`evento.start.hour == hora` liberaria as 13h *e* as 14h para o evento das
13:15, e a Renata ofereceria ao lead um horário já ocupado. Aqui um slot
`[h, h+1)` está livre quando nenhum evento satisfaz
`inicio < slot_fim and fim > slot_inicio`. As desigualdades são estritas de
propósito: um evento que termina às 13:00 não bloqueia o slot das 13h.

**Evento de dia inteiro bloqueia o dia inteiro.** Ele chega com `start.date`
(sem hora) e `end.date` exclusivo, e numa agenda de trabalho significa
feriado, viagem ou off-site. Oferecer um horário dentro dele compra um
no-show; perdê-lo custa uma alternativa a menos na lista. O tratamento é
convertê-lo no intervalo `[data 00:00, data_fim 00:00)` — a partir daí a
mesma checagem de sobreposição cuida do resto, sem caso especial.

**`status == "cancelled"` não ocupa nada.** O Google devolve cancelados em
certas listagens; tratá-los como ocupados esconderia horários livres.

**Os portões do SOP são `if`, não parágrafo.** `calendar_agendar` recusa
enquanto o lead não tiver e-mail *e* faturamento mensal, e devolve ao agente
a fase para onde voltar (6 ou 7). O prompt chama a sequência de "INVIOLÁVEL"
e "TERMINANTEMENTE PROIBIDO" — três parágrafos pedem, um `if` garante. Como
nenhuma outra tool grava esses dois campos (o n8n coletava e nunca
persistia: 0 leads com e-mail no banco de origem), `calendar_agendar` aceita
os dois como argumento e os persiste junto do agendamento — mas a coluna,
quando preenchida, sempre vence o argumento (ver `_preferir_coluna`).

**O único `event_id` legítimo é o gravado no lead.** `calendar_update`,
`calendar_delete` e `calendar_get_event` recusam id divergente em vez de
obedecer: aceitar um id alucinado faria `calendar_delete` responder
"Cancelado" ao lead com o evento intacto na agenda do Silvio, e
`calendar_get_event` despejar na conversa o compromisso de um terceiro. Ver
`resolver_event_id`.

**A gravação no banco é um desfecho, não um efeito colateral.**
`gravar_agendamento` confere `rowcount` e devolve `False` quando nada casou
— lead ausente de `leads_crm` com evento já criado na agenda é o caminho
concreto de um `google_event_id` se perder para sempre. Nesse caso a tool
confirma o agendamento (ele existe) e manda acionar `human_handover`, com o
id no log para reconciliação. O id do evento é derivado de `(lead, slot)`
justamente para sobreviver a isso — ver `event_id_deterministico`.

Toda tool devolve **string** em qualquer desfecho, inclusive erro. Exceção
que sobe de uma tool derruba o turno do agente; uma frase deixa a Renata
seguir o SOP ("tente 3x, depois human_handover").
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from langchain_core.tools import tool

from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.google_calendar import (
    FUSO,
    GoogleCalendarClient,
)
from whatsapp_langchain.shared.phone import canonico_do_lead

from ..contexto import sanitizar_nome, telefone_do_turno

logger = structlog.get_logger()

DURACAO_MINUTOS = 60
DIAS_JANELA = 4

PERIODOS: dict[str, tuple[int, ...]] = {
    "manha": (8, 9, 10, 11),
    "tarde": (13, 14, 15, 16, 17),
    "noite": (18, 19, 20, 21),
}
PERIODOS["qualquer"] = tuple(
    sorted(PERIODOS["manha"] + PERIODOS["tarde"] + PERIODOS["noite"])
)

PERIODO_DA_HORA = {
    hora: nome for nome in ("manha", "tarde", "noite") for hora in PERIODOS[nome]
}

# Ordem de preferência quando o lead NÃO pede período — ver `escolher_horarios`.
PREFERENCIA_PERIODO = ("tarde", "noite", "manha")

# Escassez do prompt: no máximo 2 dias e 2 horários por dia (4 slots).
MAX_DIAS_SUGERIDOS = 2
MAX_HORARIOS_POR_DIA = 2

TITULO = "Consultoria de Alavancagem de Carreira - {nome}"

# Sem "-feira": a saída do n8n é `quinta 12/02: 13, 14`.
DIAS_CURTOS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

# Suficiente para separar um e-mail de um "sim" — validar RFC 5322 aqui só
# criaria falso negativo. O que importa é o convite não ir para o vazio.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

FALHA_AGENDA = (
    "Não consegui falar com a agenda agora. Tente de novo em instantes; "
    "se persistir, acione o human_handover."
)

_cliente: GoogleCalendarClient | None = None


# --- Cliente ---------------------------------------------------------------


def obter_cliente() -> GoogleCalendarClient:
    """Cliente único do processo — o cache de token só vale se ele for um.

    Construir um por chamada faria cada tool renovar o access token do zero,
    jogando fora o cache que a Task 4 montou. Credencial ausente levanta
    `ValueError` no construtor, que as tools transformam em mensagem.
    """
    global _cliente
    if _cliente is None:
        _cliente = GoogleCalendarClient(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            refresh_token=settings.google_refresh_token,
            calendar_id=settings.google_calendar_id,
        )
    return _cliente


def resetar_cliente() -> None:
    """Descarta o cliente guardado. Existe para os testes."""
    global _cliente
    _cliente = None


def agora_sp() -> datetime:
    """Instante atual em São Paulo. Ponto único de injeção nos testes."""
    return datetime.now(FUSO)


# --- Disponibilidade (puro) ------------------------------------------------


def horas_do_periodo(periodo: str) -> tuple[int, ...] | None:
    return PERIODOS.get((periodo or "").strip().lower())


def _instante(bloco: Any) -> datetime | None:
    """Converte `start`/`end` do Google em datetime no fuso de São Paulo.

    Dois shapes: `dateTime` (com offset) e `date` (dia inteiro, sem hora).
    O segundo vira meia-noite local — é o que faz o evento de dia inteiro
    cobrir todos os slots do dia pela checagem normal de sobreposição.
    """
    if not isinstance(bloco, dict):
        return None

    bruto = bloco.get("dateTime")
    if isinstance(bruto, str) and bruto:
        try:
            return datetime.fromisoformat(bruto.replace("Z", "+00:00")).astimezone(FUSO)
        except ValueError:
            return None

    bruto = bloco.get("date")
    if isinstance(bruto, str) and bruto:
        try:
            return datetime.fromisoformat(bruto).replace(tzinfo=FUSO)
        except ValueError:
            return None

    return None


def intervalo_do_evento(evento: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """`(inicio, fim)` do evento, ou `None` quando não dá para ler.

    Evento sem horário legível é ignorado com aviso, não bloqueia o dia: o
    Google sempre manda `start`/`end`, então esse shape é anomalia, e
    apagar um dia inteiro da lista por causa dela custaria mais do que
    corrige.

    Duas normalizações defensivas. O Google não produz nenhum dos dois
    shapes, mas a checagem de sobreposição os leria como **livre**, que é o
    lado errado do erro:

    - `fim < inicio` (invertido) tem os extremos trocados. Sem isso,
      `ev_i < fim and ev_f > inicio` é falso para qualquer slot e o evento
      simplesmente some da agenda.
    - `fim == inicio` (duração zero) é um ponto no tempo. Ele passa a
      ocupar um minuto, o suficiente para bloquear o slot que o contém —
      compromisso marcado sem duração ainda é compromisso.
    """
    inicio = _instante(evento.get("start"))
    fim = _instante(evento.get("end"))
    if inicio is None or fim is None:
        logger.warning("agenda_evento_sem_horario", event_id=evento.get("id"))
        return None

    if fim < inicio:
        logger.warning(
            "agenda_evento_invertido",
            event_id=evento.get("id"),
            inicio=inicio.isoformat(),
            fim=fim.isoformat(),
        )
        inicio, fim = fim, inicio

    if fim == inicio:
        logger.warning("agenda_evento_sem_duracao", event_id=evento.get("id"))
        fim = inicio + timedelta(minutes=1)

    return inicio, fim


def intervalos_ocupados(
    eventos: list[dict[str, Any]],
    ignorar_event_id: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Intervalos que de fato ocupam a agenda.

    `ignorar_event_id` serve ao reagendamento: o evento que está sendo
    movido aparece na listagem e não pode bloquear a si mesmo.
    """
    ocupados = []
    for evento in eventos:
        if not isinstance(evento, dict):
            continue
        if evento.get("status") == "cancelled":
            continue
        if ignorar_event_id and evento.get("id") == ignorar_event_id:
            continue
        intervalo = intervalo_do_evento(evento)
        if intervalo is not None:
            ocupados.append(intervalo)
    return ocupados


def slot_livre(dia: date, hora: int, ocupados: list[tuple[datetime, datetime]]) -> bool:
    """Sobreposição de intervalo, com desigualdade estrita nas bordas."""
    inicio = datetime(dia.year, dia.month, dia.day, hora, tzinfo=FUSO)
    fim = inicio + timedelta(minutes=DURACAO_MINUTOS)
    return not any(ev_i < fim and ev_f > inicio for ev_i, ev_f in ocupados)


def janela_bruta(
    agora: datetime,
    a_partir_de: date | None = None,
    dias: int = DIAS_JANELA,
) -> list[date]:
    """Os `dias` dias corridos da janela, fim de semana incluído.

    Começa em D+1 sempre: hoje nunca entra. `a_partir_de` só empurra a
    janela para frente — pedir o passado não desfaz a regra.
    """
    primeiro = agora.astimezone(FUSO).date() + timedelta(days=1)
    if a_partir_de is not None and a_partir_de > primeiro:
        primeiro = a_partir_de
    return [primeiro + timedelta(days=i) for i in range(dias)]


def calcular_disponibilidade(
    eventos: list[dict[str, Any]],
    agora: datetime,
    periodo: str = "qualquer",
    a_partir_de: date | None = None,
    dias: int = DIAS_JANELA,
) -> list[tuple[date, list[int]]]:
    """Horas livres por dia útil da janela, sem aplicar escassez."""
    horas = horas_do_periodo(periodo)
    if horas is None:
        raise ValueError(f"período desconhecido: {periodo!r}")

    ocupados = intervalos_ocupados(eventos)

    disponibilidade: list[tuple[date, list[int]]] = []
    for dia in janela_bruta(agora, a_partir_de, dias):
        if dia.weekday() >= 5:
            continue
        livres = [hora for hora in horas if slot_livre(dia, hora, ocupados)]
        if livres:
            disponibilidade.append((dia, livres))
    return disponibilidade


def escolher_horarios(horas: list[int], periodo: str) -> list[int]:
    """Os 2 horários que a Renata vai oferecer naquele dia.

    Quando o lead **pediu** um período, os dois primeiros dele: ele já
    escolheu a faixa, e a tool não tem por que discordar.

    Quando o lead **não** pediu (`qualquer`, o default), pegar os dois
    primeiros da lista ordenada daria `8, 9` para toda agenda com a manhã
    livre — o horário menos provável para um profissional em cargo de
    liderança, que é o público do SOP, e concentraria as consultorias às 8h
    da manhã. Por isso duas correções:

    1. **Começa pela tarde**, depois noite, depois manhã. É a preferência
       que o próprio exemplo do n8n carrega (`quinta 12/02: 13, 14`).
    2. **O segundo horário vem de outro período**, quando existir. `13, 14`
       são a mesma resposta para o lead — quem não pode às 13h raramente
       pode às 14h. `13, 18` é escolha de verdade e dobra a chance de uma
       das duas servir.

    A saída sai em ordem crescente: `13, 18` lê melhor que `18, 13`.
    """
    if periodo != "qualquer":
        return horas[:MAX_HORARIOS_POR_DIA]

    ordenadas = sorted(
        horas,
        key=lambda hora: (PREFERENCIA_PERIODO.index(PERIODO_DA_HORA[hora]), hora),
    )
    if not ordenadas:
        return []

    escolhidas = [ordenadas[0]]
    for hora in ordenadas[1:]:
        if PERIODO_DA_HORA[hora] != PERIODO_DA_HORA[escolhidas[0]]:
            escolhidas.append(hora)
            break

    if len(escolhidas) < MAX_HORARIOS_POR_DIA:
        escolhidas += [hora for hora in ordenadas if hora not in escolhidas][
            : MAX_HORARIOS_POR_DIA - len(escolhidas)
        ]

    return sorted(escolhidas)


def aplicar_escassez(
    disponibilidade: list[tuple[date, list[int]]],
    periodo: str = "qualquer",
) -> list[tuple[date, list[int]]]:
    """Corta em 2 dias × 2 horários.

    Os **dias** são sempre os mais próximos: fecha reunião mais cedo e é
    determinístico — sortear daria respostas diferentes para o mesmo lead
    perguntando duas vezes, e o SOP manda reconsultar antes de confirmar.
    Os **horários** dentro do dia seguem `escolher_horarios`.
    """
    return [
        (dia, escolher_horarios(horas, periodo))
        for dia, horas in disponibilidade[:MAX_DIAS_SUGERIDOS]
    ]


def formatar_disponibilidade(disponibilidade: list[tuple[date, list[int]]]) -> str:
    """`quinta 12/02: 13, 14` — uma linha por dia, como no n8n."""
    return "\n".join(
        f"{DIAS_CURTOS[dia.weekday()]} {dia.strftime('%d/%m')}: "
        + ", ".join(str(hora) for hora in horas)
        for dia, horas in disponibilidade
    )


def formatar_slot(momento: datetime) -> str:
    """`quinta 12/02 às 13h`."""
    return (
        f"{DIAS_CURTOS[momento.weekday()]} {momento.strftime('%d/%m')} "
        f"às {momento.hour}h"
    )


# --- Entrada do agente -----------------------------------------------------

_FORMATOS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %Hh",
)


def parse_inicio(bruto: str) -> datetime | None:
    """Data/hora escolhida pelo lead, em qualquer forma plausível.

    O modelo escreve `2026-02-12T13:00`, `12/02/2026 13:00` ou o ISO
    completo com offset. Sem tzinfo é lido como relógio de parede de São
    Paulo — a mesma convenção de `formatar_iso` e `formatar_data_hoje`.
    """
    texto = (bruto or "").strip()
    if not texto:
        return None

    try:
        momento = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        momento = None

    if momento is None:
        for formato in _FORMATOS:
            try:
                momento = datetime.strptime(texto, formato)
                break
            except ValueError:
                continue

    if momento is None:
        return None

    if momento.tzinfo is None:
        return momento.replace(tzinfo=FUSO)
    return momento.astimezone(FUSO)


def validar_slot(momento: datetime, agora: datetime) -> str | None:
    """Mensagem de recusa quando o horário viola a política, ou `None`."""
    # `microsecond` junto: `2026-02-12 13:00:00.5` passaria pelos dois
    # primeiros e criaria um evento às 13:00:00.5 — que o Google aceita.
    if momento.minute or momento.second or momento.microsecond:
        return (
            "A consultoria só encaixa em hora cheia (13:00, 14:00...). "
            "Confirme com o lead uma hora cheia e tente de novo."
        )
    if momento.weekday() >= 5:
        return "A agenda do Silvio é de segunda a sexta. Ofereça um dia útil ao lead."
    if momento.date() <= agora.astimezone(FUSO).date():
        return (
            "Só dá para agendar a partir de amanhã. "
            "Ofereça um dia da janela consultada."
        )
    if momento.hour not in PERIODOS["qualquer"]:
        return (
            f"{momento.hour}h está fora da grade de atendimento "
            "(manhã 8-11, tarde 13-17, noite 18-21). Ofereça outro horário."
        )
    return None


# --- Lead ------------------------------------------------------------------


def _canonico(telefone: str) -> str:
    return canonico_do_lead(telefone)


async def carregar_lead(telefone: str) -> dict[str, Any] | None:
    """Lê do lead o que os portões e o convite precisam. Nunca levanta."""
    canonico = _canonico(telefone)
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select phone, name, email, faturamento_mensal, google_event_id "
                "from leads_crm where phone = %s",
                (canonico,),
            )
            linha = await cur.fetchone()
    except Exception as erro:
        logger.warning("agenda_lead_nao_lido", phone=canonico, erro=str(erro))
        return None

    if linha is None:
        logger.info("agenda_lead_ausente", phone=canonico)
        return None

    return {
        "phone": linha[0],
        "name": linha[1],
        "email": linha[2],
        "faturamento_mensal": linha[3],
        "google_event_id": linha[4],
    }


async def gravar_agendamento(
    telefone: str,
    google_event_id: str | None,
    email: str | None = None,
    faturamento_mensal: str | None = None,
) -> bool:
    """Persiste o id do evento e, quando vierem, e-mail e faturamento.

    `google_event_id` é escrito sempre — inclusive `NULL`, que é como o
    cancelamento apaga o vínculo. `email` e `faturamento_mensal` só quando
    não-vazios: um argumento em branco não pode apagar o que já estava lá.

    **Devolve `False` quando o UPDATE não casou nenhuma linha.** Sem checar
    `rowcount`, um lead ausente de `leads_crm` — que é possível: o portão
    aceita e-mail e faturamento vindos por argumento — faria o evento nascer
    na agenda e o `UPDATE` casar zero linhas em silêncio absoluto, perdendo
    o `google_event_id` para sempre. Zero linhas é falha, não sucesso.

    Continua sem levantar: o evento já está na agenda e uma exceção aqui
    desfaria uma confirmação verdadeira. Quem chama decide o que dizer ao
    lead, e o log carrega `google_event_id` para reconciliação manual.
    """
    canonico = _canonico(telefone)
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "update leads_crm set"
                "  google_event_id = %s,"
                "  email = coalesce(nullif(%s, ''), email),"
                "  faturamento_mensal = coalesce(nullif(%s, ''), faturamento_mensal)"
                " where phone = %s",
                (google_event_id, email or "", faturamento_mensal or "", canonico),
            )
            afetadas = cur.rowcount
    except Exception as erro:
        logger.error(
            "agenda_lead_nao_atualizado",
            phone=canonico,
            google_event_id=google_event_id,
            erro=str(erro),
        )
        return False

    if afetadas == 0:
        logger.error(
            "agenda_lead_inexistente_na_gravacao",
            phone=canonico,
            google_event_id=google_event_id,
        )
        return False

    return True


def event_id_deterministico(telefone: str, inicio: datetime) -> str:
    """Id do evento derivado de telefone + horário, estável entre turnos.

    O `events.insert` do Google aceita id escolhido pelo cliente e responde
    `409 Conflict` quando ele já existe — que `criar_evento` resolve lendo o
    evento existente. Derivar o id de `(lead, slot)` em vez de sortear
    compra duas coisas:

    - **retry deixa de duplicar.** Um timeout depois de o Google já ter
      criado o evento, ou o processor reentregando a mensagem, cai no mesmo
      id e colide — em vez de marcar duas consultorias no mesmo horário na
      agenda de trabalho de uma pessoa real.
    - **reconciliação trivial.** Se o `UPDATE` no banco falhar, o id não se
      perde: recomputá-lo a partir do telefone e do horário confirmado ao
      lead devolve exatamente o evento que ficou órfão.

    SHA-256 truncado em 32 caracteres hex — `0-9a-f` é subconjunto próprio
    do base32hex que `validar_event_id` exige, e 128 bits deixam a colisão
    fora de qualquer escala que este sistema alcance.
    """
    semente = f"{_canonico(telefone)}:{inicio.astimezone(FUSO).isoformat()}"
    return hashlib.sha256(semente.encode()).hexdigest()[:32]


async def _lead_do_turno() -> tuple[str, dict[str, Any] | None] | None:
    telefone = telefone_do_turno()
    if not telefone:
        logger.warning("agenda_sem_telefone_no_config")
        return None
    return telefone, await carregar_lead(telefone)


def _preferir_coluna(gravado: Any, argumento: str, campo: str) -> str:
    """Valor do portão: a coluna vence o argumento quando existe.

    O argumento do modelo é o fallback, não a fonte. Hoje ele é o único
    caminho — nenhuma tool grava `email`/`faturamento_mensal`, e ler só a
    coluna deixaria o agendamento inalcançável —, mas a partir do momento em
    que a Task 6 persistir esses campos, o que o lead **realmente disse**
    passa a estar no banco, e um argumento inventado no meio do turno não
    pode sobrepô-lo. Escrito assim desde já para que a virada seja
    automática.

    Divergência entre os dois vira log: é o sintoma de coluna velha (o lead
    corrigiu o e-mail e ninguém atualizou) e de alucinação, e os dois casos
    precisam ser diagnosticáveis sem reproduzir a conversa.
    """
    coluna = (gravado or "").strip() if isinstance(gravado, str) else ""
    informado = (argumento or "").strip()

    if coluna and informado and coluna != informado:
        logger.warning("agenda_valor_divergente_do_cadastro", campo=campo)

    return coluna or informado


def resolver_event_id(lead: dict[str, Any], event_id: str) -> tuple[str | None, str]:
    """`(id, mensagem_de_recusa)` para as tools que operam sobre um evento.

    **O único id legítimo é o gravado no lead.** Nada no prompt dá ao modelo
    uma fonte de ids de evento, então um `event_id` que chega por argumento
    e não bate com o do lead é alucinação ou vazamento de outra conversa —
    e o estrago de aceitá-lo é concreto:

    - `calendar_delete` com id inventado bate num 404 que o cliente tolera
      por ser idempotente; a tool responderia *"Cancelado"* ao lead com o
      evento intacto na agenda do Silvio **e** o vínculo apagado, deixando
      o agente sem como cancelar de verdade depois.
    - `calendar_get_event` com id de terceiro devolveria o `summary` de um
      compromisso alheio para dentro da conversa.

    Por isso o argumento não tem precedência: ele é aceito só quando é
    redundante (igual ao gravado) e recusado quando diverge.
    """
    gravado = (lead.get("google_event_id") or "").strip()
    informado = (event_id or "").strip()

    if not gravado:
        return None, (
            "Não encontrei nenhum agendamento registrado para este lead. "
            "Confirme com ele e agende do zero."
        )

    if informado and informado != gravado:
        logger.warning(
            "agenda_event_id_divergente",
            informado=informado,
            gravado=gravado,
        )
        return None, (
            "O event_id informado não é o do agendamento deste lead — não vou "
            "mexer em evento de outra pessoa. Chame de novo sem event_id."
        )

    return gravado, ""


# --- Tools -----------------------------------------------------------------


@tool
async def calendar_get_many(periodo: str = "qualquer", a_partir_de: str = "") -> str:
    """Consulta a agenda e devolve horários livres para oferecer ao lead.

    Sempre use antes de sugerir qualquer horário e de novo antes de agendar.

    Args:
        periodo: `manha` (8-11), `tarde` (13-17), `noite` (18-21) ou
            `qualquer`. Use o que o lead pediu.
        a_partir_de: opcional, data `AAAA-MM-DD` para empurrar a janela para
            frente quando o lead pedir uma semana específica. Vazio começa
            amanhã.

    Devolve no máximo 2 dias com 2 horários cada, no formato
    `quinta 12/02: 13, 14`. Nunca oferece hoje, fim de semana ou horário
    fora da grade.
    """
    horas = horas_do_periodo(periodo)
    if horas is None:
        return f"Período '{periodo}' não existe. Use manha, tarde, noite ou qualquer."

    inicio_pedido: date | None = None
    if a_partir_de.strip():
        momento = parse_inicio(f"{a_partir_de.strip()} 00:00")
        if momento is None:
            return "Não entendi a data em a_partir_de. Use o formato AAAA-MM-DD."
        inicio_pedido = momento.date()

    agora = agora_sp()
    dias = janela_bruta(agora, inicio_pedido)

    try:
        cliente = obter_cliente()
        eventos = await cliente.listar_eventos(
            datetime(dias[0].year, dias[0].month, dias[0].day, tzinfo=FUSO),
            datetime(dias[-1].year, dias[-1].month, dias[-1].day, tzinfo=FUSO)
            + timedelta(days=1),
        )
    except Exception as erro:
        logger.warning("agenda_consulta_falhou", periodo=periodo, erro=str(erro))
        return FALHA_AGENDA

    disponibilidade = calcular_disponibilidade(
        eventos, agora, periodo, a_partir_de=inicio_pedido
    )
    if not disponibilidade:
        return (
            "Nenhum horário livre nesse período nos próximos dias. "
            "Ofereça outro período ou consulte uma semana adiante."
        )

    return formatar_disponibilidade(aplicar_escassez(disponibilidade, periodo))


@tool
async def calendar_agendar(
    inicio: str,
    email: str = "",
    faturamento_mensal: str = "",
) -> str:
    """Agenda a Consultoria de Alavancagem de Carreira no horário escolhido.

    Só chame depois de ter (a) o e-mail do lead e (b) o faturamento médio
    mensal. Sem os dois a tool recusa.

    Args:
        inicio: data e hora escolhidas, em `AAAA-MM-DDTHH:MM` (hora cheia,
            dia útil, a partir de amanhã).
        email: e-mail informado pelo lead. Passe sempre que tiver acabado de
            recebê-lo — é o que vai receber o convite.
        faturamento_mensal: faturamento médio mensal como o lead falou
            ("uns 30 mil"). Passe sempre que tiver acabado de recebê-lo.

    A tool reconsulta a disponibilidade antes de criar e recusa se o horário
    estiver ocupado.
    """
    contexto = await _lead_do_turno()
    if contexto is None:
        return (
            "Não consegui identificar o lead nesta conversa e não vou "
            "agendar às cegas. Acione o human_handover."
        )
    telefone, lead = contexto
    lead = lead or {}

    email_final = _preferir_coluna(lead.get("email"), email, "email")
    if not _EMAIL.match(email_final):
        return (
            "Ainda não tenho um e-mail válido deste lead — volte à Fase 6 e "
            "peça o melhor e-mail dele antes de agendar."
        )

    faturamento_final = _preferir_coluna(
        lead.get("faturamento_mensal"), faturamento_mensal, "faturamento_mensal"
    )
    if not faturamento_final:
        return (
            "Ainda não tenho o faturamento médio mensal deste lead — volte à "
            "Fase 7 e pergunte antes de agendar."
        )

    momento = parse_inicio(inicio)
    if momento is None:
        return (
            "Não entendi a data e hora. Confirme com o lead e mande no "
            "formato AAAA-MM-DDTHH:MM."
        )

    agora = agora_sp()
    recusa = validar_slot(momento, agora)
    if recusa:
        return recusa

    fim = momento + timedelta(minutes=DURACAO_MINUTOS)
    nome = sanitizar_nome(lead.get("name"))
    # Derivado de (lead, slot): retry não duplica e o id sobrevive à falha
    # de gravação. Ver `event_id_deterministico`.
    id_pretendido = event_id_deterministico(telefone, momento)

    try:
        cliente = obter_cliente()
        eventos = await cliente.listar_eventos(momento, fim)
        ocupados = intervalos_ocupados(eventos, ignorar_event_id=id_pretendido)
        if not slot_livre(momento.date(), momento.hour, ocupados):
            return (
                f"{formatar_slot(momento)} está ocupado na agenda. "
                "Consulte a disponibilidade e ofereça outro horário."
            )

        criado = await cliente.criar_evento(
            summary=TITULO.format(nome=nome),
            inicio=momento,
            fim=fim,
            participantes=[email_final],
            event_id=id_pretendido,
        )
    except Exception as erro:
        logger.warning("agenda_agendamento_falhou", inicio=inicio, erro=str(erro))
        return FALHA_AGENDA

    event_id = (criado.get("id") if isinstance(criado, dict) else None) or id_pretendido

    gravou = await gravar_agendamento(
        telefone,
        google_event_id=event_id,
        email=email_final,
        faturamento_mensal=faturamento_final,
    )

    confirmacao = (
        f"Agendado: {formatar_slot(momento)}. Convite enviado para {email_final}."
    )

    if not gravou:
        # O evento EXISTE na agenda — negar isso ao lead seria mentira na
        # direção oposta. O que falhou foi o vínculo no CRM, e o log carrega
        # tudo que a reconciliação manual precisa.
        logger.error(
            "agenda_vinculo_nao_gravado",
            telefone=telefone,
            google_event_id=event_id,
            inicio=momento.isoformat(),
        )
        return (
            f"{confirmacao} ATENÇÃO: não consegui registrar o agendamento no "
            "cadastro do lead — acione o human_handover para registro manual."
        )

    logger.info(
        "agenda_evento_agendado",
        telefone=telefone,
        google_event_id=event_id,
        inicio=momento.isoformat(),
    )
    return confirmacao


@tool
async def calendar_update(novo_inicio: str, event_id: str = "") -> str:
    """Reagenda a consultoria já marcada para um novo horário.

    Args:
        novo_inicio: nova data e hora, em `AAAA-MM-DDTHH:MM`.
        event_id: normalmente não precisa — a tool usa o evento gravado no
            lead. Um id diferente do gravado é recusado.

    Reconsulta a disponibilidade do novo horário antes de mover.
    """
    contexto = await _lead_do_turno()
    if contexto is None:
        return "Não consegui identificar o lead nesta conversa."
    telefone, lead = contexto
    lead = lead or {}

    alvo, recusa_id = resolver_event_id(lead, event_id)
    if alvo is None:
        return recusa_id

    momento = parse_inicio(novo_inicio)
    if momento is None:
        return "Não entendi a data e hora. Use o formato AAAA-MM-DDTHH:MM."

    recusa = validar_slot(momento, agora_sp())
    if recusa:
        return recusa

    fim = momento + timedelta(minutes=DURACAO_MINUTOS)

    try:
        cliente = obter_cliente()
        eventos = await cliente.listar_eventos(momento, fim)
        ocupados = intervalos_ocupados(eventos, ignorar_event_id=alvo)
        if not slot_livre(momento.date(), momento.hour, ocupados):
            return (
                f"{formatar_slot(momento)} está ocupado na agenda. "
                "Ofereça outro horário."
            )
        await cliente.atualizar_evento(alvo, inicio=momento, fim=fim)
    except Exception as erro:
        logger.warning("agenda_reagendamento_falhou", event_id=alvo, erro=str(erro))
        return FALHA_AGENDA

    logger.info("agenda_evento_reagendado", telefone=telefone, event_id=alvo)
    return f"Reagendado para {formatar_slot(momento)}."


@tool
async def calendar_delete(event_id: str = "") -> str:
    """Cancela a consultoria agendada.

    Args:
        event_id: normalmente não precisa — a tool usa o evento gravado no
            lead. Um id diferente do gravado é recusado.
    """
    contexto = await _lead_do_turno()
    if contexto is None:
        return "Não consegui identificar o lead nesta conversa."
    telefone, lead = contexto
    lead = lead or {}

    alvo, recusa_id = resolver_event_id(lead, event_id)
    if alvo is None:
        return recusa_id

    try:
        await obter_cliente().deletar_evento(alvo)
    except Exception as erro:
        logger.warning("agenda_cancelamento_falhou", event_id=alvo, erro=str(erro))
        return FALHA_AGENDA

    gravou = await gravar_agendamento(telefone, google_event_id=None)
    logger.info("agenda_evento_cancelado", telefone=telefone, event_id=alvo)
    if not gravou:
        logger.error(
            "agenda_vinculo_nao_apagado", telefone=telefone, google_event_id=alvo
        )
        return (
            "Cancelado na agenda. ATENÇÃO: não consegui limpar o registro no "
            "cadastro do lead — acione o human_handover."
        )
    return "Cancelado. A consultoria foi removida da agenda."


@tool
async def calendar_get_event(event_id: str = "") -> str:
    """Consulta os detalhes da consultoria já agendada.

    Args:
        event_id: normalmente não precisa — a tool usa o evento gravado no
            lead. Um id diferente do gravado é recusado.
    """
    contexto = await _lead_do_turno()
    if contexto is None:
        return "Não consegui identificar o lead nesta conversa."
    _telefone, lead = contexto
    lead = lead or {}

    alvo, recusa_id = resolver_event_id(lead, event_id)
    if alvo is None:
        return recusa_id

    try:
        evento = await obter_cliente().obter_evento(alvo)
    except Exception as erro:
        logger.warning("agenda_consulta_evento_falhou", event_id=alvo, erro=str(erro))
        return FALHA_AGENDA

    inicio = _instante(evento.get("start"))
    quando = formatar_slot(inicio) if inicio else "horário não informado"
    titulo = evento.get("summary") or "Consultoria"
    status = evento.get("status") or "desconhecido"
    return f"{titulo} — {quando} (status: {status})."


TOOLS_AGENDA = [
    calendar_get_many,
    calendar_agendar,
    calendar_update,
    calendar_delete,
    calendar_get_event,
]
