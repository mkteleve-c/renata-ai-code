"""Monitoramento da primeira hora do cutover (Fase 5, Task 5).

Seis medições contra o Postgres de produção (a sétima fonte, o Google
Calendar, é lida à parte -- ver abaixo), cada uma com um limiar explícito de
"motivo para reverter" documentado junto da função que a calcula e em
`docs/CUTOVER.md` ("Monitoramento da primeira hora"). O objetivo não é uma
foto bonita da fila -- é responder, sem ambiguidade, "isto é motivo para
voltar ao n8n agora?".

Cada chamada deste script é uma fotografia de um instante, não um processo
contínuo -- rode em intervalos (o roteiro de cutover sugere a cada 5-10
minutos) durante a primeira hora de tráfego real.

## Por que a maioria das medições recebe `desde`/`agora` explícitos

Nenhuma consulta usa `now()` do lado do Postgres para decidir a janela --
`agora` é sempre um argumento Python, com default `datetime.now(UTC)` só no
ponto de entrada do CLI. Isso é o que torna as funções testáveis com um
relógio controlado (mesmo padrão de `tests/integration/conftest.py`,
`lead_factory`): um teste que insere uma linha com `created_at` fixo e chama
a medição com `agora` fixo não corre contra o relógio de parede.

## `eventos_calendar` é a exceção -- lê a API do Google, não o Postgres

`leads_crm` não guarda quando um `google_event_id` foi escrito (não há
coluna de auditoria de transição -- só o valor atual). A única fonte
confiável de "quando este evento foi criado" é o campo `created` que a
própria API do Google Calendar devolve por evento, que é diferente do
horário de início do compromisso. Por isso esta medição consulta
`GoogleCalendarClient.listar_eventos` numa janela ampla de horários de
início (cobrindo agendamentos futuros, não só o passado recente) e filtra o
resultado pelo campo `created` de cada evento -- não pela janela de busca em
si.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.config import Settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.google_calendar import (
    GoogleCalendarClient,
    GoogleCalendarError,
)

# --- Limiares -- cada um documentado em docs/CUTOVER.md também -------------

# Mensagem em queued/processing há mais desse tempo = worker parado.
# 5x o LEASE_SECONDS default (60s) -- tempo de sobra para qualquer retry
# legítimo não ser confundido com fila travada.
FILA_PARADA_MINUTOS = 5

# Falhas com tentativas esgotadas na janela. 5 ou mais = reverter; 1-4 = atenção.
FALHAS_REVERTER = 5

# Taxa de leads recém-criados que já nascem com agent_active=false
# (handover disparando). Amostra mínima evita que 1 lead pausado em 2 vire
# "50%" e dispare reversão por ruído estatístico.
HANDOVER_TAXA_REVERTER = 0.5
HANDOVER_TAXA_ATENCAO = 0.2
HANDOVER_AMOSTRA_MINIMA = 3

# Taxa de respostas do elevec_sdr fora do schema de balões esperado.
FALLBACK_TAXA_REVERTER = 0.3
FALLBACK_AMOSTRA_MINIMA = 3

# Leads novos na janela acima disso vira alerta para investigar (não é,
# sozinho, motivo de reversão -- ver docstring de medir_leads_criados).
LEADS_NOVOS_ATENCAO = 20

AGENTE_BALOES = "elevec_sdr"

StatusMetrica = Literal["ok", "atencao", "reverter", "indisponivel"]


@dataclass(frozen=True)
class Metrica:
    """Resultado de uma medição.

    `status='reverter'` é o único que o roteiro de cutover lê como gatilho
    de reversão -- `atencao` pede investigação, não ação imediata.
    """

    nome: str
    status: StatusMetrica
    valor: str
    detalhe: str


@dataclass(frozen=True)
class LinhaFila:
    channel: str
    status: str
    total: int


@dataclass(frozen=True)
class Relatorio:
    fila: list[LinhaFila]
    metricas: list[Metrica]


def motivos_reversao(relatorio: Relatorio) -> list[str]:
    """Detalhe de cada métrica com status='reverter' -- vazio se nenhuma."""
    return [m.detalhe for m in relatorio.metricas if m.status == "reverter"]


# --- 1. Fila por status e por canal -----------------------------------------


async def contar_fila_por_status_e_canal(conn: AsyncConnection) -> list[LinhaFila]:
    """Fotografia bruta: quantas mensagens em cada (canal, status).

    Só para leitura humana na tabela impressa -- o gatilho de reversão da
    fila é `medir_fila_parada`, não esta contagem (uma fila com poucas
    mensagens pendentes pode ser normal minutos após o cutover; o que
    importa é há quanto tempo elas esperam).
    """
    cur = await conn.execute(
        """
        SELECT channel, status, COUNT(*)
        FROM message_queue
        GROUP BY channel, status
        ORDER BY channel, status
        """
    )
    return [LinhaFila(row[0], row[1], int(row[2])) for row in await cur.fetchall()]


async def medir_fila_parada(
    conn: AsyncConnection,
    *,
    minutos: int = FILA_PARADA_MINUTOS,
    agora: datetime | None = None,
) -> Metrica:
    """Mensagens em queued/processing há mais de `minutos`.

    Motivo de reverter: qualquer uma. Um lead real está esperando resposta
    com o sistema antigo já desligado -- o worker parou, travou, ou não está
    rodando.
    """
    nome = "Fila parada (queued/processing > 5min)"
    agora = agora or datetime.now(UTC)
    corte = agora - timedelta(minutes=minutos)

    cur = await conn.execute(
        """
        SELECT COUNT(*)
        FROM message_queue
        WHERE status IN ('queued', 'processing')
          AND created_at < %s
        """,
        (corte,),
    )
    linha = await cur.fetchone()
    assert linha is not None
    total = int(linha[0])

    if total > 0:
        return Metrica(
            nome,
            "reverter",
            str(total),
            f"{total} mensagem(ns) parada(s) há mais de {minutos} minutos -- "
            "worker parado, travado, ou fora do ar.",
        )
    return Metrica(nome, "ok", "0", "nenhuma mensagem parada")


# --- 2. Falhas com tentativas esgotadas -------------------------------------


async def medir_falhas_max_attempts(
    conn: AsyncConnection,
    *,
    desde: datetime,
) -> Metrica:
    """Mensagens `failed` com `attempts >= max_attempts` desde `desde`.

    0 = ok. 1-4 = atenção (uma falha isolada acontece). 5+ = reverter --
    padrão, não acaso, na primeira hora.
    """
    nome = "Falhas com tentativas esgotadas"
    cur = await conn.execute(
        """
        SELECT COUNT(*)
        FROM message_queue
        WHERE status = 'failed'
          AND attempts >= max_attempts
          AND created_at >= %s
        """,
        (desde,),
    )
    linha = await cur.fetchone()
    assert linha is not None
    total = int(linha[0])

    if total >= FALHAS_REVERTER:
        return Metrica(
            nome,
            "reverter",
            str(total),
            f"{total} mensagem(ns) falhada(s) com tentativas esgotadas na "
            f"janela -- limiar de reversão é {FALHAS_REVERTER}.",
        )
    if total >= 1:
        return Metrica(nome, "atencao", str(total), f"{total} falha(s) -- investigar")
    return Metrica(nome, "ok", "0", "nenhuma falha com tentativas esgotadas")


# --- 3. Leads criados na última hora ----------------------------------------


async def medir_leads_criados(
    conn: AsyncConnection,
    *,
    desde: datetime,
) -> Metrica:
    """Leads com `created_at` desde `desde`.

    Esperado ~0 logo após a migração -- os leads importados vêm com
    `created_at` PRESERVADO do Supabase (o mais antigo entre linhas
    fundidas), nunca `now()` (ver `scripts/migrar_supabase.py`,
    `_SQL_UPSERT_LEAD`: `created_at = least(leads_crm.created_at,
    excluded.created_at)`). Um número alto aqui logo após o passo 6 do
    cutover é sinal de duplicação em vez de fusão, ou de canonicalização de
    telefone divergente entre o gate de ingestão e a migração.

    Não é, sozinha, motivo de reversão -- leads novos de verdade (campanha
    ativa) também aparecem aqui. É sinal para cruzar com as métricas de
    handover e fallback antes de decidir.
    """
    nome = "Leads criados na janela"
    cur = await conn.execute(
        "SELECT COUNT(*) FROM leads_crm WHERE created_at >= %s",
        (desde,),
    )
    linha = await cur.fetchone()
    assert linha is not None
    total = int(linha[0])

    if total > LEADS_NOVOS_ATENCAO:
        return Metrica(
            nome,
            "atencao",
            str(total),
            f"{total} lead(s) novo(s) -- acima do esperado logo após a "
            "migração; cruzar com handover/fallback antes de agir.",
        )
    return Metrica(nome, "ok", str(total), "dentro do esperado")


# --- 4. Handover entre leads novos ------------------------------------------


async def medir_handover_leads_novos(
    conn: AsyncConnection,
    *,
    desde: datetime,
) -> Metrica:
    """Entre os leads criados na janela, fração com `agent_active=false`.

    Handover disparando muito entre leads que acabaram de chegar é sinal de
    a Renata travando (erro de tool, saída fora do SOP) e caindo no caminho
    de desligamento.
    """
    nome = "Handover entre leads novos"
    cur = await conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE NOT agent_active), COUNT(*)
        FROM leads_crm
        WHERE created_at >= %s
        """,
        (desde,),
    )
    linha = await cur.fetchone()
    assert linha is not None
    pausados, total = int(linha[0]), int(linha[1])

    if total == 0:
        return Metrica(nome, "ok", "0/0", "nenhum lead novo na janela")

    taxa = pausados / total
    resumo = f"{pausados}/{total} ({taxa:.0%})"

    if total >= HANDOVER_AMOSTRA_MINIMA and taxa >= HANDOVER_TAXA_REVERTER:
        return Metrica(
            nome,
            "reverter",
            resumo,
            f"{resumo} dos leads novos já pausados -- limiar de reversão é "
            f"{HANDOVER_TAXA_REVERTER:.0%} com amostra >= "
            f"{HANDOVER_AMOSTRA_MINIMA}.",
        )
    if taxa > HANDOVER_TAXA_ATENCAO:
        return Metrica(nome, "atencao", resumo, f"{resumo} pausados -- investigar")
    return Metrica(nome, "ok", resumo, "dentro do esperado")


