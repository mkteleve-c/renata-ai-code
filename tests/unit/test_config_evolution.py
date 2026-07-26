"""Canal Evolution registrado no enum e nas settings."""

import pytest

from whatsapp_langchain.shared.config import MIN_PRODUCTION_SECRET_LENGTH, Settings
from whatsapp_langchain.shared.models import MessagingChannel


def test_enum_tem_evolution():
    assert MessagingChannel.EVOLUTION.value == "evolution"


def test_settings_tem_campos_da_evolution():
    s = Settings(
        evolution_base_url="https://evolution.exemplo.host",
        evolution_api_key="chave",
        evolution_instance="instancia-teste",
    )
    assert s.evolution_base_url == "https://evolution.exemplo.host"
    assert s.evolution_api_key == "chave"
    assert s.evolution_instance == "instancia-teste"


def test_settings_da_evolution_tem_default_vazio():
    s = Settings()
    assert s.evolution_base_url == ""
    assert s.evolution_api_key == ""
    assert s.evolution_instance == ""


SEGREDO_FORTE = "s" * MIN_PRODUCTION_SECRET_LENGTH


def _producao(**kwargs) -> Settings:
    return Settings(
        environment="production",
        internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
        outbound_mode="real",
        **kwargs,
    )


class TestChannelStatusEvolution:
    """A Evolution entra no mesmo mecanismo de fail-fast dos outros canais."""

    def test_evolution_intocada_nao_dispara_erro(self):
        """Canal intocado fica desabilitado, mas não derruba o boot."""
        s = _producao()
        status = s.channel_status()
        assert status["evolution"]["touched"] is False
        s.validate_runtime_settings()

    def test_evolution_parcial_marca_incompleta(self):
        s = _producao(evolution_base_url="https://evolution.exemplo.host")
        status = s.channel_status()
        assert status["evolution"]["touched"] is True
        assert status["evolution"]["complete"] is False
        assert "EVOLUTION_API_KEY" in status["evolution"]["missing"]
        assert "EVOLUTION_INSTANCE" in status["evolution"]["missing"]

    def test_evolution_parcial_falha_no_boot_em_modo_real(self):
        s = _producao(evolution_api_key="chave")
        with pytest.raises(ValueError, match="evolution"):
            s.validate_runtime_settings()

    def test_evolution_completa_fica_habilitada(self):
        s = _producao(
            evolution_base_url="https://evolution.exemplo.host",
            evolution_api_key="chave",
            evolution_instance="instancia-teste",
            evolution_webhook_secret=SEGREDO_FORTE,
        )
        status = s.channel_status()
        assert status["evolution"]["touched"] is True
        assert status["evolution"]["complete"] is True
        assert status["evolution"]["missing"] == []
        s.validate_runtime_settings()

    def test_modo_mock_nao_exige_credenciais(self):
        s = Settings(
            environment="development",
            internal_service_token="token-local",
            outbound_mode="mock",
            evolution_api_key="chave",
        )
        status = s.channel_status()
        assert status["evolution"]["complete"] is True
        s.validate_runtime_settings()


class TestWebhookSecretEmProducao:
    """Sem secret, `/webhook/evolution` aceita qualquer POST.

    O vetor caro não é o `fromMe`: é um POST anônimo com texto arbitrário e
    `remoteJid` escolhido pelo atacante — o gate cria o lead, a fila roda, o
    agente responde, e sai mensagem pelo número oficial Meta do cliente para
    um telefone qualquer. A URL é adivinhável: os ids de agente são públicos
    no repositório template.
    """

    def test_producao_com_canal_configurado_e_sem_secret_derruba_o_boot(self):
        s = _producao(
            evolution_base_url="https://evolution.exemplo.host",
            evolution_api_key="chave",
            evolution_instance="instancia-teste",
        )
        with pytest.raises(ValueError, match="EVOLUTION_WEBHOOK_SECRET"):
            s.validate_runtime_settings()

    def test_producao_com_secret_fraco_derruba_o_boot(self):
        s = _producao(
            evolution_base_url="https://evolution.exemplo.host",
            evolution_api_key="chave",
            evolution_instance="instancia-teste",
            evolution_webhook_secret="123",
        )
        with pytest.raises(ValueError, match="EVOLUTION_WEBHOOK_SECRET"):
            s.validate_runtime_settings()

    def test_producao_em_outbound_mock_tambem_exige_secret(self):
        """A rota inbound fica aberta independente do modo outbound."""
        s = Settings(
            environment="production",
            internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
            outbound_mode="mock",
            evolution_base_url="https://evolution.exemplo.host",
            evolution_api_key="chave",
            evolution_instance="instancia-teste",
        )
        with pytest.raises(ValueError, match="EVOLUTION_WEBHOOK_SECRET"):
            s.validate_runtime_settings()

    def test_producao_sem_canal_evolution_nao_exige_secret(self):
        _producao(
            twilio_account_sid="AC123",
            twilio_api_key_sid="SK123",
            twilio_api_key_secret="segredo",
            twilio_from_number="whatsapp:+5511999999999",
        ).validate_runtime_settings()

    def test_desenvolvimento_sem_secret_continua_livre(self):
        Settings(
            environment="development",
            internal_service_token="token-local",
            outbound_mode="real",
            evolution_base_url="https://evolution.exemplo.host",
            evolution_api_key="chave",
            evolution_instance="instancia-teste",
        ).validate_runtime_settings()
