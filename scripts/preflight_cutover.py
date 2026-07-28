"""Pré-voo do cutover (Fase 5, Task 2): "dá para virar a chave agora?"

Roda contra o ambiente que vai virar produção e responde sim/não, listando
exatamente o que falta. Não é um smoke test do sistema novo -- é a última
checagem antes de desligar o n8n, que está atendendo gente agora.

**Nenhuma checagem escreve.** A do Google lê a agenda (`listar_eventos`,
nunca `criar_evento`); a do Pipedrive lê `/stages`, nunca move um card; a do
OpenRouter chama `GET /key` (custo zero, sem completion) em vez de gerar
texto. Se uma checagem só pudesse ser feita escrevendo, ela não entraria
aqui -- um pré-voo honesto e incompleto vale mais que um que cria um evento
de teste na agenda do Silvio.

O CHECK de `leads_crm` é lido de `pg_get_constraintdef` contra o banco vivo,
nunca do arquivo da migração 014. É o modo de falha que a Fase 3 documentou:
um teste (ou uma checagem) que lê o arquivo fica verde contra uma constraint
velha -- ver `test_check_ddl_do_arquivo_e_executado_...` em
`tests/integration/test_migracao_014.py`, que existe exatamente por causa
disso.

Cada checagem devolve uma `Checagem(nome, ok, detalhe)`. `detalhe` é sempre
seguro para stdout: nunca o valor de uma credencial, só nomes de variável
ausentes, códigos de status HTTP e contagens. O código de saída do processo
é `0` só quando as doze passam -- é o que um roteiro de cutover lê, não a
tabela impressa.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.config import MIN_PRODUCTION_SECRET_LENGTH, Settings
from whatsapp_langchain.shared.db import MIGRATIONS_DIR, get_pool
from whatsapp_langchain.shared.google_calendar import (
    GoogleCalendarClient,
    GoogleCalendarError,
)
from whatsapp_langchain.shared.phone import canonicalizar
from whatsapp_langchain.shared.pipedrive import PIPEDRIVE_API_BASE

TIMEOUT = httpx.Timeout(15.0)

# Nome da constraint que a migração 014 instala. É só um identificador para
# a consulta a pg_get_constraintdef -- o conteúdo das três cláusulas abaixo
# vem sempre do banco vivo, nunca deste literal nem do arquivo .sql.
_CHECK_NOME = "leads_crm_phone_canonico_check"
_CLAUSULAS_ESPERADAS = (
    r"55[0-9]{2}9[0-9]{8}",
    r"^550",
    r"[0-9]{10,11}",
)


@dataclass(frozen=True)
class Checagem:
    """Resultado de uma checagem do pré-voo.

    `detalhe` nunca carrega valor de credencial -- só nome de variável
    ausente, código de status HTTP, contagem ou trecho de definição de SQL
    (que é dado de schema, não segredo).
    """

    nome: str
    ok: bool
    detalhe: str


@dataclass(frozen=True)
class Transportes:
    """Dublês de transporte HTTP para os testes -- nunca a rede real.

    Um campo por serviço externo, todos opcionais. Em produção os quatro
    ficam `None` e cada `httpx.AsyncClient` usa o transporte real.
    """

    evolution: httpx.AsyncBaseTransport | None = None
    google: httpx.AsyncBaseTransport | None = None
    pipedrive: httpx.AsyncBaseTransport | None = None
    openrouter: httpx.AsyncBaseTransport | None = None


# --- Banco -------------------------------------------------------------


async def checar_migracoes(conn: AsyncConnection) -> Checagem:
    """Toda migração presente em `db/migrations/` está registrada em `_migrations`.

    Deriva a lista esperada do próprio diretório que `run_migrations` lê
    (`MIGRATIONS_DIR`, `shared/db.py`) em vez de hardcodar "001..015" --
    hoje são exatamente esses quinze arquivos, e a checagem continua
    correta se um 016 for acrescentado antes do próximo cutover.

    `esperadas` vazio -- `MIGRATIONS_DIR` inexistente, apagado, ou apontando
    para o lugar errado -- FALHA explicitamente em vez de passar vacuamente.
    Sem esta guarda, `faltando = []` (nada esperado, nada pode faltar) e a
    checagem reporta `True :: 0/0 aplicadas`, verde, exatamente no cenário em
    que ela é incapaz de confirmar que a 015 (ou qualquer outra) foi
    aplicada -- a única das doze checagens do pré-voo que cobre a 015.
    """
    nome = "Migrações aplicadas"
    esperadas = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    if not esperadas:
        return Checagem(
            nome,
            False,
            f"nenhum arquivo .sql encontrado em {MIGRATIONS_DIR} -- "
            "MIGRATIONS_DIR errado ou vazio, checagem não pode confirmar nada",
        )

    cur = await conn.execute("select name from _migrations")
    aplicadas = {row[0] for row in await cur.fetchall()}

    faltando = [n for n in esperadas if n not in aplicadas]
    if faltando:
        return Checagem(nome, False, f"faltando: {', '.join(faltando)}")
    return Checagem(nome, True, f"{len(esperadas)}/{len(esperadas)} aplicadas")


def _clausula_nega_com_padrao(definicao: str, padrao: str) -> bool:
    """`True` só se `padrao` aparece dentro de um literal negado (`!~ '...'`).

    Checagem por SUBSTRING pura (`padrao in definicao`) casaria igual se
    alguém trocasse `!~` por `~` na constraint viva -- o padrão em si (o
    texto da regex) não muda quando o operador é invertido, só o
    comportamento (aceita em vez de rejeitar). Essa troca inverteria a
    constraint inteira (passa a EXIGIR o formato que deveria PROIBIR) e a
    checagem antiga ficaria verde do mesmo jeito. Aqui, o operador `!~`
    precisa aparecer imediatamente antes da aspa que abre o literal que
    contém `padrao` -- é o que amarra "negado" a "este padrão específico",
    não só "negado em algum lugar da definição".
    """
    regex = r"!~\s*'[^']*" + re.escape(padrao) + r"[^']*'"
    return re.search(regex, definicao) is not None


async def checar_check_constraint(conn: AsyncConnection) -> Checagem:
    """O CHECK da 014 está presente, com as três cláusulas, e as três NEGADAS
    (`!~`, não `~`) -- lido do banco.

    `pg_get_constraintdef`, nunca o texto do arquivo de migração: é
    exatamente o modo de falha que a Fase 3 documentou -- um teste (ou uma
    checagem) que lê o arquivo fica verde contra uma constraint velha.
    """
    nome = "CHECK de leads_crm.phone com as três cláusulas negadas"
    cur = await conn.execute(
        "select pg_get_constraintdef(oid) from pg_constraint "
        "where conname = %s and conrelid = 'leads_crm'::regclass",
        (_CHECK_NOME,),
    )
    linha = await cur.fetchone()
    if linha is None:
        return Checagem(nome, False, f"constraint '{_CHECK_NOME}' não existe")

    definicao = linha[0]
    faltando = [c for c in _CLAUSULAS_ESPERADAS if c not in definicao]
    if faltando:
        return Checagem(
            nome, False, f"CHECK sem a(s) cláusula(s): {', '.join(faltando)}"
        )

    nao_negadas = [
        c for c in _CLAUSULAS_ESPERADAS if not _clausula_nega_com_padrao(definicao, c)
    ]
    if nao_negadas:
        return Checagem(
            nome,
            False,
            f"cláusula(s) presente(s) mas não negada(s) com '!~' -- "
            f"constraint pode estar invertida (aceita em vez de proibir): "
            f"{', '.join(nao_negadas)}",
        )

    return Checagem(nome, True, "as três cláusulas presentes e negadas no CHECK vivo")


async def checar_leads_crm_vazia(conn: AsyncConnection) -> Checagem:
    """`leads_crm` precisa estar vazia -- o cutover é a primeira carga.

    Uma linha preexistente muda o resultado da fusão que `migrar_supabase.py
    --executar` faz: ela entraria como se fosse um lado do grupo, sem nunca
    ter vindo do Supabase.
    """
    nome = "leads_crm vazia"
    cur = await conn.execute("select count(*) from leads_crm")
    linha = await cur.fetchone()
    assert linha is not None
    total = linha[0]
    if total > 0:
        return Checagem(nome, False, f"{total} linha(s) em leads_crm, esperado 0")
    return Checagem(nome, True, "0 linhas")


# --- Configuração pura (sem rede, sem banco) -----------------------------


def checar_outbound_evolution(settings: Settings) -> Checagem:
    nome = "OUTBOUND_MODE=real e canal Evolution completo"
    modo = settings.resolved_outbound_mode
    if modo != "real":
        return Checagem(nome, False, f"OUTBOUND_MODE resolvido é '{modo}', não 'real'")

    status = settings.channel_status()["evolution"]
    if not status["touched"]:
        return Checagem(
            nome, False, "canal Evolution sem nenhuma credencial preenchida"
        )
    if not status["complete"]:
        faltando = ", ".join(status["missing"])  # type: ignore[arg-type]
        return Checagem(
            nome, False, f"canal Evolution incompleto -- faltam: {faltando}"
        )
    return Checagem(nome, True, "OUTBOUND_MODE=real, canal Evolution completo")


def checar_webhook_secret(settings: Settings) -> Checagem:
    nome = "EVOLUTION_WEBHOOK_SECRET com 32+ caracteres"
    secret = settings.evolution_webhook_secret.strip()
    if not secret:
        return Checagem(nome, False, "EVOLUTION_WEBHOOK_SECRET ausente")
    if len(secret) < MIN_PRODUCTION_SECRET_LENGTH:
        return Checagem(
            nome,
            False,
            f"{len(secret)} caractere(s), mínimo {MIN_PRODUCTION_SECRET_LENGTH}",
        )
    return Checagem(nome, True, f"{len(secret)} caracteres")


def checar_internal_service_token(settings: Settings) -> Checagem:
    nome = "INTERNAL_SERVICE_TOKEN forte"
    token = settings.internal_service_token.strip()
    if not token:
        return Checagem(nome, False, "INTERNAL_SERVICE_TOKEN ausente")
    if len(token) < MIN_PRODUCTION_SECRET_LENGTH:
        return Checagem(
            nome,
            False,
            f"{len(token)} caractere(s), mínimo {MIN_PRODUCTION_SECRET_LENGTH}",
        )
    return Checagem(nome, True, f"{len(token)} caracteres")


def checar_followup_desligado(settings: Settings) -> Checagem:
    nome = "FOLLOWUP_ENABLED=false"
    if settings.followup_enabled:
        return Checagem(
            nome,
            False,
            "FOLLOWUP_ENABLED=true -- a régua liga depois, como decisão separada",
        )
    return Checagem(nome, True, "desligado")


def checar_handover_phone(settings: Settings) -> Checagem:
    nome = "HANDOVER_NOTIFY_PHONE preenchido e canonicalizável"
    bruto = settings.handover_notify_phone.strip()
    if not bruto:
        return Checagem(nome, False, "ausente -- handover ficaria silencioso")
    if canonicalizar(bruto) is None:
        return Checagem(nome, False, "preenchido, mas não é um telefone reconhecível")
    return Checagem(nome, True, "preenchido e canonicalizável")


# --- Serviços externos (só leitura) --------------------------------------


def _safe_json(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except Exception:
        return None


def _status_da_instancia(
    dados: Any, nome_instancia: str, profundidade: int = 3
) -> str | None:
    """Busca o status da instância em qualquer um dos shapes plausíveis.

    Sem especificação fechada do formato de resposta de `fetchInstances`
    disponível neste repositório (só o dump `name=... status=...
    integration=...` em `docs/EVOLUTION.md`), a extração aceita tanto chaves
    de topo (`name`/`status`) quanto aninhadas em `instance`
    (`instanceName`/`connectionStatus`/`state`) -- mesmo espírito defensivo
    de `_extract_message_id` em `worker/evolution_client.py`.
    """
    if profundidade <= 0:
        return None

    if isinstance(dados, list):
        for item in dados:
            achado = _status_da_instancia(item, nome_instancia, profundidade - 1)
            if achado is not None:
                return achado
        return None

    if not isinstance(dados, dict):
        return None

    candidato_nome = dados.get("name") or dados.get("instanceName")
    candidato_status = (
        dados.get("status") or dados.get("connectionStatus") or dados.get("state")
    )
    if candidato_nome == nome_instancia and isinstance(candidato_status, str):
        return candidato_status

    aninhado = dados.get("instance")
    if isinstance(aninhado, dict):
        achado = _status_da_instancia(aninhado, nome_instancia, profundidade - 1)
        if achado is not None:
            return achado

    return None


async def checar_evolution_alcancavel(
    settings: Settings, transporte: httpx.AsyncBaseTransport | None = None
) -> Checagem:
    """`GET /instance/fetchInstances` -- lê o status, nunca envia mensagem."""
    nome = "Evolution alcançável e instância conectada"
    if not (
        settings.evolution_base_url
        and settings.evolution_api_key
        and settings.evolution_instance
    ):
        return Checagem(nome, False, "canal Evolution não configurado")

    try:
        async with httpx.AsyncClient(transport=transporte, timeout=TIMEOUT) as client:
            resposta = await client.get(
                f"{settings.evolution_base_url.rstrip('/')}/instance/fetchInstances",
                headers={"apikey": settings.evolution_api_key},
            )
    except httpx.RequestError as exc:
        return Checagem(nome, False, f"falha de transporte: {type(exc).__name__}")

    if resposta.status_code >= 400:
        return Checagem(nome, False, f"Evolution respondeu {resposta.status_code}")

    status = _status_da_instancia(_safe_json(resposta), settings.evolution_instance)
    if status is None:
        return Checagem(
            nome, False, f"instância '{settings.evolution_instance}' não encontrada"
        )
    if status.lower() not in {"open", "connected"}:
        return Checagem(
            nome, False, f"instância com status '{status}', esperado 'open'"
        )
    return Checagem(nome, True, f"instância conectada (status={status})")


async def checar_google_calendar(
    settings: Settings, transporte: httpx.AsyncBaseTransport | None = None
) -> Checagem:
    """`listar_eventos` numa janela de 1 minuto -- um GET, nunca um POST de escrita.

    O refresh do access token É um POST, mas para o endpoint OAuth do
    Google (autenticação), não para a API de calendário -- não escreve
    nenhum dado de agenda. `criar_evento`/`atualizar_evento` nunca são
    chamados aqui.
    """
    nome = "Google Calendar: refresh token válido"
    campos = (
        settings.google_client_id,
        settings.google_client_secret,
        settings.google_refresh_token,
        settings.google_calendar_id,
    )
    if not all(campos):
        return Checagem(nome, False, "Google Calendar não configurado")

    cliente = GoogleCalendarClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        refresh_token=settings.google_refresh_token,
        calendar_id=settings.google_calendar_id,
    )
    cliente._transport = transporte

    agora = datetime.now(tz=UTC)
    try:
        await cliente.listar_eventos(agora, agora + timedelta(minutes=1))
    except GoogleCalendarError as exc:
        return Checagem(nome, False, f"Google Calendar respondeu {exc.status_code}")
    return Checagem(nome, True, "refresh token válido, agenda alcançada")


async def checar_pipedrive(
    settings: Settings, transporte: httpx.AsyncBaseTransport | None = None
) -> Checagem:
    """`GET /stages` -- lê os estágios, nunca move um card.

    Autentica por header `x-api-token`, nunca `?api_token=` na query string
    -- mesma defesa que `shared/pipedrive.py` documenta para `mover_card`:
    o token na URL acaba em qualquer log ou mensagem de erro que capture a
    URL inteira.
    """
    nome = "Pipedrive alcançável, estágios qualificado e agendado existem"
    token = settings.pipedrive_api_token.strip()
    if not token:
        return Checagem(nome, False, "PIPEDRIVE_API_TOKEN ausente")

    try:
        async with httpx.AsyncClient(transport=transporte, timeout=TIMEOUT) as client:
            resposta = await client.get(
                f"{PIPEDRIVE_API_BASE}/stages",
                headers={"x-api-token": token},
            )
    except httpx.RequestError as exc:
        return Checagem(nome, False, f"falha de transporte: {type(exc).__name__}")

    if resposta.status_code >= 400:
        return Checagem(nome, False, f"Pipedrive respondeu {resposta.status_code}")

    dados = _safe_json(resposta)
    if not isinstance(dados, dict) or not isinstance(dados.get("data"), list):
        return Checagem(nome, False, "resposta do Pipedrive em formato inesperado")

    existentes = {item.get("id") for item in dados["data"] if isinstance(item, dict)}
    esperados = {
        settings.pipedrive_stage_qualificado,
        settings.pipedrive_stage_agendado,
    }
    faltando = sorted(esperados - existentes)
    if faltando:
        return Checagem(nome, False, f"estágio(s) ausente(s) no Pipedrive: {faltando}")
    return Checagem(nome, True, f"estágios {sorted(esperados)} confirmados")


async def checar_openrouter(
    settings: Settings, transporte: httpx.AsyncBaseTransport | None = None
) -> Checagem:
    """`GET /key` -- confere a chave sem gastar um token de completion."""
    nome = "OPENROUTER_API_KEY responde"
    chave = settings.openrouter_api_key
    valor = chave.get_secret_value().strip() if chave else ""
    if not valor:
        return Checagem(nome, False, "OPENROUTER_API_KEY ausente")

    try:
        async with httpx.AsyncClient(transport=transporte, timeout=TIMEOUT) as client:
            resposta = await client.get(
                f"{settings.openrouter_base_url.rstrip('/')}/key",
                headers={"Authorization": f"Bearer {valor}"},
            )
    except httpx.RequestError as exc:
        return Checagem(nome, False, f"falha de transporte: {type(exc).__name__}")

    if resposta.status_code >= 400:
        return Checagem(nome, False, f"OpenRouter respondeu {resposta.status_code}")
    return Checagem(nome, True, "chave válida")


# --- Orquestração ----------------------------------------------------------


async def rodar_preflight(
    *,
    settings: Settings,
    pool: AsyncConnectionPool,
    transportes: Transportes | None = None,
) -> list[Checagem]:
    """Roda as doze checagens, na ordem do roteiro de cutover.

    As três de banco compartilham UMA conexão -- não porque precisem de
    transação em comum (nenhuma escreve), mas para não abrir três conexões
    do pool para um comando que roda uma vez e sai.
    """
    transportes = transportes or Transportes()

    async with pool.connection() as conn:
        checagens_banco = [
            await checar_migracoes(conn),
            await checar_check_constraint(conn),
            await checar_leads_crm_vazia(conn),
        ]

    return [
        *checagens_banco,
        checar_outbound_evolution(settings),
        checar_webhook_secret(settings),
        await checar_evolution_alcancavel(settings, transportes.evolution),
        await checar_google_calendar(settings, transportes.google),
        await checar_pipedrive(settings, transportes.pipedrive),
        checar_internal_service_token(settings),
        checar_followup_desligado(settings),
        checar_handover_phone(settings),
        await checar_openrouter(settings, transportes.openrouter),
    ]


def codigo_de_saida(checagens: list[Checagem]) -> int:
    """`0` só quando todas as checagens passam -- é o que o roteiro lê."""
    return 0 if all(c.ok for c in checagens) else 1


def _imprimir_tabela(checagens: list[Checagem]) -> None:
    largura = max(len(c.nome) for c in checagens)
    print(f"{'CHECAGEM':<{largura}}  STATUS  DETALHE")
    print(f"{'-' * largura}  ------  {'-' * 40}")
    for c in checagens:
        status = "OK" if c.ok else "FALHOU"
        print(f"{c.nome:<{largura}}  {status:<6}  {c.detalhe}")

    total = len(checagens)
    falhas = [c for c in checagens if not c.ok]
    print()
    if falhas:
        print(f"{len(falhas)}/{total} checagem(ns) falharam -- NÃO virar a chave.")
    else:
        print(f"{total}/{total} checagens passaram -- pré-voo ok.")


async def _rodar_e_imprimir(settings: Settings, pool: AsyncConnectionPool) -> int:
    checagens = await rodar_preflight(settings=settings, pool=pool)
    _imprimir_tabela(checagens)
    return codigo_de_saida(checagens)


def main() -> int:
    """Ponto de entrada do CLI. Uso: ``python scripts/preflight_cutover.py``."""
    settings = Settings()
    return asyncio.run(_main_com_settings(settings))


async def _main_com_settings(settings: Settings) -> int:
    pool = await get_pool()
    return await _rodar_e_imprimir(settings, pool)


if __name__ == "__main__":
    sys.exit(main())
