"""Cliente do Google Calendar — a agenda onde a Renata marca as consultorias.

Autenticação por **refresh token** de OAuth 2.0 (fluxo de aplicação
instalada, o mesmo que o nó do n8n usava): o refresh token é permanente e
vale por credencial de usuário; o access token que ele gera dura ~3600s.
Renovar a cada chamada seria uma ida à rede a mais por operação; nunca
renovar quebraria o agendamento uma hora depois do boot. O cliente guarda o
access token com margem (`MARGEM_RENOVACAO_SEGUNDOS`) e renova sozinho,
serializado por um lock — várias tools chamando em paralelo disparam uma
única renovação, não N.

Rotacionar o OAuth Client no Google Cloud Console invalida o refresh token
e derruba o agendamento até que alguém gere outro. É risco aceito no spec.

**Blindagem de resposta.** Toda leitura de corpo passa por `_safe_json` e
checagem de tipo, com log estruturado antes de levantar. Foi a lição que a
Fase 1 pagou caro no `EvolutionClient`: `response.json()["campo"]` cru
transforma um 200 com corpo inesperado num `KeyError` sem log, no meio de um
turno do agente.

Leitura e escrita tratam corpo estranho de formas **deliberadamente
diferentes**:

- leitura (`listar_eventos`, `obter_evento`) levanta `GoogleCalendarError`.
  Devolver lista vazia diante de um corpo irreconhecível faria a Renata ler
  "agenda livre" e oferecer um horário já ocupado — errar calado é pior que
  falhar.
- escrita (`criar_evento`, `atualizar_evento`) devolve `{"id": ...}` com
  warning. O 2xx significa que o evento já existe na agenda; levantar aqui
  levaria o processor ao retry e criaria o evento de novo. O `id` é sempre
  conhecido antes da resposta, então nunca existe o estado "escrevi e não
  sei qual é" — que deixaria a Renata sem como remarcar ou cancelar.

**Idempotência da criação.** O `events.insert` aceita um `id` escolhido pelo
cliente; um segundo insert com o mesmo id responde `409 Conflict` em vez de
criar outro evento. Como o id é gerado antes de sair pela rede, um timeout
num POST que o Google já processou é retentável: a segunda tentativa colide
e o 409 é resolvido lendo o evento existente. Sem isso, o caminho de falha
**mais comum** — a rede — duplicaria evento na agenda de trabalho de uma
pessoa real, exatamente o que a tolerância a corpo estranho quis evitar no
caminho raro.

Ressalva registrada, da própria documentação do Google: *"we cannot
guarantee that ID collisions will be detected at event creation time"* por
causa da natureza distribuída do serviço. O 409 é best-effort, então isso
reduz muito a janela de duplicata, mas não é exactly-once. Fechar de vez
exigiria reconciliação por `extendedProperties.private` + `events.list`, que
não vale o custo para um retry que acontece segundos depois da primeira
tentativa, do mesmo processo.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import structlog

logger = structlog.get_logger()

TIMEOUT = httpx.Timeout(30.0)

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

FUSO = ZoneInfo("America/Sao_Paulo")
TIMEZONE_NOME = "America/Sao_Paulo"

# Renova antes de expirar de fato: um token que vence no meio do voo faria a
# operação falhar com 401 sem ninguém para reagir.
MARGEM_RENOVACAO_SEGUNDOS = 60

# Fallback quando o corpo do token não traz `expires_in` — o contrato do
# Google é 3600s, mas confiar em campo ausente é como o KeyError nasce.
EXPIRACAO_PADRAO_SEGUNDOS = 3600

# Teto de eventos por listagem. A janela que a Renata consulta é de poucos
# dias; sem teto, uma agenda cheia pagaria paginação que ninguém lê.
MAX_RESULTS_PADRAO = 250

# O Google devolve 409 no insert quando o `id` fornecido pelo cliente já
# existe no calendário. É o que torna a criação idempotente: o retry não
# duplica, ele colide.
HTTP_CONFLITO = 409

# Sentinela de `GoogleCalendarError.status_code` para falha de transporte —
# timeout, DNS, conexão recusada. Não houve resposta HTTP, então nenhum
# código real cabe, e distinguir isso importa: só neste caso o cliente sabe
# que **não sabe** se o Google processou a requisição.
STATUS_SEM_RESPOSTA = 0

# Caracteres válidos num id de evento fornecido pelo cliente: base32hex.
ALFABETO_EVENT_ID = "abcdefghijklmnopqrstuv0123456789"
EVENT_ID_MIN = 5
EVENT_ID_MAX = 1024


class GoogleCalendarError(Exception):
    """Erro em qualquer operação contra o Google Calendar ou o endpoint OAuth.

    **`detail` é para log, nunca para o usuário final.** Ele carrega o corpo
    cru da resposta (truncado em 500 caracteres) porque é o que permite
    diagnosticar um `invalid_grant` ou uma quota estourada. Repassar
    `str(exc)` para o texto que a Renata manda ao lead entregaria mensagem de
    erro do Google — e eventualmente o e-mail da agenda ou detalhe de
    projeto do Google Cloud — no WhatsApp de quem só queria marcar um
    horário. As tools da Task 5 devem traduzir para uma frase própria e
    deixar o `detail` no `logger`.

    `status_code` é `STATUS_SEM_RESPOSTA` quando não houve resposta HTTP
    (timeout, DNS, conexão recusada).
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        if status_code == STATUS_SEM_RESPOSTA:
            super().__init__(f"Google Calendar não respondeu: {detail}")
        else:
            super().__init__(f"Google Calendar respondeu {status_code}: {detail}")


