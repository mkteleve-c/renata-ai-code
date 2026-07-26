"""Cliente outbound da Evolution API.

A instância desta conta roda integração WHATSAPP-BUSINESS — por baixo é a
Meta Cloud API oficial, com a Evolution fazendo de proxy. A superfície REST
é a mesma da integração Baileys. Por rodar sobre a Cloud API oficial, o teto
de 4096 caracteres por mensagem de texto é confirmado (não uma estimativa
prática como na uazapi) — mensagens acima disso são quebradas em partes.

Não há "digitando…" neste canal, e isso é uma limitação da integração, não
uma escolha nossa. Na integração Baileys o `delay` do sendText emite
presença `composing` durante a espera; na WHATSAPP-BUSINESS o serviço
equivalente (`whatsapp.business.service.ts`) recebe `{delay, presence}` dos
callers e **descarta os dois** — `sendMessageWithTyping` posta direto em
`/messages` — e `sendPresence()` lança
`BadRequestException('Method not available on WhatsApp Business API')`.

Por isso send_typing é no-op: chamar o endpoint de presença renderia HTTP
400 por mensagem, e passar `delay` não produziria indicador nenhum. O
parâmetro `delay_ms` continua exposto porque funciona em instâncias Baileys.

Mídia recebida vem criptografada (herança do Baileys); baixá-la exige
getBase64FromMediaMessage, não um GET na URL.

Suporta `delivery_mode="mock"` para simular envio sem consumir a API real.
"""

import base64
import binascii
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

TIMEOUT = httpx.Timeout(30.0)

# A integração WHATSAPP-BUSINESS é proxy direto da Cloud API oficial da Meta,
# que tem teto confirmado de 4096 caracteres por mensagem de texto.
EVOLUTION_TEXT_BODY_LIMIT = 4096


