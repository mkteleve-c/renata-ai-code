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

from whatsapp_langchain.shared.phone import canonicalizar

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

    # --- Google Calendar (agenda de agendamento do SDR) ---
    # OAuth 2.0 por refresh token, de uma conta com permissão de escrita na
    # agenda. GOOGLE_CALENDAR_ID é o e-mail do calendário.
    # Rotacionar o OAuth Client no Google Cloud Console invalida o refresh
    # token e derruba o agendamento até que alguém reautorize.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = ""

    # --- Pipedrive (funil comercial do SDR) ---
    # Os stage_id vieram do workflow `#4 CRM Control` do n8n e são do
    # pipeline comercial em produção. `desqualificado` não move card — só
    # atualiza o banco —, por isso não tem stage aqui.
    pipedrive_api_token: str = ""
    pipedrive_stage_qualificado: int = 12
    pipedrive_stage_agendado: int = 13

    # --- Handover ---
    # Número (E.164 ou só dígitos) que recebe o aviso quando `human_handover`
    # desliga o agente. Vazio = ninguém é avisado; o desligamento acontece
    # do mesmo jeito.
    handover_notify_phone: str = ""

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

    # --- Follow-up (régua de reengajamento do SDR) ---
    # Desligada por padrão: um deploy que herda este template e esquece de
    # decidir não pode sair mandando WhatsApp de follow-up para leads reais.
    followup_enabled: bool = False
    followup_interval_seconds: int = 300
    followup_batch_size: int = 10
    # Os três degraus ancoram em last_inbound_at (o último inbound DO lead),
    # não em last_interaction_at — ver worker/followup.py.
    followup_nivel1_minutos: int = 15
    followup_nivel2_minutos: int = 75
    followup_nivel3_minutos: int = 1380
    # Corte da janela de 24h da Cloud API, com folga. janela efetiva =
    # 24*60 - esta margem.
    followup_janela_margem_minutos: int = 30

    # --- Balões (resposta em múltiplas mensagens, hoje só elevec_sdr) ---
    # Espaçamento entre balões enviados em sequência. Não é indicador de
    # "digitando" — é puramente temporal (o delay_ms do EvolutionClient não
    # produz efeito nenhum na integração WHATSAPP-BUSINESS desta conta).
    balao_delay_ms: int = 700
    # Teto de balões por resposta — acima disso, extrair_baloes concatena o
    # resto no último balão. Protege lease_seconds: sem teto, uma resposta
    # com dezenas de itens soma segundos de sleep suficientes para estourar
    # o lease e, com mais de um worker, duplicar o envio.
    balao_max_count: int = 10

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

    def _evolution_credentials_status(self) -> tuple[bool, list[str]]:
        """Retorna (touched, missing) para o canal Evolution em modo real."""
        # A instância é fixa por deploy e a apikey autentica envio e download
        # de mídia — os três campos são obrigatórios quando o canal foi tocado.
        touched = (
            bool(self.evolution_base_url)
            or bool(self.evolution_api_key)
            or bool(self.evolution_instance)
        )
        missing: list[str] = []
        if not self.evolution_base_url:
            missing.append("EVOLUTION_BASE_URL")
        if not self.evolution_api_key:
            missing.append("EVOLUTION_API_KEY")
        if not self.evolution_instance:
            missing.append("EVOLUTION_INSTANCE")
        return touched, missing

    def _sdr_credentials_status(self) -> tuple[bool, list[str]]:
        """Retorna (touched, missing) para o agente SDR em modo real.

        Mesma doutrina de "toque parcial" dos canais, e pelo mesmo motivo: o
        que está no grupo só funciona inteiro.

        - **Google Calendar** ausente → a Renata não consulta nem marca nada.
        - **`HANDOVER_NOTIFY_PHONE`** ausente → o pior caso: `human_handover`
          desliga o agente, o lead fica em silêncio esperando, e **nenhuma
          pessoa é acionada**. O único sinal é uma frase que só o modelo lê.
          É a única falha desta task cujo sintoma não aparece em lugar
          nenhum, e por isso é boot em vez de checklist.

        **`PIPEDRIVE_API_TOKEN` ficou de fora, de propósito.** Ele é o único
        do conjunto cuja ausência tem um comportamento definido e desejável:
        a fase continua sendo gravada no banco, o card não se move, e
        `mover_card` registra `crm_pipedrive_nao_configurado` sem devolver
        aviso ao agente. Exigi-lo aqui contradizia o próprio código, que diz
        que rodar sem Pipedrive é escolha válida, e travava o caso concreto
        de staging em modo real sem sandbox de CRM. O funil interno é quem
        alimenta a régua de follow-up e o gate de ingestão; o Pipedrive é
        espelho para o time comercial.

        Deploys que não usam `elevec_sdr` (illumi, rhawk) não tocam nenhuma
        das cinco variáveis e continuam subindo normalmente — é o que o
        `touched` garante.
        """
        campos = (
            ("GOOGLE_CLIENT_ID", self.google_client_id),
            ("GOOGLE_CLIENT_SECRET", self.google_client_secret),
            ("GOOGLE_REFRESH_TOKEN", self.google_refresh_token),
            ("GOOGLE_CALENDAR_ID", self.google_calendar_id),
            ("HANDOVER_NOTIFY_PHONE", self.handover_notify_phone),
        )

        touched = any(valor.strip() for _, valor in campos)
        missing = [nome for nome, valor in campos if not valor.strip()]
        return touched, missing

    def _handover_phone_errors(self) -> list[str]:
        """`HANDOVER_NOTIFY_PHONE` preenchido precisa ser um telefone real.

        Um valor formatado como `"11 97777-6666"` passa em qualquer checagem
        de "está preenchido" e só falha no envio, dentro do `except` que o
        handover usa para não derrubar o desligamento — ou seja, handover
        silencioso **com a variável preenchida**, que é pior que vazia
        porque ninguém suspeita da configuração.
        """
        bruto = self.handover_notify_phone.strip()
        if not bruto or canonicalizar(bruto):
            return []
        return [
            "HANDOVER_NOTIFY_PHONE não é um telefone reconhecível — use "
            "E.164 (+5511999998888) ou só dígitos com DDI e DDD."
        ]

    def channel_status(self) -> dict[str, dict[str, object]]:
        """Diagnóstico por canal: touched, complete, missing.

        Usado pelo worker no boot e por endpoints de debug. Em modo mock,
        nenhum canal é considerado "incompleto" — credenciais não são
        exigidas porque o cliente simula envio.
        """
        twilio_touched, twilio_missing = self._twilio_credentials_status()
        meta_touched, meta_missing = self._meta_credentials_status()
        uazapi_touched, uazapi_missing = self._uazapi_credentials_status()
        evolution_touched, evolution_missing = self._evolution_credentials_status()

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
            "evolution": {
                "touched": evolution_touched,
                "missing": [] if is_mock else evolution_missing,
                "complete": is_mock or not evolution_missing,
            },
        }

    def _evolution_webhook_secret_errors(self) -> list[str]:
        """Exige segredo no webhook inbound da Evolution em produção.

        A Evolution não assina o body — o único gate do inbound é o header
        estático. Com `EVOLUTION_WEBHOOK_SECRET` vazia a rota aceita
        qualquer POST: texto arbitrário e `remoteJid` escolhido pelo
        atacante viram lead, fila, agente e **mensagem de WhatsApp saindo
        pelo número oficial Meta do cliente** para o telefone que ele
        quiser. Custo de LLM, `leads_crm` envenenado e risco de ban do
        número. A URL é adivinhável: os ids de agente são públicos no
        repositório template.

        Vale independente do modo outbound: quem abre a porta é a rota
        inbound, que não olha `OUTBOUND_MODE`.
        """
        if not self.is_production:
            return []

        touched, _ = self._evolution_credentials_status()
        if not touched:
            return []

        secret = self.evolution_webhook_secret.strip()
        if not secret:
            return [
                "Canal 'evolution' configurado em produção exige "
                "EVOLUTION_WEBHOOK_SECRET — sem ele /webhook/evolution aceita "
                "qualquer POST e um terceiro dispara mensagem pelo número "
                "oficial do cliente. Gere com: openssl rand -base64 32"
            ]

        if len(secret) < MIN_PRODUCTION_SECRET_LENGTH:
            return [
                "Production requer valor forte para EVOLUTION_WEBHOOK_SECRET "
                f"(mínimo {MIN_PRODUCTION_SECRET_LENGTH} caracteres). "
                "Gere com: openssl rand -base64 32"
            ]

        return []

    def validate_runtime_settings(self) -> None:
        """Valida configuração mínima e hardening por ambiente.

        Para cada canal:
        - tocado parcialmente em modo real → ValueError (fail-fast)
        - intocado → desabilitado (worker não instancia o cliente)
        - completo → habilitado

        O agente SDR (`elevec_sdr`) segue a mesma doutrina, como um grupo só:
        Google Calendar + telefone de handover. `PIPEDRIVE_API_TOKEN` fica
        fora — ver `_sdr_credentials_status`.
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

        errors: list[str] = []

        # Em modo mock, credenciais outbound são opcionais — o cliente simula
        # envio. O segredo do webhook inbound não entra nessa isenção.
        if self.resolved_outbound_mode != "mock":
            for channel, status in self.channel_status().items():
                if status["touched"] and status["missing"]:
                    missing_list = ", ".join(status["missing"])  # type: ignore[arg-type]
                    errors.append(
                        f"Canal '{channel}' está parcialmente configurado em "
                        f"modo real — preencha: {missing_list} (ou zere todas as "
                        f"credenciais do canal para desabilitá-lo)."
                    )

            sdr_touched, sdr_missing = self._sdr_credentials_status()
            if sdr_touched and sdr_missing:
                errors.append(
                    "Agente SDR está parcialmente configurado em modo real — "
                    f"preencha: {', '.join(sdr_missing)} (ou zere todas as cinco "
                    "variáveis para desabilitá-lo). Sem HANDOVER_NOTIFY_PHONE o "
                    "human_handover desliga o agente sem avisar ninguém."
                )

            errors.extend(self._handover_phone_errors())

        errors.extend(self._evolution_webhook_secret_errors())

        if errors:
            raise ValueError(" | ".join(errors))


# Singleton — importar de qualquer lugar do projeto
settings = Settings()
