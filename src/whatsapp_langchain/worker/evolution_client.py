"""Cliente outbound da Evolution API.

A instância desta conta roda integração WHATSAPP-BUSINESS — por baixo é a
Meta Cloud API oficial, com a Evolution fazendo de proxy. A superfície REST
é a mesma da integração Baileys.

O parâmetro `delay` do sendText é nativo e em milissegundos: ele mostra
"digitando…" durante a espera. Por isso send_typing é no-op — chamar um
endpoint de presença separado duplicaria o efeito.

Mídia recebida vem criptografada (herança do Baileys); baixá-la exige
getBase64FromMediaMessage, não um GET na URL.

Suporta `delivery_mode="mock"` para simular envio sem consumir a API real.
"""

import base64
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

TIMEOUT = httpx.Timeout(30.0)


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

        `token` é ignorado — a Evolution autentica pela apikey da instância,
        não por um token entregue por mensagem como a uazapi. O parâmetro
        existe só para manter a assinatura compatível com os outros clientes
        outbound (duck typing no processor).
        """
        if self.delivery_mode == "mock":
            logger.info("evolution_mock_send", to=to, body=body[:80])
            return None

        payload: dict[str, Any] = {"number": to.lstrip("+"), "text": body}
        if delay_ms:
            payload["delay"] = delay_ms

        async with httpx.AsyncClient(
            transport=self._transport, timeout=TIMEOUT
        ) as client:
            response = await client.post(
                f"{self.base_url}/message/sendText/{self.instance}",
                headers=self._headers(),
                json=payload,
            )

        if response.status_code >= 400:
            detail = response.text[:300]
            logger.error(
                "evolution_send_failed",
                to=payload["number"],
                status_code=response.status_code,
                detail=detail,
            )
            raise EvolutionSendError(response.status_code, detail)

        dados = response.json()
        msg_id = (dados.get("key") or {}).get("id")
        logger.info("evolution_message_sent", to=payload["number"], id=msg_id)
        return msg_id

    async def send_typing(
        self,
        to: str,
        message_id: str | None = None,
        token: str | None = None,
    ) -> bool:
        """No-op: o `delay` nativo do sendText já exibe "digitando…"."""
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
            detail = response.text[:300]
            logger.error(
                "evolution_download_failed",
                status_code=response.status_code,
                detail=detail,
            )
            raise EvolutionSendError(response.status_code, detail)

        return base64.b64decode(response.json()["base64"])
