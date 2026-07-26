"""Testes das validacoes de configuracao por ambiente."""

import pytest

from whatsapp_langchain.shared.config import (
    MIN_PRODUCTION_SECRET_LENGTH,
    Settings,
)


class TestRuntimeSettingsValidation:
    """Garantias de configuração mínima e hardening por ambiente."""

    def test_rejects_missing_internal_service_token(self):
        """API deve falhar cedo quando o token interno não está preenchido."""
        settings = Settings(environment="development", internal_service_token="")

        with pytest.raises(ValueError, match="INTERNAL_SERVICE_TOKEN"):
            settings.validate_runtime_settings()

    def test_rejects_short_internal_service_token_in_production(self):
        """Production exige token forte com tamanho minimo."""
        settings = Settings(
            environment="production",
            internal_service_token="curto-demais",
        )

        with pytest.raises(ValueError, match="INTERNAL_SERVICE_TOKEN"):
            settings.validate_runtime_settings()

    def test_accepts_non_empty_token_in_development(self):
        """Desenvolvimento local aceita qualquer token não-vazio."""
        settings = Settings(
            environment="development",
            internal_service_token="token-local",
        )

        settings.validate_runtime_settings()

    def test_accepts_strong_internal_service_token_in_production(self):
        """Production aceita token nao-default com comprimento suficiente."""
        settings = Settings(
            environment="production",
            internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
        )

        settings.validate_runtime_settings()


class TestChannelStatus:
    """Diagnóstico por canal: tocado/incompleto/completo."""

    def test_no_channel_touched_in_mock_mode(self):
        """Modo mock: todos os canais reportam complete=True (não exigem creds)."""
        s = Settings(
            environment="development",
            internal_service_token="token-local",
            outbound_mode="mock",
        )
        status = s.channel_status()
        assert status["twilio"]["complete"] is True
        assert status["meta"]["complete"] is True
        assert status["uazapi"]["complete"] is True
        # Em mock, nada está "tocado" se as credenciais estão vazias.
        assert status["twilio"]["touched"] is False

    def test_partial_meta_credentials_marks_incomplete(self):
        """Meta com VERIFY_TOKEN só (sem ACCESS_TOKEN/PHONE_NUMBER_ID) é incompleto."""
        s = Settings(
            environment="production",
            internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
            outbound_mode="real",
            meta_verify_token="abc123",
            meta_app_secret="secret",
        )
        status = s.channel_status()
        assert status["meta"]["touched"] is True
        assert status["meta"]["complete"] is False
        assert "META_PHONE_NUMBER_ID" in status["meta"]["missing"]
        assert "META_ACCESS_TOKEN" in status["meta"]["missing"]

    def test_complete_twilio_is_enabled(self):
        """Twilio com todas as credenciais preenchidas reporta complete."""
        s = Settings(
            environment="production",
            internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
            outbound_mode="real",
            twilio_account_sid="AC" + "x" * 32,
            twilio_api_key_sid="SK" + "x" * 32,
            twilio_api_key_secret="x" * 32,
            twilio_from_number="whatsapp:+14155238886",
        )
        status = s.channel_status()
        assert status["twilio"]["touched"] is True
        assert status["twilio"]["complete"] is True
        assert status["twilio"]["missing"] == []

    def test_validate_raises_for_partial_channel_in_real_mode(self):
        """Canal tocado parcialmente em modo real → ValueError no boot."""
        s = Settings(
            environment="production",
            internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
            outbound_mode="real",
            uazapi_instance_token="xyz",  # tocado mas sem UAZAPI_BASE_URL
        )
        with pytest.raises(ValueError, match="uazapi"):
            s.validate_runtime_settings()

    def test_validate_skips_untouched_channels_in_real_mode(self):
        """Canais com nenhuma credencial preenchida não disparam fail-fast."""
        s = Settings(
            environment="production",
            internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
            outbound_mode="real",
            twilio_account_sid="AC" + "x" * 32,
            twilio_api_key_sid="SK" + "x" * 32,
            twilio_api_key_secret="x" * 32,
            twilio_from_number="whatsapp:+14155238886",
        )
        # Twilio completo, Meta e uazapi intocados — não erra.
        s.validate_runtime_settings()