# --- 5. Taxa de fallback de balão único -------------------------------------


def eh_estruturado(resposta: str | None) -> bool:
    """True se `resposta` é `{"messages": [...]}` com só strings não-vazias.

    Réplica deliberadamente estrita da leitura feliz de `extrair_baloes`
    (`agents/catalog/elevec_sdr/saida.py`) -- sem os fallbacks de cerca de
    código que aquela função tenta antes de desistir. Para fins de saúde do
    schema, uma resposta que só passa por causa de uma cerca de código já é
    uma saída que não seguiu a instrução de emitir JSON cru; contá-la como
    estruturada esconderia o sintoma que esta medição existe para achar.
    """
    bruto = (resposta or "").strip()
    if not bruto:
        return False
    try:
        dados = json.loads(bruto)
    except (ValueError, TypeError):
        return False
    if not isinstance(dados, dict):
        return False
    mensagens = dados.get("messages")
    if not isinstance(mensagens, list) or not mensagens:
        return False
    return all(isinstance(m, str) and m.strip() for m in mensagens)


async def medir_fallback_baloes(
    conn: AsyncConnection,
    *,
    desde: datetime,
    agent_id: str = AGENTE_BALOES,
) -> Metrica:
    """Taxa de respostas concluídas do `elevec_sdr` fora do schema de balões.

    30%+ com amostra >= 3 = reverter -- a Renata está estruturalmente fora
    do schema esperado, não é caso isolado. Abaixo disso, algum ruído de
    modelo é esperado numa integração real com LLM.
    """
    nome = "Taxa de fallback de balão único"
    cur = await conn.execute(
        """
        SELECT response
        FROM message_queue
        WHERE agent_id = %s
          AND status = 'done'
          AND created_at >= %s
        """,
        (agent_id, desde),
    )
    respostas = [row[0] for row in await cur.fetchall()]
    total = len(respostas)

    if total == 0:
        return Metrica(nome, "ok", "0/0", "nenhuma resposta concluída na janela")

    estruturadas = sum(1 for r in respostas if eh_estruturado(r))
    fallback = total - estruturadas
    taxa = fallback / total
    resumo = f"{fallback}/{total} ({taxa:.0%})"

    if total >= FALLBACK_AMOSTRA_MINIMA and taxa >= FALLBACK_TAXA_REVERTER:
        return Metrica(
            nome,
            "reverter",
            resumo,
            f"{resumo} das respostas fora do schema de balões -- limiar de "
            f"reversão é {FALLBACK_TAXA_REVERTER:.0%} com amostra >= "
            f"{FALLBACK_AMOSTRA_MINIMA}.",
        )
    if fallback > 0:
        return Metrica(nome, "atencao", resumo, f"{resumo} em fallback -- investigar")
    return Metrica(nome, "ok", resumo, "todas estruturadas")


