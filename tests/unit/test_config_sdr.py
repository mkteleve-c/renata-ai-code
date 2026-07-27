"""O agente SDR entra no mesmo fail-fast de "toque parcial" dos canais.

O caso que motiva: `HANDOVER_NOTIFY_PHONE` vazio em produção faz
`human_handover` desligar o agente sem avisar ninguém. O lead fica em
silêncio esperando um humano que nunca foi acionado, e o único sinal é uma
frase que só o modelo lê. É a falha mais cara desta task e a única cujo
sintoma não aparece em lugar nenhum — por isso é boot, não checklist.
"""

import pytest

from whatsapp_langchain.shared.config import MIN_PRODUCTION_SECRET_LENGTH, Settings

COMPLETO = {
    "google_client_id": "cid",
    "google_client_secret": "csecret",
    "google_refresh_token": "rtoken",
    "google_calendar_id": "silvio@exemplo.com",
    "pipedrive_api_token": "tok",
    "handover_notify_phone": "+5511999998888",
}


def _real(**kwargs) -> Settings:
    return Settings(
        internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
        outbound_mode="real",
        **kwargs,
    )


def test_sdr_intocado_nao_derruba_o_boot():
    # Deploys que não usam elevec_sdr (illumi, rhawk) não tocam nenhuma das
    # seis variáveis e continuam subindo.
    touched, missing = _real()._sdr_credentials_status()
    assert touched is False
    assert len(missing) == 6
    _real().validate_runtime_settings()


def test_sdr_completo_passa():
    touched, missing = _real(**COMPLETO)._sdr_credentials_status()
    assert touched is True
    assert missing == []
    _real(**COMPLETO).validate_runtime_settings()


def test_sem_telefone_de_handover_derruba_o_boot():
    incompleto = {**COMPLETO, "handover_notify_phone": ""}

    with pytest.raises(ValueError) as exc:
        _real(**incompleto).validate_runtime_settings()

    assert "HANDOVER_NOTIFY_PHONE" in str(exc.value)
    # A mensagem precisa dizer o que se perde, não só o que falta.
    assert "sem avisar ninguém" in str(exc.value)


def test_sem_token_do_pipedrive_derruba_o_boot():
    incompleto = {**COMPLETO, "pipedrive_api_token": ""}

    with pytest.raises(ValueError) as exc:
        _real(**incompleto).validate_runtime_settings()

    assert "PIPEDRIVE_API_TOKEN" in str(exc.value)


def test_so_o_telefone_de_handover_ja_conta_como_tocado():
    # Qualquer uma das seis liga o grupo — é o que impede um deploy de
    # configurar meia Renata sem perceber.
    with pytest.raises(ValueError) as exc:
        _real(handover_notify_phone="+5511999998888").validate_runtime_settings()

    assert "GOOGLE_CLIENT_ID" in str(exc.value)


def test_modo_mock_nao_exige_nada_do_sdr():
    # Mesma isenção que os canais têm: em dev o cliente simula envio.
    Settings(
        internal_service_token="dev-token",
        outbound_mode="mock",
        handover_notify_phone="",
        google_client_id="cid",
    ).validate_runtime_settings()


@pytest.mark.parametrize(
    "bruto",
    ["11 97777-6666", "+55 (11) 97777-6666", "5511977776666"],
)
def test_telefone_de_handover_formatado_por_humano_e_aceito(bruto):
    _real(**{**COMPLETO, "handover_notify_phone": bruto}).validate_runtime_settings()


@pytest.mark.parametrize("bruto", ["ramal 42", "não sei", "+55", "sim"])
def test_telefone_de_handover_irreconhecivel_derruba_o_boot(bruto):
    # Preenchido mas inválido é PIOR que vazio: passa em qualquer checagem
    # de "está configurado" e só morre dentro do `except` do envio, que
    # existe para não derrubar o desligamento. Handover silencioso com a
    # variável preenchida — ninguém suspeita da configuração.
    with pytest.raises(ValueError) as exc:
        _real(
            **{**COMPLETO, "handover_notify_phone": bruto}
        ).validate_runtime_settings()

    assert "HANDOVER_NOTIFY_PHONE não é um telefone reconhecível" in str(exc.value)


def test_numero_local_sem_ddd_passa_no_boot_e_e_limitacao_conhecida():
    # `canonicalizar` aceita 8 a 15 dígitos (mesmo contrato do CHECK de
    # `leads_crm.phone`), então um número local sem DDI/DDD passa. Apertar
    # isso aqui divergiria da regra que o resto do projeto usa para telefone;
    # o teto que resta é o operador conferir o número no deploy.
    _real(
        **{**COMPLETO, "handover_notify_phone": "97777-6666"}
    ).validate_runtime_settings()
