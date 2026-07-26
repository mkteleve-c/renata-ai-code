"""Cliente assíncrono para envio de mensagens via uazapi (uazapiGO).

A uazapi é uma API HTTP não-oficial baseada em Baileys que expõe um servidor
por instância (subdomínio próprio, ex: https://meucliente.uazapi.com).
A autenticação por instância é via header `token` com o token retornado em
/instance/init — esse token chega para nós via payload do webhook.

Mantém a mesma interface pública dos demais clientes (TwilioClient/MetaClient):
- send_message(to, body, token=None) -> str
- send_typing(to, message_sid=None, token=None) -> bool

`token` é um override opcional por chamada. Quando preenchido, sobrescreve
o `instance_token` configurado no construtor — útil porque a uazapi entrega
o token da instância ativa em cada webhook (suporte multi-instância e
rotação sem reiniciar o worker).

Suporta `delivery_mode="mock"` para simular envio sem consumir a API real.

Uso:
    from whatsapp_langchain.worker.uazapi_client import UazapiClient

    client = UazapiClient(base_url="https://meucliente.uazapi.com")
    msg_id = await client.send_message(
        to="+5511999999999", body="Olá!", token="<token vindo do webhook>"
    )
"""

import uuid

import httpx
import structlog

logger = structlog.get_logger()

# A doc não cita um limite explícito de chars para /send/text, mas o WhatsApp
# tem teto prático de ~4096 caracteres por mensagem. Mantemos o mesmo do Meta.
UAZAPI_TEXT_BODY_LIMIT = 4096

# Duração padrão do indicador "digitando" (ms). Máx aceito pela uazapi: 5min.
UAZAPI_TYPING_DELAY_MS = 25_000