class GoogleCalendarClient:
    """Cliente assíncrono do Google Calendar API v3.

    Args:
        client_id: client ID do OAuth Client.
        client_secret: client secret do OAuth Client.
        refresh_token: refresh token do usuário dono da agenda.
        calendar_id: id do calendário (o e-mail da agenda de agendamento).
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str,
    ):
        faltando = [
            nome
            for nome, valor in (
                ("client_id", client_id),
                ("client_secret", client_secret),
                ("refresh_token", refresh_token),
                ("calendar_id", calendar_id),
            )
            if not valor
        ]
        if faltando:
            raise ValueError(
                f"GoogleCalendarClient exige {', '.join(faltando)} preenchido(s)"
            )

        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.calendar_id = calendar_id

        self._access_token: str | None = None
        self._expira_em: float = 0.0
        self._lock = asyncio.Lock()
        self._transport: httpx.AsyncBaseTransport | None = None

    # --- Autenticação ---------------------------------------------------

    async def obter_access_token(self, token_invalido: str | None = None) -> str:
        """Access token válido, renovando pelo refresh token quando preciso.

        Dois caminhos, e **os dois deduplicam**:

        - partida a frio / token vencido (`token_invalido=None`): quem chega
          depois do lock revalida `_token_valido()` e sai com o token que o
          primeiro acabou de buscar.
        - reação a 401 (`token_invalido=<token que a API recusou>`): quem
          chega depois do lock compara o token guardado com o que falhou. Se
          alguém já trocou, o guardado serve — não há por que renovar de novo.

        A versão anterior tinha um `forcar=True` que **pulava** a dupla
        checagem, e com isso o lock só serializava os refreshes em vez de
        deduplicá-los: cinco operações em voo levando 401 ao mesmo tempo
        rendiam cinco renovações enfileiradas (mais a inicial). Justamente o
        cenário que o retry existe para cobrir — token revogado com várias
        tools em voo — era o que estourava o endpoint de token. A Renata tem
        cinco tools de calendário e o SOP manda consultar a agenda duas vezes
        antes de agendar.
        """
        if token_invalido is None and self._token_valido():
            return self._access_token  # type: ignore[return-value]

        async with self._lock:
            if token_invalido is None:
                if self._token_valido():
                    return self._access_token  # type: ignore[return-value]
            elif self._access_token and self._access_token != token_invalido:
                # Outro chamador já substituiu o token recusado enquanto este
                # esperava o lock. Aproveita em vez de gastar outra renovação.
                logger.info("gcal_token_ja_renovado_por_outro")
                return self._access_token

            return await self._renovar_access_token()

    def _token_valido(self) -> bool:
        return bool(self._access_token) and time.monotonic() < self._expira_em

    async def _renovar_access_token(self) -> str:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=TIMEOUT
        ) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code >= 400:
            # O corpo do endpoint de token carrega `error`/`error_description`
            # (ex: "invalid_grant" quando o OAuth Client foi rotacionado). É a
            # informação que diz se o conserto é reautorizar ou só tentar de novo.
            detail = response.text[:500]
            logger.error(
                "gcal_token_falhou",
                status_code=response.status_code,
                detail=detail,
            )
            raise GoogleCalendarError(response.status_code, detail)

        dados = _safe_json(response)
        if not isinstance(dados, dict):
            detail = f"corpo do token não é objeto JSON: {response.text[:200]!r}"
            logger.error(
                "gcal_token_resposta_invalida",
                status_code=response.status_code,
                detail=detail,
            )
            raise GoogleCalendarError(response.status_code, detail)

        access_token = dados.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            detail = "resposta do token sem campo access_token"
            logger.error(
                "gcal_token_sem_access_token",
                status_code=response.status_code,
                campos=sorted(dados.keys()),
            )
            raise GoogleCalendarError(response.status_code, detail)

        expira_em = _segundos_de_expiracao(dados.get("expires_in"))
        self._access_token = access_token
        self._expira_em = time.monotonic() + max(
            expira_em - MARGEM_RENOVACAO_SEGUNDOS, 0
        )

        logger.info("gcal_token_renovado", expires_in=expira_em)
        return access_token

    # --- Operações -------------------------------------------------------

    async def listar_eventos(
        self,
        inicio: datetime,
        fim: datetime,
        max_results: int = MAX_RESULTS_PADRAO,
    ) -> list[dict[str, Any]]:
        """Eventos que cruzam a janela `[inicio, fim)`, ordenados por início.

        `singleEvents=true` expande recorrências em ocorrências concretas —
        sem isso um compromisso semanal apareceria uma vez só, com a data da
        primeira ocorrência, e a Renata ofereceria um horário ocupado.

        Corpo irreconhecível levanta em vez de devolver lista vazia: ver o
        docstring do módulo.
        """
        params = {
            "timeMin": formatar_iso(inicio),
            "timeMax": formatar_iso(fim),
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeZone": TIMEZONE_NOME,
            "maxResults": str(max_results),
        }

        dados = await self._requisitar(
            "GET", self._eventos_url(), operacao="listar_eventos", params=params
        )
        corpo = self._exigir_objeto(dados, "listar_eventos")

        itens = corpo.get("items")
        if not isinstance(itens, list):
            detail = "resposta de listagem sem lista em `items`"
            logger.error(
                "gcal_listagem_sem_items",
                operacao="listar_eventos",
                campos=sorted(corpo.keys()),
            )
            raise GoogleCalendarError(200, detail)

        eventos = [item for item in itens if isinstance(item, dict)]
        logger.info(
            "gcal_eventos_listados",
            calendar_id=self.calendar_id,
            time_min=params["timeMin"],
            time_max=params["timeMax"],
            total=len(eventos),
        )
        return eventos

    async def obter_evento(self, event_id: str) -> dict[str, Any]:
        """Um evento pelo id. Leitura — corpo estranho levanta."""
        dados = await self._requisitar(
            "GET",
            f"{self._eventos_url()}/{event_id}",
            operacao="obter_evento",
        )
        return self._exigir_objeto(dados, "obter_evento")

    async def criar_evento(
        self,
        summary: str,
        inicio: datetime,
        fim: datetime,
        participantes: list[str] | None = None,
        descricao: str | None = None,
        notificar: bool = True,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Cria o evento e devolve o corpo do Google. **O `id` sempre volta.**

        `participantes` vira `attendees` — é o que faz o convite chegar no
        e-mail do lead. `notificar` liga `sendUpdates=all`; desligar cria o
        evento sem avisar ninguém.

        **Idempotência.** O `events.insert` do Google aceita um `id` escolhido
        pelo cliente, e um segundo insert com o mesmo id responde
        `409 Conflict` em vez de criar outro evento. O cliente gera esse id
        (`gerar_event_id`) antes de sair pela rede, então:

        - um timeout no POST que o Google **já processou** é retentável sem
          medo: a segunda tentativa colide em 409, e o 409 é resolvido lendo
          o evento que já existe. Sem isso, o caminho de falha mais comum
          (rede) criaria dois eventos na agenda de trabalho de uma pessoa
          real, e o lead receberia confirmação de um horário duplicado.
        - o `id` é conhecido antes da resposta, então mesmo um 2xx com corpo
          irreconhecível devolve `{"id": ...}` em vez de `{}`. Não existe
          mais o estado "criei e não sei qual é" — que deixaria a Renata sem
          como remarcar ou cancelar depois.

        `event_id` pode ser fornecido por quem chama (a Task 5 pode derivar
        um id determinístico do lead, tornando a operação idempotente
        também entre turnos). Precisa ser base32hex — ver `validar_event_id`.

        Corpo irreconhecível com 2xx **não** levanta: o evento está na
        agenda, e levantar aqui levaria o processor ao retry. Ver o docstring
        do módulo.
        """
        event_id = validar_event_id(event_id) if event_id else gerar_event_id()

        corpo = self._corpo_evento(
            summary=summary,
            inicio=inicio,
            fim=fim,
            participantes=participantes,
            descricao=descricao,
        )
        corpo["id"] = event_id

        params = {"sendUpdates": "all" if notificar else "none"}
        dados: Any = None

        for tentativa in (1, 2):
            try:
                dados = await self._requisitar(
                    "POST",
                    self._eventos_url(),
                    operacao="criar_evento",
                    params=params,
                    json=corpo,
                )
                break
            except GoogleCalendarError as exc:
                if exc.status_code == HTTP_CONFLITO:
                    # O id já existe: ou é o retry desta mesma criação, ou
                    # outra execução já marcou este horário. Nos dois casos o
                    # evento desejado está lá — ler é melhor que duplicar.
                    logger.info(
                        "gcal_evento_ja_existia",
                        event_id=event_id,
                        tentativa=tentativa,
                    )
                    return await self.obter_evento(event_id)

                if exc.status_code == STATUS_SEM_RESPOSTA and tentativa == 1:
                    logger.warning(
                        "gcal_criar_evento_repetindo_apos_falha_de_rede",
                        event_id=event_id,
                        detail=exc.detail,
                    )
                    continue

                raise

        evento = self._objeto_tolerante(
            dados, "criar_evento", fallback={"id": event_id}
        )
        logger.info(
            "gcal_evento_criado",
            calendar_id=self.calendar_id,
            event_id=evento.get("id", event_id),
            inicio=corpo["start"]["dateTime"],
        )
        return evento

    async def atualizar_evento(
        self,
        event_id: str,
        summary: str | None = None,
        inicio: datetime | None = None,
        fim: datetime | None = None,
        participantes: list[str] | None = None,
        descricao: str | None = None,
        notificar: bool = True,
    ) -> dict[str, Any]:
        """Atualiza campos do evento via PATCH (reagendamento).

        PATCH e não PUT: o `update` do Google substitui o recurso inteiro, e
        remarcar o horário apagaria `attendees` e `description` que não
        fossem reenviados. Campos `None` simplesmente não vão no corpo.

        Naturalmente idempotente — reaplicar o mesmo PATCH leva ao mesmo
        estado —, então uma falha de rede aqui é retentável pelo processor
        sem tratamento especial, ao contrário do insert. Corpo estranho com
        2xx devolve `{"id": event_id}`: a identidade veio de quem chamou.
        """
        corpo: dict[str, Any] = {}
        if summary is not None:
            corpo["summary"] = summary
        if descricao is not None:
            corpo["description"] = descricao
        if inicio is not None:
            corpo["start"] = _instante(inicio)
        if fim is not None:
            corpo["end"] = _instante(fim)
        if participantes is not None:
            corpo["attendees"] = [{"email": email} for email in participantes]

        if not corpo:
            raise ValueError("atualizar_evento exige pelo menos um campo a alterar")

        dados = await self._requisitar(
            "PATCH",
            f"{self._eventos_url()}/{event_id}",
            operacao="atualizar_evento",
            params={"sendUpdates": "all" if notificar else "none"},
            json=corpo,
        )

        evento = self._objeto_tolerante(
            dados, "atualizar_evento", fallback={"id": event_id}
        )
        logger.info(
            "gcal_evento_atualizado",
            calendar_id=self.calendar_id,
            event_id=event_id,
            campos=sorted(corpo.keys()),
        )
        return evento

    async def deletar_evento(self, event_id: str, notificar: bool = True) -> None:
        """Cancela o evento. Idempotente: 404/410 conta como já cancelado.

        O Google devolve 410 Gone para um evento já deletado e 404 para id
        inexistente. Nos dois casos o estado desejado — não existe evento —
        já vale, e levantar faria a Renata dizer ao lead que o cancelamento
        falhou quando ele já estava feito.
        """
        await self._requisitar(
            "DELETE",
            f"{self._eventos_url()}/{event_id}",
            operacao="deletar_evento",
            params={"sendUpdates": "all" if notificar else "none"},
            status_tolerados=(404, 410),
        )
        logger.info(
            "gcal_evento_deletado",
            calendar_id=self.calendar_id,
            event_id=event_id,
        )

    # --- Infraestrutura --------------------------------------------------

    def _eventos_url(self) -> str:
        # O calendar_id é um e-mail e vira segmento de path — `quote` com
        # `safe=""` cobre o `@` e qualquer id de agenda secundária com `#`.
        calendario = quote(self.calendar_id, safe="")
        return f"{CALENDAR_API_BASE}/calendars/{calendario}/events"

    def _corpo_evento(
        self,
        summary: str,
        inicio: datetime,
        fim: datetime,
        participantes: list[str] | None,
        descricao: str | None,
    ) -> dict[str, Any]:
        corpo: dict[str, Any] = {
            "summary": summary,
            "start": _instante(inicio),
            "end": _instante(fim),
        }
        if descricao:
            corpo["description"] = descricao
        if participantes:
            corpo["attendees"] = [{"email": email} for email in participantes]
        return corpo

    async def _requisitar(
        self,
        metodo: str,
        url: str,
        operacao: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        status_tolerados: tuple[int, ...] = (),
    ) -> Any:
        """Chamada autenticada, com uma retentativa após 401.

        O 401 acontece quando o access token guardado vence antes da margem
        (relógio parado numa VM suspensa, revogação pontual). Renovar e
        repetir uma vez é mais barato que devolver erro ao agente no meio de
        um agendamento. Um segundo 401 é problema de credencial, não de
        expiração, e sobe.
        """
        token = await self.obter_access_token()

        response = await self._enviar(metodo, url, token, params, json)

        if response.status_code == 401:
            logger.warning(
                "gcal_401_renovando_token",
                operacao=operacao,
                detail=response.text[:200],
            )
            token = await self.obter_access_token(token_invalido=token)
            response = await self._enviar(metodo, url, token, params, json)

        if response.status_code in status_tolerados:
            logger.info(
                "gcal_status_tolerado",
                operacao=operacao,
                status_code=response.status_code,
            )
            return None

        if response.status_code >= 400:
            detail = response.text[:500]
            logger.error(
                "gcal_requisicao_falhou",
                operacao=operacao,
                metodo=metodo,
                status_code=response.status_code,
                detail=detail,
            )
            raise GoogleCalendarError(response.status_code, detail)

        return _safe_json(response)

    async def _enviar(
        self,
        metodo: str,
        url: str,
        token: str,
        params: dict[str, str] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        """Requisição autenticada. Falha de transporte vira `GoogleCalendarError`.

        Deixar um `httpx.TimeoutException` subir cru daria ao processor um
        `Exception` genérico → `mark_failed` → retry, sem ninguém saber que a
        requisição pode ter sido processada. Com `STATUS_SEM_RESPOSTA` quem
        chama consegue distinguir "não deu certo" de "não sei se deu certo",
        que é a diferença entre um retry seguro e um evento duplicado.
        """
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT
            ) as client:
                return await client.request(
                    metodo,
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    params=params,
                    json=json,
                )
        except httpx.RequestError as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.error("gcal_falha_de_transporte", metodo=metodo, detail=detail)
            raise GoogleCalendarError(STATUS_SEM_RESPOSTA, detail) from exc

    def _exigir_objeto(self, dados: Any, operacao: str) -> dict[str, Any]:
        """Corpo de leitura precisa ser objeto JSON — senão levanta com log."""
        if isinstance(dados, dict):
            return dados

        detail = f"corpo inesperado em {operacao} (não é objeto JSON)"
        logger.error(
            "gcal_resposta_invalida",
            operacao=operacao,
            tipo=type(dados).__name__,
        )
        raise GoogleCalendarError(200, detail)

    def _objeto_tolerante(
        self, dados: Any, operacao: str, fallback: dict[str, Any]
    ) -> dict[str, Any]:
        """Corpo de escrita irreconhecível cai no `fallback`, não em exceção.

        O `fallback` carrega o `id` do evento, que quem chama sempre conhece
        — foi gerado antes do POST, ou passado no PATCH. Antes esta função
        devolvia `{}`, e o chamador ficava sem distinguir "criou e não sei o
        id" de "não criou": o evento existia na agenda do Silvio e a Renata
        não tinha como remarcar nem cancelar depois.
        """
        if isinstance(dados, dict):
            return dados

        logger.warning(
            "gcal_escrita_resposta_inesperada",
            operacao=operacao,
            tipo=type(dados).__name__,
            fallback=fallback,
        )
        return dict(fallback)