class EvolutionSendError(Exception):
    """Erro ao enviar mensagem ou baixar mídia via Evolution API."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Evolution respondeu {status_code}: {detail}")


class EvolutionClient:
    """Cliente assíncrono para envio via Evolution API.

    Args:
        base_url: URL base da instância Evolution (ex: https://evolution.host).
        api_key: apikey da instância — autentica envio e download de mídia.
        instance: nome da instância (fixa por deploy).
        delivery_mode: "real" (envia) ou "mock" (simula).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        instance: str,
        delivery_mode: str = "real",
    ):
        if delivery_mode not in {"real", "mock"}:
            raise ValueError(
                f"delivery_mode deve ser 'real' ou 'mock', recebido: {delivery_mode}"
            )

        if delivery_mode == "real" and not (base_url and api_key and instance):
            raise ValueError(
                "base_url, api_key e instance são obrigatórios em modo real"
            )

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.instance = instance
        self.delivery_mode = delivery_mode
        self._transport: httpx.AsyncBaseTransport | None = None

    def _headers(self) -> dict[str, str]:
        return {"apikey": self.api_key, "Content-Type": "application/json"}

    async def send_message(
        self,
        to: str,
        body: str,
        token: str | None = None,
        delay_ms: int = 0,
    ) -> str | None:
        """Envia mensagem de texto via /message/sendText. Retorna key.id.

        Mensagens acima de `EVOLUTION_TEXT_BODY_LIMIT` são quebradas em
        partes e enviadas em sequência, preservando a ordem. O retorno é o
        `id` da última parte enviada — mesmo precedente do `last_id` em
        `uazapi_client.send_message`.

        `token` é ignorado — a Evolution autentica pela apikey da instância,
        não por um token entregue por mensagem como a uazapi. O parâmetro
        existe só para manter a assinatura compatível com os outros clientes
        outbound (duck typing no processor).

        `delay_ms` vai no payload quando maior que zero, mas só tem efeito em
        instâncias Baileys — a integração WHATSAPP-BUSINESS descarta o campo
        (ver docstring do módulo). O processor não o passa hoje.
        """
        if self.delivery_mode == "mock":
            logger.info("evolution_mock_send", to=to, body=body[:80])
            return None

        chunks = split_message_body(body, limit=EVOLUTION_TEXT_BODY_LIMIT)
        chunk_count = len(chunks)

        if chunk_count > 1:
            logger.info(
                "evolution_message_chunked",
                to=to,
                original_length=len(body),
                chunk_count=chunk_count,
            )

        normalized_to = to.lstrip("+")
        last_id: str | None = None

        async with httpx.AsyncClient(
            transport=self._transport, timeout=TIMEOUT
        ) as client:
            for idx, chunk in enumerate(chunks, start=1):
                payload: dict[str, Any] = {"number": normalized_to, "text": chunk}
                if delay_ms:
                    payload["delay"] = delay_ms

                response = await client.post(
                    f"{self.base_url}/message/sendText/{self.instance}",
                    headers=self._headers(),
                    json=payload,
                )

                if response.status_code >= 400:
                    detail = response.text[:500]
                    logger.error(
                        "evolution_send_failed",
                        to=normalized_to,
                        status_code=response.status_code,
                        detail=detail,
                        chunk_index=idx,
                        chunk_count=chunk_count,
                    )
                    raise EvolutionSendError(response.status_code, detail)

                dados = _safe_json(response)
                if not isinstance(dados, dict):
                    detail = (
                        f"corpo inesperado (não é objeto JSON): {response.text[:500]!r}"
                    )
                    logger.error(
                        "evolution_send_invalid_response",
                        to=normalized_to,
                        status_code=response.status_code,
                        detail=detail,
                        chunk_index=idx,
                        chunk_count=chunk_count,
                    )
                    raise EvolutionSendError(response.status_code, detail)

                chunk_id = _extract_message_id(dados)
                if chunk_id is None:
                    detail = f"resposta sem key.id: {response.text[:500]!r}"
                    logger.error(
                        "evolution_send_missing_id",
                        to=normalized_to,
                        status_code=response.status_code,
                        detail=detail,
                        chunk_index=idx,
                        chunk_count=chunk_count,
                    )
                    raise EvolutionSendError(response.status_code, detail)

                last_id = chunk_id
                logger.info(
                    "evolution_message_sent",
                    to=normalized_to,
                    id=last_id,
                    chunk_index=idx,
                    chunk_count=chunk_count,
                )

        return last_id

    async def send_typing(
        self,
        to: str,
        message_id: str | None = None,
        token: str | None = None,
    ) -> bool:
        """No-op deliberado: a integração WHATSAPP-BUSINESS não tem presença.

        `/chat/sendPresence` lança "Method not available on WhatsApp Business
        API" nessa integração, então uma chamada real seria HTTP 400 por
        mensagem. Retorna False (nada enviado) sem tocar a rede — o processor
        trata typing como best-effort e segue para o envio.
        """
        return False

    async def baixar_midia(self, message_key: dict[str, Any]) -> bytes:
        """Baixa e decifra mídia via /chat/getBase64FromMediaMessage.

        A URL de mídia que chega no payload do webhook é criptografada
        (herança do Baileys) — um GET direto nela não devolve o arquivo.
        """
        async with httpx.AsyncClient(
            transport=self._transport, timeout=TIMEOUT
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/getBase64FromMediaMessage/{self.instance}",
                headers=self._headers(),
                json={"message": {"key": message_key}, "convertToMp4": False},
            )

        if response.status_code >= 400:
            detail = response.text[:500]
            logger.error(
                "evolution_download_failed",
                status_code=response.status_code,
                detail=detail,
            )
            raise EvolutionSendError(response.status_code, detail)

        dados = _safe_json(response)
        if not isinstance(dados, dict):
            detail = f"corpo inesperado (não é objeto JSON): {response.text[:500]!r}"
            logger.error(
                "evolution_download_invalid_response",
                status_code=response.status_code,
                detail=detail,
            )
            raise EvolutionSendError(response.status_code, detail)

        b64 = dados.get("base64")
        if not isinstance(b64, str) or not b64:
            detail = f"resposta sem campo base64: {response.text[:500]!r}"
            logger.error(
                "evolution_download_missing_base64",
                status_code=response.status_code,
                detail=detail,
            )
            raise EvolutionSendError(response.status_code, detail)

        try:
            return base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            detail = f"base64 inválido: {exc}"
            logger.error(
                "evolution_download_invalid_base64",
                status_code=response.status_code,
                detail=detail,
            )
            raise EvolutionSendError(response.status_code, detail) from exc


def _safe_json(response: httpx.Response) -> dict[str, Any] | list[Any] | None:
    try:
        return response.json()
    except Exception:
        return None


def _extract_message_id(dados: dict[str, Any]) -> str | None:
    key = dados.get("key")
    if not isinstance(key, dict):
        return None
    msg_id = key.get("id")
    return msg_id if isinstance(msg_id, str) and msg_id else None


def split_message_body(body: str, limit: int = EVOLUTION_TEXT_BODY_LIMIT) -> list[str]:
    """Divide mensagens longas para respeitar o limite da Cloud API (4096 chars)."""
    if limit <= 0:
        raise ValueError("limit deve ser maior que zero")

    if len(body) <= limit:
        return [body]

    chunks: list[str] = []
    remaining = body

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = -1
        for sep in ("\n\n", "\n", " "):
            idx = remaining.rfind(sep, 0, limit + 1)
            if idx > 0:
                split_at = idx
                break

        if split_at <= 0:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]

        chunks.append(chunk)
        remaining = remaining[len(chunk) :].lstrip()

    return chunks