# --- 6. Eventos criados no Google Calendar ----------------------------------


def contar_eventos_criados_na_janela(
    eventos: list[dict[str, Any]], *, desde: datetime
) -> int:
    """Quantos `eventos` (formato bruto da API do Google) têm `created` >= `desde`.

    `created` vem em RFC3339 (`...Z` ou offset explícito); eventos sem o
    campo, ou com o campo em formato irreconhecível, são ignorados em vez de
    contados -- um evento que não dá para datar não pode virar evidência de
    "criado nesta janela".
    """
    total = 0
    for evento in eventos:
        bruto = evento.get("created")
        if not isinstance(bruto, str):
            continue
        try:
            momento = datetime.fromisoformat(bruto.replace("Z", "+00:00"))
        except ValueError:
            continue
        if momento >= desde:
            total += 1
    return total


async def medir_eventos_calendar(
    settings: Settings,
    *,
    desde: datetime,
    agora: datetime | None = None,
    janela_futura_dias: int = 60,
    transporte: httpx.AsyncBaseTransport | None = None,
) -> Metrica:
    """Eventos do Google Calendar criados desde `desde`.

    Informativo -- nunca `reverter`. Zero na primeira hora é normal; a
    checagem falhando isoladamente (agenda inalcançável) não bloqueia o
    resto do relatório, mas combinado com falhas nas métricas 1-5 é sinal de
    que o agendamento pode estar quebrado silenciosamente.
    """
    nome = "Eventos criados no Google Calendar"
    campos = (
        settings.google_client_id,
        settings.google_client_secret,
        settings.google_refresh_token,
        settings.google_calendar_id,
    )
    if not all(campos):
        return Metrica(nome, "indisponivel", "-", "Google Calendar não configurado")

    agora = agora or datetime.now(UTC)
    cliente = GoogleCalendarClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        refresh_token=settings.google_refresh_token,
        calendar_id=settings.google_calendar_id,
    )
    cliente._transport = transporte

    try:
        eventos = await cliente.listar_eventos(
            agora - timedelta(days=1), agora + timedelta(days=janela_futura_dias)
        )
    except GoogleCalendarError as exc:
        return Metrica(
            nome, "indisponivel", "-", f"Google Calendar respondeu {exc.status_code}"
        )

    total = contar_eventos_criados_na_janela(eventos, desde=desde)
    return Metrica(nome, "ok", str(total), f"{total} evento(s) criado(s) na janela")


