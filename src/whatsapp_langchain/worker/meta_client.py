"""Cliente assíncrono para envio de mensagens via Meta WhatsApp Cloud API.

Usa httpx para chamadas não-bloqueantes à Graph API. Autenticação via Bearer
token (System User permanent token, com permissão whatsapp_business_messaging).

Mantém a mesma interface pública do TwilioClient (send_message, send_typing)
para que o processor do worker funcione com qualquer um dos dois (duck typing).

Em desenvolvimento local, suporta `delivery_mode="mock"` que simula o envio sem
chamar a API.

Uso:
    from whatsapp_langchain.worker.meta_client import MetaClient

    client = MetaClient(
        phone_number_id="1234567890",
        access_token="EAAxxx...",
    )
    wamid = await client.send_message(to="+5511999999999", body="Olá!")
    await client.send_typing(to="+5511999999999", message_sid="wamid.HBg...")
"""

import uuid

import httpx
import structlog

logger = structlog.get_logger()

GRAPH_BASE_URL = "https://graph.facebook.com"
META_TEXT_BODY_LIMIT = 4096


class MetaSendError(Exception):
    """Erro ao enviar mensagem via Meta Graph API."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Meta Graph API error {status_code}: {detail}")


class MetaClient:
    """Cliente assíncrono para envio de mensagens WhatsApp via Meta Cloud API.

    Args:
        phone_number_id: Phone Number ID do número WhatsApp Business.
        access_token: System User Access Token com permissão whatsapp_business_messaging.
        graph_api_version: Versão da Graph API (default: v23.0).
        delivery_mode: "real" (envia) ou "mock" (simula).
    """

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        *,
        graph_api_version: str = "v23.0",
        delivery_mode: str = "real",
    ):
        if delivery_mode not in {"real", "mock"}:
            raise ValueError(
                "delivery_mode deve ser 'real' ou 'mock', "
                f"recebido: {delivery_mode}"
            )

        if delivery_mode == "real":
            if not phone_number_id:
                raise ValueError("phone_number_id não pode ser vazio")
            if not access_token:
                raise ValueError("access_token não pode ser vazio")

        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.graph_api_version = graph_api_version
        self.delivery_mode = delivery_mode
        self.messages_url = (
            f"{GRAPH_BASE_URL}/{graph_api_version}/{phone_number_id}/messages"
        )

    async def send_message(self, to: str, body: str) -> str:
        """Envia mensagem de texto via Cloud API. Retorna o wamid.

        Args:
            to: Número destino em E.164 (ex: +5511999999999) — o '+' é opcional.
            body: Texto da mensagem.

        Returns:
            wamid (WhatsApp Message ID) da mensagem enviada.

        Raises:
            MetaSendError: Se a API retornar erro (4xx/5xx).
        """
        chunks = split_message_body(body)
        chunk_count = len(chunks)

        if chunk_count > 1:
            logger.info(
                "meta_message_chunked",
                to=to,
                original_length=len(body),
                chunk_count=chunk_count,
            )

        # Meta Cloud API espera 'to' sem '+' nem 'whatsapp:'
        normalized_to = to.lstrip("+").replace("whatsapp:", "")

        if self.delivery_mode == "mock":
            last_wamid = ""
            for idx, chunk in enumerate(chunks, start=1):
                last_wamid = f"wamid.MOCK{uuid.uuid4().hex[:24]}"
                logger.info(
                    "meta_message_mocked",
                    to=normalized_to,
                    wamid=last_wamid,
                    body_length=len(chunk),
                    chunk_index=idx,
                    chunk_count=chunk_count,
                )
            return last_wamid

        last_wamid = ""
        async with httpx.AsyncClient() as http:
            for idx, chunk in enumerate(chunks, start=1):
                response = await http.post(
                    self.messages_url,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": normalized_to,
                        "type": "text",
                        "text": {"body": chunk},
                    },
                    timeout=15.0,
                )

                if not response.is_success:
                    detail = response.text[:500]
                    logger.error(
                        "meta_send_failed",
                        to=normalized_to,
                        status_code=response.status_code,
                        detail=detail,
                        chunk_index=idx,
                        chunk_count=chunk_count,
                        body_length=len(chunk),
                    )
                    raise MetaSendError(response.status_code, detail)

                data = response.json()
                last_wamid = data["messages"][0]["id"]

                logger.info(
                    "meta_message_sent",
                    to=normalized_to,
                    wamid=last_wamid,
                    body_length=len(chunk),
                    chunk_index=idx,
                    chunk_count=chunk_count,
                )

        return last_wamid

    async def send_typing(self, to: str, message_sid: str | None = None) -> bool:
        """Marca a mensagem inbound como lida (Cloud API não tem typing real).

        A Cloud API não expõe typing indicator nativo. O melhor equivalente é
        marcar como lida (status=read), o que mostra os dois checks azuis
        para o usuário enquanto o agente processa.

        Args:
            to: Número destino (apenas para logging).
            message_sid: wamid da mensagem inbound a marcar como lida.

        Returns:
            True se marcou com sucesso, False caso contrário.
        """
        if self.delivery_mode == "mock":
            logger.debug("meta_mark_read_skipped", to=to, reason="mock_mode")
            return False

        if not message_sid:
            logger.debug("meta_mark_read_skipped", to=to, reason="no_message_sid")
            return False

        try:
            async with httpx.AsyncClient() as http:
                response = await http.post(
                    self.messages_url,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "messaging_product": "whatsapp",
                        "status": "read",
                        "message_id": message_sid,
                    },
                    timeout=5.0,
                )

            if response.is_success:
                logger.info("meta_marked_as_read", to=to, wamid=message_sid)
                return True

            logger.warning(
                "meta_mark_read_failed",
                to=to,
                wamid=message_sid,
                status_code=response.status_code,
                detail=response.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("meta_mark_read_error", to=to, error=str(exc))
            return False


def split_message_body(body: str, limit: int = META_TEXT_BODY_LIMIT) -> list[str]:
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
