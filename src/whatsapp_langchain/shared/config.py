"""Configuração centralizada via variáveis de ambiente.

Usa pydantic-settings para carregar, validar e tipar todas as configurações
do projeto a partir de variáveis de ambiente ou arquivo .env.

Uso:
    from whatsapp_langchain.shared.config import settings

    print(settings.database_url)
    print(settings.rate_limit_per_hour)

A maior parte das configurações tem defaults sensatos para desenvolvimento local.
Segredos compartilhados do painel/admin devem ser preenchidos explicitamente.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_PRODUCTION_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Configurações do projeto carregadas de variáveis de ambiente.

    Cada campo corresponde a uma env var (case-insensitive).
    Ex: database_url → DATABASE_URL
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = (
        "postgresql://postgres:postgres@localhost:5432/whatsapp_langchain"
    )

    # --- Environment ---
    # "development" (default) ou "production" — controla comportamentos como
    # exposicao do webhook sincrono (desabilitado em production)
    environment: str = "development"

    # --- Server ---
    port: int = 8000
    log_level: str = "info"
    log_json: bool = False  # True em prod para logs estruturados

    # --- Messaging channel ---
    # O canal de cada mensagem é decidido pelo webhook que a recebeu e
    # persistido em message_queue.channel. O worker mantém clientes outbound
    # de todos os canais "habilitados" (com credenciais preenchidas) e
    # seleciona pelo channel da mensagem na hora de responder.
    # Não há mais env MESSAGING_CHANNEL — cada canal é habilitado pela
    # presença das próprias credenciais.

    # --- Outbound mode (compartilhado entre Twilio e Meta) ---
    # "real" envia mensagens de verdade; "mock" apenas simula (logs).
    # Default por ambiente: production=real, dev=mock.
    outbound_mode: str = ""

    # --- Twilio ---
    # Inbound (validação de assinatura no webhook)
    validate_twilio_signature: bool = False
    twilio_auth_token: str = ""
    twilio_webhook_url: str = ""

    # Outbound (envio de mensagens pelo worker via API Key)
    # Legado — preferir outbound_mode acima. Mantido por retrocompatibilidade.
    twilio_outbound_mode: str = ""
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    twilio_from_number: str = ""

    # --- Meta WhatsApp Cloud API ---
    # Inbound (handshake + validação de assinatura)
    meta_validate_signature: bool = True
    meta_verify_token: str = ""
    meta_app_secret: str = ""
    # Outbound (Graph API)
    meta_phone_number_id: str = ""
    meta_access_token: str = ""
    meta_graph_api_version: str = "v23.0"

    # --- uazapi (uazapiGO — API HTTP não-oficial, baseada em Baileys) ---
    # Cada instância tem subdomínio próprio (ex: https://meucliente.uazapi.com).
    # O token da instância (header 'token') chega via payload do webhook e é
    # persistido em message_queue.outbound_token; o worker o usa no envio.
    # uazapi_instance_token aqui é apenas fallback estático opcional, útil
    # para deploys com 1 instância fixa onde o operador prefere setar via env.
    uazapi_base_url: str = ""
    uazapi_instance_token: str = ""

    # --- Evolution API (integração WHATSAPP-BUSINESS: Meta Cloud API por baixo) ---
    # A instância é fixa por deploy; o apikey autentica tanto o envio quanto o
    # download de mídia decifrada.
    evolution_base_url: str = ""
    evolution_api_key: str = ""
    evolution_instance: str = ""
    # Segredo opcional do webhook inbound. A Evolution não assina o body;
    # se preenchido aqui e configurado como header no webhook da instância,
    # a rota passa a exigi-lo. Vazio = rota aberta (default de dev).
    evolution_webhook_secret: str = ""

    # --- Rate Limit ---
    rate_limit_per_hour: int = 30

    # --- Debounce ---
    message_buffer_seconds: float = 2.0

    # --- LLM (OpenRouter) ---
    # Todas as chamadas LLM, embeddings e transcrição usam OpenRouter
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "x-ai/grok-4.1-fast"
    # Modelo dedicado ao pré-processamento de mídia (imagem/áudio)
    openrouter_midia_model: str = "google/gemini-2.5-flash-lite"

    # --- LLM Rate Limit ---
    llm_rate_limit_requests_per_second: float = 0.5
    llm_rate_limit_max_burst: int = 10

    # --- Worker ---
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 60
    max_attempts: int = 3

    # --- Media ---
    media_image_enabled: bool = True
    media_audio_enabled: bool = True

    # --- Context Management (migrado do .env manual) ---
    context_strategy: str = "trim"
    trim_keep_turns: int = 5
    summarize_trigger_tokens: int = 4000
    summarize_keep_messages: int = 10
    summarize_model: str = "x-ai/grok-4.1-fast"

    # --- Internal Service Token ---
    # Token compartilhado entre frontend e API para proteger rotas administrativas.
    # Preencha também em desenvolvimento; em produção, use um token forte.
    internal_service_token: str = ""

    # --- Semantic Memory (LangGraph Store) ---
    memory_enabled: bool = True
    # Nome do modelo no OpenRouter (sem prefixo "openai:")
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dims: int = 1536
    memory_search_limit: int = 5

    @property
    def resolved_outbound_mode(self) -> str:
        """Resolve o modo outbound (real|mock) compartilhado entre Twilio e Meta.

        Precedência: outbound_mode > twilio_outbound_mode (legacy) > default por ambiente.
        """
        mode = self.outbound_mode.strip().lower()
        if mode:
            return mode

        legacy = self.twilio_outbound_mode.strip().lower()
        if legacy:
            return legacy

        return "real" if self.environment == "production" else "mock"

    @property
    def resolved_twilio_outbound_mode(self) -> str:
        """Alias retrocompatível — preferir resolved_outbound_mode."""
        return self.resolved_outbound_mode

    @property
    def is_production(self) -> bool:
        """Indica se a aplicacao esta rodando em modo production."""
        return self.environment.strip().lower() == "production"

    # --- Channel enablement helpers ----------------------------------------
    # Cada canal é "tocado" se o operador preencheu pelo menos uma credencial.
    # Se foi tocado, exige todas as credenciais necessárias preenchidas
    # (validação em validate_runtime_settings); se não, o canal fica
    # desabilitado e o worker não tenta instanciar o cliente. Mensagens
    # que chegarem em canais desabilitados falham no processor com erro claro.

    def _twilio_credentials_status(self) -> tuple[bool, list[str]]:
        """Retorna (touched, missing) para o canal Twilio em modo real."""
        provided = [
            bool(self.twilio_account_sid),
            bool(self.twilio_api_key_sid),
            bool(self.twilio_api_key_secret),
            bool(self.twilio_from_number),
        ]
        touched = any(provided)
        missing: list[str] = []
        if not self.twilio_account_sid:
            missing.append("TWILIO_ACCOUNT_SID")
        if not self.twilio_api_key_sid:
            missing.append("TWILIO_API_KEY_SID")
        if not self.twilio_api_key_secret:
            missing.append("TWILIO_API_KEY_SECRET")
        if not self.twilio_from_number:
            missing.append("TWILIO_FROM_NUMBER")
        return touched, missing

    def _meta_credentials_status(self) -> tuple[bool, list[str]]:
        """Retorna (touched, missing) para o canal Meta em modo real."""
        provided = [
            bool(self.meta_phone_number_id),
            bool(self.meta_access_token),
            bool(self.meta_verify_token),
            bool(self.meta_app_secret),
        ]
        touched = any(provided)
        missing: list[str] = []
        if not self.meta_phone_number_id:
            missing.append("META_PHONE_NUMBER_ID")
        if not self.meta_access_token:
            missing.append("META_ACCESS_TOKEN")
        if not self.meta_verify_token:
            missing.append("META_VERIFY_TOKEN")
        if self.meta_validate_signature and not self.meta_app_secret:
            missing.append("META_APP_SECRET")
        return touched, missing

    def _uazapi_credentials_status(self) -> tuple[bool, list[str]]:
        """Retorna (touched, missing) para o canal uazapi em modo real."""
        # UAZAPI_INSTANCE_TOKEN é opcional — token chega via payload do
        # webhook por mensagem. Só base_url é obrigatório quando o canal
        # foi tocado.
        touched = bool(self.uazapi_base_url) or bool(self.uazapi_instance_token)
        missing: list[str] = []
        if touched and not self.uazapi_base_url:
            missing.append("UAZAPI_BASE_URL")
        return touched, missing

    def channel_status(self) -> dict[str, dict[str, object]]:
        """Diagnóstico por canal: touched, complete, missing.

        Usado pelo worker no boot e por endpoints de debug. Em modo mock,
        nenhum canal é considerado "incompleto" — credenciais não são
        exigidas porque o cliente simula envio.
        """
        twilio_touched, twilio_missing = self._twilio_credentials_status()
        meta_touched, meta_missing = self._meta_credentials_status()
        uazapi_touched, uazapi_missing = self._uazapi_credentials_status()

        is_mock = self.resolved_outbound_mode == "mock"
        return {
            "twilio": {
                "touched": twilio_touched,
                "missing": [] if is_mock else twilio_missing,
                "complete": is_mock or not twilio_missing,
            },
            "meta": {
                "touched": meta_touched,
                "missing": [] if is_mock else meta_missing,
                "complete": is_mock or not meta_missing,
            },
            "uazapi": {
                "touched": uazapi_touched,
                "missing": [] if is_mock else uazapi_missing,
                "complete": is_mock or not uazapi_missing,
            },
        }

    def validate_runtime_settings(self) -> None:
        """Valida configuração mínima e hardening por ambiente.

        Para cada canal:
        - tocado parcialmente em modo real → ValueError (fail-fast)
        - intocado → desabilitado (worker não instancia o cliente)
        - completo → habilitado
        """
        token = self.internal_service_token.strip()
        if not token:
            raise ValueError(
                "INTERNAL_SERVICE_TOKEN deve ser preenchido antes de subir a API."
            )

        if self.is_production and len(token) < MIN_PRODUCTION_SECRET_LENGTH:
            raise ValueError(
                "Production requer valor forte para INTERNAL_SERVICE_TOKEN. "
                "Atualize as env vars antes do deploy."
            )

        # Em modo mock, credenciais são opcionais — o cliente simula envio.
        if self.resolved_outbound_mode == "mock":
            return

        errors: list[str] = []
        for channel, status in self.channel_status().items():
            if status["touched"] and status["missing"]:
                missing_list = ", ".join(status["missing"])  # type: ignore[arg-type]
                errors.append(
                    f"Canal '{channel}' está parcialmente configurado em "
                    f"modo real — preencha: {missing_list} (ou zere todas as "
                    f"credenciais do canal para desabilitá-lo)."
                )
        if errors:
            raise ValueError(" | ".join(errors))


# Singleton — importar de qualquer lugar do projeto
settings = Settings()