class UazapiSendError(Exception):
    """Erro ao enviar mensagem via uazapi."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"uazapi error {status_code}: {detail}")


class UazapiClient:
    """Cliente assíncrono para envio via uazapi.

    Args:
        base_url: URL base da instância (ex: https://meucliente.uazapi.com).
        instance_token: Fallback estático (opcional). Quando o webhook entrega
            o token na mensagem, o caller passa via `send_message(token=...)`
            e este valor é ignorado. Útil só para deploys com 1 instância
            fixa onde o operador prefere setar via env.
        delivery_mode: "real" (envia) ou "mock" (simula).
    """

    def __init__(
        self,
        base_url: str,
        instance_token: str = "",
        *,
        delivery_mode: str = "real",
    ):
        if delivery_mode not in {"real", "mock"}:
            raise ValueError(
                "delivery_mode deve ser 'real' ou 'mock', "
                f"recebido: {delivery_mode}"
            )

        if delivery_mode == "real" and not base_url:
            raise ValueError("base_url não pode ser vazio")

        self.base_url = base_url.rstrip("/")
        self.instance_token = instance_token
        self.delivery_mode = delivery_mode
        self.send_text_url = f"{self.base_url}/send/text"
        self.presence_url = f"{self.base_url}/message/presence"

    def _resolve_token(self, override: str | None) -> str:
        """Token efetivo da chamada: override > instance_token (estático)."""
        if override and override.strip():
            return override.strip()
        return self.instance_token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "token": token,
            "Content-Type": "application/json",
        }

    async def send_message(
        self,
        to: str,
        body: str,
        token: str | None = None,
    ) -> str:
        """Envia mensagem de texto via /send/text. Retorna o ID retornado pela API.

        A uazapi aceita 'number' em E.164 com ou sem '+' e com ou sem o sufixo
        '@s.whatsapp.net'. Normalizamos para apenas dígitos.

        Args:
            to: Número destino em E.164 (ex: +5511999999999).
            body: Texto da mensagem.
            token: Token da instância para esta chamada. Quando vazio, cai no
                fallback estático configurado no construtor.

        Returns:
            ID da mensagem enviada (campo 'id' do response, quando presente).
            Em modo mock, retorna um UUID prefixado.

        Raises:
            UazapiSendError: Se a API retornar erro (4xx/5xx).
            ValueError: Se nenhum token é resolvido em modo real.
        """
        chunks = split_message_body(body)
        chunk_count = len(chunks)

        if chunk_count > 1:
            logger.info(
                "uazapi_message_chunked",
                to=to,
                original_length=len(body),
                chunk_count=chunk_count,
            )

        normalized_to = (
            to.lstrip("+").replace("whatsapp:", "").split("@", 1)[0]
        )

        if self.delivery_mode == "mock":
            last_id = ""
            for idx, chunk in enumerate(chunks, start=1):
                last_id = f"uazapi-mock-{uuid.uuid4().hex[:24]}"
                logger.info(
                    "uazapi_message_mocked",
                    to=normalized_to,
                    id=last_id,
                    body_length=len(chunk),
                    chunk_index=idx,
                    chunk_count=chunk_count,
                )
            return last_id

        effective_token = self._resolve_token(token)
        if not effective_token:
            raise ValueError(
                "uazapi: nenhum token disponível para a chamada — verifique se "
                "o webhook entrega o token no payload ou configure "
                "UAZAPI_INSTANCE_TOKEN como fallback."
            )

        last_id = ""
        async with httpx.AsyncClient() as http:
            for idx, chunk in enumerate(chunks, start=1):
                response = await http.post(
                    self.send_text_url,
                    headers=self._headers(effective_token),
                    json={
                        "number": normalized_to,
                        "text": chunk,
                    },
                    timeout=15.0,
                )

                if not response.is_success:
                    detail = response.text[:500]
                    logger.error(
                        "uazapi_send_failed",
                        to=normalized_to,
                        status_code=response.status_code,
                        detail=detail,
                        chunk_index=idx,
                        chunk_count=chunk_count,
                        body_length=len(chunk),
                    )
                    raise UazapiSendError(response.status_code, detail)

                data = _safe_json(response)
                last_id = _extract_message_id(data) or ""

                logger.info(
                    "uazapi_message_sent",
                    to=normalized_to,
                    id=last_id or None,
                    body_length=len(chunk),
                    chunk_index=idx,
                    chunk_count=chunk_count,
                )

        return last_id

    async def send_typing(
        self,
        to: str,
        message_sid: str | None = None,
        token: str | None = None,
    ) -> bool:
        """Envia indicador de "digitando..." via /message/presence (composing).

        A uazapi é assíncrona — uma única chamada mantém a presença por até 5min,
        re-emitindo a cada 10s. Cancelada automaticamente quando enviamos uma
        mensagem para o mesmo chat.

        message_sid é ignorado (não há marcação de "lida" atrelada a um messageid
        neste endpoint) — mantemos a assinatura compatível com os outros clientes
        para duck typing no processor.
        """
        if self.delivery_mode == "mock":
            logger.debug("uazapi_typing_skipped", to=to, reason="mock_mode")
            return False

        effective_token = self._resolve_token(token)
        if not effective_token:
            logger.debug("uazapi_typing_skipped", to=to, reason="no_token")
            return False

        normalized_to = (
            to.lstrip("+").replace("whatsapp:", "").split("@", 1)[0]
        )

        try:
            async with httpx.AsyncClient() as http:
                response = await http.post(
                    self.presence_url,
                    headers=self._headers(effective_token),
                    json={
                        "number": normalized_to,
                        "presence": "composing",
                        "delay": UAZAPI_TYPING_DELAY_MS,
                    },
                    timeout=5.0,
                )

            if response.is_success:
                logger.info("uazapi_typing_sent", to=normalized_to)
                return True

            logger.warning(
                "uazapi_typing_failed",
                to=normalized_to,
                status_code=response.status_code,
                detail=response.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("uazapi_typing_error", to=normalized_to, error=str(exc))
            return False


def _safe_json(response: httpx.Response) -> dict | list | None:
    try:
        return response.json()
    except Exception:
        return None


def _extract_message_id(data: dict | list | None) -> str | None:
    """Extrai um ID útil da resposta da uazapi.

    A API tem várias formas de retorno conforme endpoint. Tentamos os campos
    mais prováveis em ordem; se nenhum bater, retornamos None.
    """
    if not isinstance(data, dict):
        return None
    for key in ("id", "messageid", "message_id", "messageId"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    message = data.get("message")
    if isinstance(message, dict):
        return _extract_message_id(message)
    return None


def split_message_body(body: str, limit: int = UAZAPI_TEXT_BODY_LIMIT) -> list[str]:
    """Divide mensagens longas para respeitar o limite prático da uazapi."""
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
