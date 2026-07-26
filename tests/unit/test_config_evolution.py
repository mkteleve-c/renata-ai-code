"""Canal Evolution registrado no enum e nas settings."""

from whatsapp_langchain.shared.config import Settings
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