def gerar_event_id() -> str:
    """Id de evento válido para o `events.insert`, no formato do Google.

    O id precisa ser base32hex (`a-v` e `0-9`), de 5 a 1024 caracteres, e
    único **por calendário**. `uuid4().hex` são 32 caracteres de `0-9a-f`,
    subconjunto próprio do alfabeto — e a própria documentação do Google
    recomenda "an established UUID algorithm such as one described in
    RFC4122" justamente para minimizar colisão.

    Gerar o id no cliente é o que torna a criação idempotente: ver
    `GoogleCalendarClient.criar_evento`.
    """
    return uuid.uuid4().hex


def validar_event_id(event_id: str) -> str:
    """Recusa id que o Google rejeitaria, com mensagem que diz o porquê.

    Existe para quem quiser um id determinístico (derivado do lead, por
    exemplo) em vez do aleatório: um telefone com `+` ou um e-mail com `@`
    passariam pelo tipo e morreriam num 400 no meio do agendamento.
    Maiúsculas e as letras `w`-`z` estão fora do base32hex.
    """
    if not EVENT_ID_MIN <= len(event_id) <= EVENT_ID_MAX:
        raise ValueError(
            f"event_id deve ter entre {EVENT_ID_MIN} e {EVENT_ID_MAX} "
            f"caracteres, recebido {len(event_id)}"
        )

    invalidos = sorted(set(event_id) - set(ALFABETO_EVENT_ID))
    if invalidos:
        raise ValueError(
            "event_id deve ser base32hex (a-v e 0-9); caracteres inválidos: "
            f"{''.join(invalidos)!r}"
        )

    return event_id


def formatar_iso(momento: datetime) -> str:
    """ISO 8601 no fuso de São Paulo — `2026-07-28T09:00:00-03:00`.

    Datetime sem tzinfo é lido como relógio de parede de São Paulo, nunca
    como o fuso do processo: em produção o container roda em UTC e a mesma
    entrada renderia três horas de diferença. Mesma convenção de
    `elevec_sdr.contexto.formatar_data_hoje`.
    """
    if momento.tzinfo is None:
        return momento.replace(tzinfo=FUSO).isoformat()
    return momento.astimezone(FUSO).isoformat()


def _instante(momento: datetime) -> dict[str, str]:
    return {"dateTime": formatar_iso(momento), "timeZone": TIMEZONE_NOME}


def _segundos_de_expiracao(bruto: Any) -> int:
    """`expires_in` como int, com fallback — o campo já veio como string."""
    try:
        segundos = int(bruto)
    except (TypeError, ValueError):
        logger.warning("gcal_expires_in_invalido", valor=repr(bruto))
        return EXPIRACAO_PADRAO_SEGUNDOS

    return segundos if segundos > 0 else EXPIRACAO_PADRAO_SEGUNDOS


def _safe_json(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except Exception:
        return None
