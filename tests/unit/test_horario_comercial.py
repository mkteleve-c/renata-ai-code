"""A Renata cobre o fora-de-expediente; o time humano cobre o resto.

Em horário comercial quem atende é gente, pelo ChatWoot. A Renata existe
para a noite, a madrugada e o fim de semana — quando não há ninguém.

Isto é `if`, não prompt. Pedir "não responda em horário comercial" no SOP
seria obedecido às vezes: o mesmo motivo que tirou o desfecho por faixa do
prompt (`faixas.py`) vale aqui, e com um agravante — o erro é visível para o
lead e por cima de um atendente que já está digitando.

Fuso `America/Sao_Paulo`, o mesmo que o prompt usa para `{data_hoje}`. Usar
UTC deslocaria a janela em 3 horas: a Renata calaria às 5h e voltaria às
15h, exatamente ao contrário do pedido.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from whatsapp_langchain.shared.config import Settings

SP = ZoneInfo("America/Sao_Paulo")


def _cfg(inicio: int = 8, fim: int = 18) -> Settings:
    return Settings(
        _env_file=None,
        horario_comercial_inicio=inicio,
        horario_comercial_fim=fim,
    )


def _em(ano: int, mes: int, dia: int, hora: int, minuto: int = 0) -> datetime:
    return datetime(ano, mes, dia, hora, minuto, tzinfo=SP)


# 2026-08-03 é uma segunda-feira; 08-08 sábado; 08-09 domingo.
@pytest.mark.parametrize(
    ("momento", "calada"),
    [
        (_em(2026, 8, 3, 7, 59), False),  # antes de abrir
        (_em(2026, 8, 3, 8, 0), True),  # abre: cala
        (_em(2026, 8, 3, 12, 30), True),  # meio do expediente
        (_em(2026, 8, 3, 17, 59), True),  # último minuto
        (_em(2026, 8, 3, 18, 0), False),  # fecha: volta a atender
        (_em(2026, 8, 3, 23, 30), False),  # noite
        (_em(2026, 8, 4, 3, 0), False),  # madrugada
        (_em(2026, 8, 7, 15, 0), True),  # sexta, expediente
        (_em(2026, 8, 8, 10, 0), False),  # sábado de manhã
        (_em(2026, 8, 9, 15, 0), False),  # domingo à tarde
    ],
)
def test_janela_comercial_de_segunda_a_sexta(momento, calada):
    assert _cfg().em_horario_comercial(momento) is calada


def test_as_bordas_sao_inicio_inclusivo_e_fim_exclusivo():
    """8h em ponto já é expediente; 18h em ponto já não é.

    Sem uma convenção explícita, o minuto da virada fica indefinido e a
    Renata responde (ou cala) numa mensagem por dia sem ninguém entender
    por quê."""
    cfg = _cfg()
    assert cfg.em_horario_comercial(_em(2026, 8, 3, 8, 0)) is True
    assert cfg.em_horario_comercial(_em(2026, 8, 3, 17, 59)) is True
    assert cfg.em_horario_comercial(_em(2026, 8, 3, 18, 0)) is False


def test_desligado_por_padrao():
    """O template é herdado por outros clientes. Uma janela que ligasse
    sozinha faria a Renata deles emudecer metade do dia útil sem ninguém
    ter pedido."""
    padrao = Settings(_env_file=None)
    assert padrao.horario_comercial_ativo is False
    assert padrao.em_horario_comercial(_em(2026, 8, 3, 12, 0)) is False


def test_inicio_igual_ao_fim_desliga():
    cfg = _cfg(inicio=0, fim=0)
    assert cfg.horario_comercial_ativo is False
    assert cfg.em_horario_comercial(_em(2026, 8, 3, 12, 0)) is False


def test_momento_sem_fuso_e_interpretado_em_sao_paulo():
    """`datetime.now()` sem tz é o erro mais fácil de cometer no caller.

    Interpretar como UTC deslocaria a janela em 3 horas — a Renata calaria
    de madrugada e responderia à tarde."""
    ingenuo = datetime(2026, 8, 3, 12, 0)
    assert _cfg().em_horario_comercial(ingenuo) is True


def test_janela_atravessando_a_meia_noite_e_recusada():
    """`inicio > fim` (ex: 18h às 8h) não é suportado e não pode virar uma
    janela vazia em silêncio — seria a Renata atendendo o dia inteiro sem
    ninguém perceber."""
    with pytest.raises(ValueError, match="HORARIO_COMERCIAL"):
        Settings(_env_file=None, horario_comercial_inicio=18, horario_comercial_fim=8)
