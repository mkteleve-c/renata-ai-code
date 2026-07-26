"""Aviso de boot para fila que chega por canal sem cliente outbound.

Cenário real: webhook da Evolution ativo com zero credenciais preenchidas.
O inbound aceita tudo, a fila enche, e cada mensagem morre em mark_failed
com o worker aparentemente saudável.
"""

from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.worker.main import _canais_sem_cliente


def test_aponta_canal_pendente_sem_cliente():
    orfaos = _canais_sem_cliente(
        {"twilio": 3, "evolution": 12},
        [MessagingChannel.TWILIO],
    )
    assert orfaos == {"evolution": 12}


def test_silencia_quando_todos_os_canais_tem_cliente():
    orfaos = _canais_sem_cliente(
        {"twilio": 3, "evolution": 12},
        [MessagingChannel.TWILIO, MessagingChannel.EVOLUTION],
    )
    assert orfaos == {}


def test_ignora_canal_sem_pendencia():
    """Canal desabilitado e com fila zerada não é problema — não avisa."""
    orfaos = _canais_sem_cliente(
        {"twilio": 3, "meta": 0},
        [MessagingChannel.TWILIO],
    )
    assert orfaos == {}


def test_fila_vazia_nao_avisa():
    assert _canais_sem_cliente({}, [MessagingChannel.EVOLUTION]) == {}
