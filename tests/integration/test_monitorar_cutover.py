"""`scripts/monitorar_cutover.py` (Fase 5, Task 5) -- as seis medições da
primeira hora do cutover, cada uma isolada.

As cinco medições de Postgres rodam contra o banco real de dev (porta 5440),
mesmo padrão de `test_migrar_cli.py`/`test_preflight.py` -- nenhum monkeypatch
na camada de banco. Todo telefone/lead de teste usa o prefixo `5511944400` --
não colide com nenhum outro prefixo já em uso nesta suíte. A medição do
Google Calendar (a única que toca rede) usa sempre `httpx.MockTransport`,
mesmo padrão de `test_preflight.py`.

`agora`/`desde` são sempre passados explicitamente às funções sob teste --
nunca o relógio de parede -- para o teste controlar a janela sem corrida
contra o tempo real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scripts.monitorar_cutover import (
    Metrica,
    Relatorio,
    Transportes,
    contar_eventos_criados_na_janela,
    contar_fila_por_status_e_canal,
    eh_estruturado,
    medir_eventos_calendar,
    medir_falhas_max_attempts,
    medir_fallback_baloes,
    medir_fila_parada,
    medir_handover_leads_novos,
    medir_leads_criados,
    motivos_reversao,
    rodar_monitoramento,
)
from whatsapp_langchain.shared.config import Settings
from whatsapp_langchain.shared.db import get_pool

_PREFIXO = "5511944400"
_AGORA = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
_DESDE = _AGORA - timedelta(hours=1)


@pytest.fixture(autouse=True)
async def limpar():
    async def apagar():
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "delete from leads_crm where phone like %s", (f"{_PREFIXO}%",)
            )
            await conn.execute(
                "delete from message_queue where phone_number like %s",
                (f"{_PREFIXO}%",),
            )
            await conn.commit()

    await apagar()
    yield
    await apagar()


async def _inserir_lead(
    phone: str,
    *,
    created_at: datetime,
    agent_active: bool = True,
) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, name, phase, agent_active, created_at, "
            " last_interaction_at) "
            "values (%s, 'Fulano de Teste', 'iniciou_conversa', %s, %s, %s)",
            (phone, agent_active, created_at, created_at),
        )
        await conn.commit()


async def _inserir_mensagem(
    phone: str,
    *,
    agent_id: str = "elevec_sdr",
    status: str = "done",
    attempts: int = 0,
    max_attempts: int = 3,
    response: str | None = None,
    created_at: datetime,
    channel: str = "evolution",
) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into message_queue "
            " (phone_number, agent_id, thread_id, incoming_message, channel, "
            "  status, attempts, max_attempts, response, created_at) "
            "values (%s, %s, %s, 'oi', %s, %s, %s, %s, %s, %s)",
            (
                phone,
                agent_id,
                f"{phone}:{agent_id}",
                channel,
                status,
                attempts,
                max_attempts,
                response,
                created_at,
            ),
        )
        await conn.commit()


def _msg_estruturada(*textos: str) -> str:
    import json

    return json.dumps({"messages": list(textos)})


# --- eh_estruturado (pura) --------------------------------------------------


def test_eh_estruturado_aceita_mensagens_validas():
    assert eh_estruturado(_msg_estruturada("Oi!", "Tudo bem?")) is True


def test_eh_estruturado_rejeita_vazio():
    assert eh_estruturado("") is False
    assert eh_estruturado(None) is False


def test_eh_estruturado_rejeita_texto_puro():
    assert eh_estruturado("Oi, tudo bem?") is False


def test_eh_estruturado_rejeita_json_sem_messages():
    assert eh_estruturado('{"foo": "bar"}') is False


def test_eh_estruturado_rejeita_messages_vazia():
    assert eh_estruturado('{"messages": []}') is False


def test_eh_estruturado_rejeita_item_nao_string():
    assert eh_estruturado('{"messages": ["oi", 123]}') is False


def test_eh_estruturado_rejeita_item_so_espaco():
    assert eh_estruturado('{"messages": ["   "]}') is False


# --- 1. Fila parada ----------------------------------------------------------


async def test_fila_sem_mensagens_paradas_ok():
    await _inserir_mensagem(
        f"{_PREFIXO}01", status="queued", created_at=_AGORA - timedelta(minutes=1)
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fila_parada(conn, agora=_AGORA)
    assert m.status == "ok"


async def test_fila_com_mensagem_parada_ha_mais_de_5_min_reverte():
    await _inserir_mensagem(
        f"{_PREFIXO}02", status="queued", created_at=_AGORA - timedelta(minutes=10)
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fila_parada(conn, agora=_AGORA)
    assert m.status == "reverter"
    assert "1" in m.valor


async def test_fila_mensagem_done_ha_muito_tempo_nao_conta():
    """Só queued/processing importam -- done velho é sucesso, não fila presa."""
    await _inserir_mensagem(
        f"{_PREFIXO}03", status="done", created_at=_AGORA - timedelta(hours=2)
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fila_parada(conn, agora=_AGORA)
    assert m.status == "ok"


async def test_contar_fila_por_status_e_canal_agrega():
    await _inserir_mensagem(f"{_PREFIXO}04", status="done", created_at=_AGORA)
    await _inserir_mensagem(f"{_PREFIXO}05", status="done", created_at=_AGORA)
    pool = await get_pool()
    async with pool.connection() as conn:
        linhas = await contar_fila_por_status_e_canal(conn)
    achada = next(
        (
            linha
            for linha in linhas
            if linha.channel == "evolution" and linha.status == "done"
        ),
        None,
    )
    assert achada is not None
    assert achada.total >= 2


# --- 2. Falhas com tentativas esgotadas -------------------------------------


async def test_sem_falhas_ok():
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_falhas_max_attempts(conn, desde=_DESDE)
    assert m.status == "ok"


async def test_uma_falha_e_atencao_nao_reverter():
    await _inserir_mensagem(
        f"{_PREFIXO}06",
        status="failed",
        attempts=3,
        max_attempts=3,
        created_at=_AGORA,
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_falhas_max_attempts(conn, desde=_DESDE)
    assert m.status == "atencao"


async def test_cinco_falhas_reverte():
    for i in range(5):
        await _inserir_mensagem(
            f"{_PREFIXO}1{i}",
            status="failed",
            attempts=3,
            max_attempts=3,
            created_at=_AGORA,
        )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_falhas_max_attempts(conn, desde=_DESDE)
    assert m.status == "reverter"


async def test_falha_com_tentativas_nao_esgotadas_nao_conta():
    await _inserir_mensagem(
        f"{_PREFIXO}07",
        status="failed",
        attempts=1,
        max_attempts=3,
        created_at=_AGORA,
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_falhas_max_attempts(conn, desde=_DESDE)
    assert m.status == "ok"


async def test_falha_fora_da_janela_nao_conta():
    for i in range(5):
        await _inserir_mensagem(
            f"{_PREFIXO}2{i}",
            status="failed",
            attempts=3,
            max_attempts=3,
            created_at=_DESDE - timedelta(hours=1),
        )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_falhas_max_attempts(conn, desde=_DESDE)
    assert m.status == "ok"


# --- 3. Leads criados --------------------------------------------------------


async def test_leads_criados_zero_ok():
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_leads_criados(conn, desde=_DESDE)
    assert m.status == "ok"
    assert m.valor == "0"


async def test_leads_criados_acima_do_limiar_e_atencao():
    for i in range(21):
        await _inserir_lead(f"{_PREFIXO}{i:02d}", created_at=_AGORA)
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_leads_criados(conn, desde=_DESDE)
    assert m.status == "atencao"


async def test_lead_antigo_fora_da_janela_nao_conta():
    await _inserir_lead(f"{_PREFIXO}08", created_at=_DESDE - timedelta(days=30))
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_leads_criados(conn, desde=_DESDE)
    assert m.valor == "0"


# --- 4. Handover entre leads novos ------------------------------------------


async def test_sem_leads_novos_handover_ok():
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_handover_leads_novos(conn, desde=_DESDE)
    assert m.status == "ok"


async def test_handover_baixo_entre_leads_novos_ok():
    for i in range(5):
        await _inserir_lead(f"{_PREFIXO}4{i}", created_at=_AGORA, agent_active=True)
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_handover_leads_novos(conn, desde=_DESDE)
    assert m.status == "ok"


async def test_handover_alto_entre_leads_novos_reverte():
    for i in range(3):
        await _inserir_lead(f"{_PREFIXO}5{i}", created_at=_AGORA, agent_active=False)
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_handover_leads_novos(conn, desde=_DESDE)
    assert m.status == "reverter"


async def test_handover_amostra_pequena_nao_reverte_mesmo_com_taxa_alta():
    """1 pausado em 1 lead novo é 100% -- mas amostra menor que o mínimo
    não pode disparar reversão sozinha (ruído estatístico)."""
    await _inserir_lead(f"{_PREFIXO}09", created_at=_AGORA, agent_active=False)
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_handover_leads_novos(conn, desde=_DESDE)
    assert m.status != "reverter"


# --- 5. Taxa de fallback de balão único -------------------------------------


async def test_sem_respostas_fallback_ok():
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fallback_baloes(conn, desde=_DESDE)
    assert m.status == "ok"


async def test_todas_estruturadas_ok():
    for i in range(4):
        await _inserir_mensagem(
            f"{_PREFIXO}6{i}",
            status="done",
            response=_msg_estruturada("Oi!"),
            created_at=_AGORA,
        )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fallback_baloes(conn, desde=_DESDE)
    assert m.status == "ok"


async def test_taxa_alta_de_fallback_reverte():
    for i in range(4):
        await _inserir_mensagem(
            f"{_PREFIXO}7{i}",
            status="done",
            response="texto puro fora do schema",
            created_at=_AGORA,
        )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fallback_baloes(conn, desde=_DESDE)
    assert m.status == "reverter"


async def test_uma_falha_isolada_e_atencao_nao_reverter():
    await _inserir_mensagem(
        f"{_PREFIXO}10",
        status="done",
        response=_msg_estruturada("Oi!"),
        created_at=_AGORA,
    )
    await _inserir_mensagem(
        f"{_PREFIXO}11",
        status="done",
        response="fora do schema",
        created_at=_AGORA,
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fallback_baloes(conn, desde=_DESDE)
    assert m.status == "atencao"


async def test_fallback_ignora_outros_agentes():
    """Só `elevec_sdr` responde em balões -- resposta de outro agente fora
    do schema JSON não é fallback, é o formato normal daquele agente."""
    await _inserir_mensagem(
        f"{_PREFIXO}12",
        agent_id="rhawk_assistant",
        status="done",
        response="texto puro, normal para este agente",
        created_at=_AGORA,
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fallback_baloes(conn, desde=_DESDE)
    assert m.status == "ok"
    assert m.valor == "0/0"


async def test_fallback_ignora_mensagens_nao_done():
    await _inserir_mensagem(
        f"{_PREFIXO}13",
        status="queued",
        response=None,
        created_at=_AGORA,
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        m = await medir_fallback_baloes(conn, desde=_DESDE)
    assert m.valor == "0/0"


# --- 6. Eventos do Google Calendar -------------------------------------------


def test_contar_eventos_criados_filtra_pelo_campo_created():
    eventos = [
        {"id": "1", "created": "2026-07-27T11:30:00Z"},
        {"id": "2", "created": "2026-07-27T10:00:00Z"},
    ]
    total = contar_eventos_criados_na_janela(eventos, desde=_DESDE)
    assert total == 1


def test_contar_eventos_ignora_sem_campo_created():
    eventos = [{"id": "1"}, {"id": "2", "created": None}]
    assert contar_eventos_criados_na_janela(eventos, desde=_DESDE) == 0


def test_contar_eventos_ignora_created_malformado():
    eventos = [{"id": "1", "created": "nao-e-uma-data"}]
    assert contar_eventos_criados_na_janela(eventos, desde=_DESDE) == 0


async def test_google_nao_configurado_e_indisponivel():
    m = await medir_eventos_calendar(Settings(), desde=_DESDE, agora=_AGORA)
    assert m.status == "indisponivel"


async def test_google_parcialmente_configurado_e_indisponivel():
    """Só `google_client_id` preenchido -- canal "tocado" mas incompleto.

    Precisa das QUATRO variáveis, não de "pelo menos uma" -- diferencia
    `all(campos)` (correto) de `any(campos)` (aceitaria configuração pela
    metade e tentaria autenticar sem client_secret).
    """
    m = await medir_eventos_calendar(
        Settings(google_client_id="cid"), desde=_DESDE, agora=_AGORA
    )
    assert m.status == "indisponivel"


def _settings_google(**kwargs) -> Settings:
    return Settings(
        google_client_id="cid",
        google_client_secret="csecret",
        google_refresh_token="refresh-nao-pode-vazar",
        google_calendar_id="silvio@exemplo.com",
        **kwargs,
    )


async def test_google_configurado_conta_eventos_da_janela():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "1", "created": "2026-07-27T11:45:00Z"},
                    {"id": "2", "created": "2026-07-27T09:00:00Z"},
                ]
            },
        )

    m = await medir_eventos_calendar(
        _settings_google(),
        desde=_DESDE,
        agora=_AGORA,
        transporte=httpx.MockTransport(handler),
    )
    assert m.status == "ok"
    assert m.valor == "1"


async def test_google_calendar_inalcancavel_fica_indisponivel_nao_bloqueia():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(500, json={"error": "boom"})

    m = await medir_eventos_calendar(
        _settings_google(),
        desde=_DESDE,
        agora=_AGORA,
        transporte=httpx.MockTransport(handler),
    )
    assert m.status == "indisponivel"


# --- Orquestração e motivos_reversao ----------------------------------------


async def test_rodar_monitoramento_sem_dados_nao_reverte():
    pool = await get_pool()
    relatorio = await rodar_monitoramento(
        pool=pool, settings=Settings(), desde=_DESDE, agora=_AGORA
    )
    assert isinstance(relatorio, Relatorio)
    assert motivos_reversao(relatorio) == []


async def test_rodar_monitoramento_agrega_motivo_de_reversao_da_fila_parada():
    await _inserir_mensagem(
        f"{_PREFIXO}14", status="queued", created_at=_AGORA - timedelta(minutes=30)
    )
    pool = await get_pool()
    relatorio = await rodar_monitoramento(
        pool=pool, settings=Settings(), desde=_DESDE, agora=_AGORA
    )
    assert motivos_reversao(relatorio) != []


async def test_rodar_monitoramento_agrega_multiplos_motivos():
    await _inserir_mensagem(
        f"{_PREFIXO}15", status="queued", created_at=_AGORA - timedelta(minutes=30)
    )
    for i in range(5):
        await _inserir_mensagem(
            f"{_PREFIXO}8{i}",
            status="failed",
            attempts=3,
            max_attempts=3,
            created_at=_AGORA,
        )
    pool = await get_pool()
    relatorio = await rodar_monitoramento(
        pool=pool, settings=Settings(), desde=_DESDE, agora=_AGORA
    )
    assert len(motivos_reversao(relatorio)) >= 2


def test_motivos_reversao_ignora_atencao_e_ok():
    relatorio = Relatorio(
        fila=[],
        metricas=[
            Metrica("a", "ok", "0", "-"),
            Metrica("b", "atencao", "1", "-"),
            Metrica("c", "indisponivel", "-", "-"),
        ],
    )
    assert motivos_reversao(relatorio) == []


async def test_transportes_default_nao_toca_rede_quando_google_nao_configurado():
    """`Transportes()` default (nenhum dublê) não deveria estourar quando o
    Google não está configurado -- a medição sai antes de qualquer request."""
    pool = await get_pool()
    relatorio = await rodar_monitoramento(
        pool=pool,
        settings=Settings(),
        desde=_DESDE,
        agora=_AGORA,
        transportes=Transportes(),
    )
    calendario = next(
        m for m in relatorio.metricas if m.nome == "Eventos criados no Google Calendar"
    )
    assert calendario.status == "indisponivel"
