"""Cliente do Pipedrive — o funil comercial onde o card do lead se move.

Escopo deliberadamente mínimo: **mover um deal de estágio**. É a única coisa
que o workflow `#4 CRM Control` do n8n fazia contra o Pipedrive, e é a única
que a Renata precisa. Cada método a mais aqui seria superfície de escrita
num CRM de produção sem ninguém pedindo.

**Autenticação por header `x-api-token`, nunca pelo `?api_token=`.** Os dois
funcionam (verificado contra a API real), mas o token no query string acaba
dentro de `str(url)` — que é o que vai para `logger.error`, para o
`detail` da exceção e para qualquer traceback. Um segredo de CRM não pode
depender de ninguém lembrar de mascarar a URL na hora de logar.

**`success: false` com HTTP 200 é falha.** O Pipedrive responde 200 com
`{"success": false, "error": "..."}` em parte dos erros de validação. Ler só
o `status_code` faria `mover_card` devolver sucesso com o card parado onde
estava — e o operador comercial descobriria isso pelo funil vazio, não pelo
log.

**Corpo irreconhecível com 2xx não levanta.** Mesma assimetria do
`GoogleCalendarClient`: o PUT já foi aplicado, e levantar aqui levaria o
processor ao retry de uma escrita que já valeu. Vira `warning`.

**`deal_id` é validado como dígitos antes de virar segmento de path.** Ele
sai de `leads_crm.pipedriveid`, que é `TEXT` e foi populado por importação
do banco legado — um valor como `12/../../users` viraria outra rota da API
com o token válido no header. Dígitos e nada mais.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

TIMEOUT = httpx.Timeout(30.0)

PIPEDRIVE_API_BASE = "https://api.pipedrive.com/v1"

# Sentinela de `PipedriveError.status_code` para falha de transporte — não
# houve resposta HTTP, então nenhum código real cabe. Mesmo contrato de
# `shared/google_calendar.STATUS_SEM_RESPOSTA`.
STATUS_SEM_RESPOSTA = 0

_SO_DIGITOS = re.compile(r"^\d+$")


class PipedriveError(Exception):
    """Falha em qualquer operação contra o Pipedrive.

    `detail` é para log, nunca para o lead: carrega o corpo cru da resposta
    (truncado), que pode trazer nome de deal, de pipeline ou de usuário do
    CRM. Quem chama traduz para uma frase própria.
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        if status_code == STATUS_SEM_RESPOSTA:
            super().__init__(f"Pipedrive não respondeu: {detail}")
        else:
            super().__init__(f"Pipedrive respondeu {status_code}: {detail}")


class PipedriveClient:
    """Cliente assíncrono do Pipedrive API v1.

    Args:
        api_token: token de API da conta (header `x-api-token`).
        base_url: base da API. Só muda em teste.
    """

    def __init__(self, api_token: str, base_url: str = PIPEDRIVE_API_BASE):
        if not api_token:
            raise ValueError("PipedriveClient exige api_token preenchido")

        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self._transport: httpx.AsyncBaseTransport | None = None

    async def mover_card(self, deal_id: str, stage_id: int) -> None:
        """Move o deal para `stage_id`. Levanta `PipedriveError` se não mover.

        `PUT /deals/{id}` é idempotente: reaplicar o mesmo `stage_id` leva ao
        mesmo estado. É o que torna seguro repetir a chamada quando a
        gravação no banco falha depois dela — ver `tools/crm.py`.
        """
        alvo = validar_deal_id(deal_id)

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT
            ) as client:
                response = await client.put(
                    f"{self.base_url}/deals/{alvo}",
                    headers={
                        "x-api-token": self.api_token,
                        "Content-Type": "application/json",
                    },
                    json={"stage_id": stage_id},
                )
        except httpx.RequestError as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.error("pipedrive_falha_de_transporte", deal_id=alvo, detail=detail)
            raise PipedriveError(STATUS_SEM_RESPOSTA, detail) from exc

        if response.status_code >= 400:
            detail = response.text[:500]
            logger.error(
                "pipedrive_requisicao_falhou",
                deal_id=alvo,
                stage_id=stage_id,
                status_code=response.status_code,
                detail=detail,
            )
            raise PipedriveError(response.status_code, detail)

        corpo = _safe_json(response)

        if isinstance(corpo, dict) and corpo.get("success") is False:
            # 200 com `success: false`. Sem esta checagem o card ficaria
            # parado e a tool diria que moveu.
            detail = str(corpo.get("error") or "resposta com success=false")[:500]
            logger.error(
                "pipedrive_success_false",
                deal_id=alvo,
                stage_id=stage_id,
                detail=detail,
            )
            raise PipedriveError(response.status_code, detail)

        if not isinstance(corpo, dict):
            # 2xx: o PUT valeu. Levantar levaria o processor ao retry de uma
            # escrita já aplicada. Ver o docstring do módulo.
            logger.warning(
                "pipedrive_resposta_inesperada",
                deal_id=alvo,
                stage_id=stage_id,
                tipo=type(corpo).__name__,
            )

        logger.info("pipedrive_card_movido", deal_id=alvo, stage_id=stage_id)


def validar_deal_id(deal_id: str) -> str:
    """Recusa `pipedriveid` que não seja um id numérico do Pipedrive.

    O valor vem de `leads_crm.pipedriveid`, coluna `TEXT` populada por
    importação. Sem esta checagem, um id com `/` viraria outra rota da API —
    com o token válido no header.
    """
    limpo = (deal_id or "").strip()
    if not _SO_DIGITOS.match(limpo):
        raise ValueError(f"pipedriveid inválido (esperado só dígitos): {deal_id!r}")
    return limpo


def _safe_json(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except Exception:
        return None