# --- Orquestração ------------------------------------------------------------


@dataclass(frozen=True)
class Transportes:
    """Dublê de transporte HTTP para o Google Calendar nos testes."""

    google: httpx.AsyncBaseTransport | None = None


async def rodar_monitoramento(
    *,
    pool: AsyncConnectionPool,
    settings: Settings,
    desde: datetime | None = None,
    agora: datetime | None = None,
    transportes: Transportes | None = None,
) -> Relatorio:
    """Roda as seis medições. `desde` default: última hora a partir de `agora`."""
    transportes = transportes or Transportes()
    agora = agora or datetime.now(UTC)
    desde = desde or (agora - timedelta(hours=1))

    async with pool.connection() as conn:
        fila = await contar_fila_por_status_e_canal(conn)
        metricas = [
            await medir_fila_parada(conn, agora=agora),
            await medir_falhas_max_attempts(conn, desde=desde),
            await medir_leads_criados(conn, desde=desde),
            await medir_handover_leads_novos(conn, desde=desde),
            await medir_fallback_baloes(conn, desde=desde),
        ]

    metricas.append(
        await medir_eventos_calendar(
            settings, desde=desde, agora=agora, transporte=transportes.google
        )
    )

    return Relatorio(fila=fila, metricas=metricas)


def _imprimir(relatorio: Relatorio) -> None:
    print("== Fila por status e canal ==")
    if not relatorio.fila:
        print("(vazia)")
    else:
        for linha in relatorio.fila:
            print(f"  {linha.channel:<10} {linha.status:<12} {linha.total}")

    print()
    print("== Métricas da primeira hora ==")
    largura = max(len(m.nome) for m in relatorio.metricas)
    print(f"{'MÉTRICA':<{largura}}  STATUS       VALOR      DETALHE")
    print(f"{'-' * largura}  -----------  ---------  {'-' * 40}")
    for m in relatorio.metricas:
        print(f"{m.nome:<{largura}}  {m.status:<11}  {m.valor:<9}  {m.detalhe}")

    motivos = motivos_reversao(relatorio)
    print()
    if motivos:
        print("== MOTIVO PARA REVERTER ==")
        for motivo in motivos:
            print(f"  - {motivo}")
    else:
        print("Nenhum limiar de reversão cruzado.")


async def _rodar_e_imprimir(pool: AsyncConnectionPool, settings: Settings) -> int:
    relatorio = await rodar_monitoramento(pool=pool, settings=settings)
    _imprimir(relatorio)
    return 1 if motivos_reversao(relatorio) else 0


def main() -> int:
    """Ponto de entrada do CLI. Uso: ``python scripts/monitorar_cutover.py``."""
    settings = Settings()
    return asyncio.run(_main_com_settings(settings))


async def _main_com_settings(settings: Settings) -> int:
    pool = await get_pool()
    return await _rodar_e_imprimir(pool, settings)


if __name__ == "__main__":
    sys.exit(main())
