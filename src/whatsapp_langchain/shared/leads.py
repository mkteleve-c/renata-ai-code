"""Acesso a leads_crm e o gate de ingestão do SDR.

A ORDEM das regras importa e replica o SQL `Add New Lead` do n8n:
a checagem de agent_active vem ANTES do upsert. Um lead em handover não
pode ter followup_count zerado nem last_interaction_at renovado — se
tivesse, ao ser reativado receberia a escada de follow-up do zero.

Cada mensagem chega como um webhook HTTP separado e o FastAPI atende em
paralelo — três mensagens seguidas do mesmo lead são o caso normal, não
exceção. Por isso o gate inteiro roda sob `pg_advisory_xact_lock` chaveado
pelo telefone canônico (mesmo idioma de `shared/queue.py`), serializando
SELECT+UPDATE/INSERT do mesmo lead e eliminando lost update, UniqueViolation
em rajada de lead novo e corrida entre handover humano (fromMe) e mensagem
concorrente do lead.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

import structlog
from psycopg import AsyncCursor
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.phone import resolver_telefone, variacoes

logger = structlog.get_logger()

_FASE_RANK = {
    "formulario_preenchido": 0,
    "iniciou_conversa": 1,
    "qualificado": 2,
    "agendou_sessao": 3,
    "desqualificado": 4,
    "perdido": 4,
}

_CAMPOS_MESCLAVEIS = (
    "pipedriveid",
    "name",
    "username",
    "email",
    "faturamento_mensal",
    "qualificacao_notas",
    "google_event_id",
    "source",
    "metadata",
    "agent_active",
    "followup_active",
    "agent_reactivate_at",
    "followup_count",
)


@dataclass
class ResultadoGate:
    aceito: bool
    motivo: str | None = None
    canonico: str | None = None
    lead: dict[str, Any] | None = None


def _lock_key(canonico: str) -> int:
    return int.from_bytes(
        hashlib.sha256(canonico.encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _mais_avancada(fase_a: str, fase_b: str) -> str:
    return fase_a if _FASE_RANK[fase_a] >= _FASE_RANK[fase_b] else fase_b


def _mais_recente(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    ta, tb = a["last_interaction_at"], b["last_interaction_at"]
    if ta is None:
        return b
    if tb is None:
        return a
    return a if ta >= tb else b


async def _consolidar_duplicata(
    cur: AsyncCursor[DictRow],
    canonico: str,
    canonica: dict[str, Any],
    legada: dict[str, Any],
) -> dict[str, Any]:
    """Funde a linha legada (telefone com o 9º dígito) na canônica e apaga a legada.

    Base legada gravando o telefone com o 9º dígito e o gate novo gravando
    sem ele convivem: as duas formas viram linhas distintas da mesma pessoa,
    e um UPDATE que muda phone de uma para a outra colidiria com a PK. Regra
    de fusão: campo a campo vale o valor de quem tem last_interaction_at mais
    recente, caindo para o valor da outra linha quando nulo; a fase mantida é
    sempre a mais avançada das duas, nunca a mais recente.
    """
    recente = _mais_recente(canonica, legada)
    antiga = legada if recente is canonica else canonica

    mesclado = {
        campo: recente[campo] if recente[campo] is not None else antiga[campo]
        for campo in _CAMPOS_MESCLAVEIS
    }
    mesclado["phase"] = _mais_avancada(canonica["phase"], legada["phase"])

    await cur.execute(
        "update leads_crm set"
        "  pipedriveid = %s, name = %s, username = %s, email = %s,"
        "  faturamento_mensal = %s, qualificacao_notas = %s, google_event_id = %s,"
        "  source = %s, metadata = %s, agent_active = %s, followup_active = %s,"
        "  agent_reactivate_at = %s, followup_count = %s, phase = %s"
        " where phone = %s returning *",
        (
            mesclado["pipedriveid"],
            mesclado["name"],
            mesclado["username"],
            mesclado["email"],
            mesclado["faturamento_mensal"],
            mesclado["qualificacao_notas"],
            mesclado["google_event_id"],
            mesclado["source"],
            Jsonb(mesclado["metadata"]) if mesclado["metadata"] is not None else None,
            mesclado["agent_active"],
            mesclado["followup_active"],
            mesclado["agent_reactivate_at"],
            mesclado["followup_count"],
            mesclado["phase"],
            canonico,
        ),
    )
    consolidada = await cur.fetchone()
    assert consolidada is not None

    await cur.execute("delete from leads_crm where phone = %s", (legada["phone"],))

    logger.info(
        "leads_duplicata_consolidada",
        canonico=canonico,
        legada=legada["phone"],
        fase_final=mesclado["phase"],
    )

    return consolidada


async def _resolver_lead(
    cur: AsyncCursor[DictRow], canonico: str, com_9: str, sem_9: str
) -> dict[str, Any] | None:
    await cur.execute(
        "select * from leads_crm where phone in (%s, %s) order by phone",
        (com_9, sem_9),
    )
    linhas = await cur.fetchall()

    if not linhas:
        return None
    if len(linhas) == 1:
        return linhas[0]

    canonica = next(linha for linha in linhas if linha["phone"] == sem_9)
    legada = next(linha for linha in linhas if linha["phone"] == com_9)
    return await _consolidar_duplicata(cur, canonico, canonica, legada)


async def aplicar_gate(
    pool: AsyncConnectionPool,
    key: dict[str, Any],
    push_name: str | None,
) -> ResultadoGate:
    canonico = resolver_telefone(key)
    if not canonico:
        logger.info(
            "gate_descartado",
            motivo="telefone_invalido",
            remote_jid=key.get("remoteJid"),
        )
        return ResultadoGate(False, "telefone_invalido")

    com_9, sem_9 = variacoes(canonico)

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # Lock transacional: serializa o gate inteiro para o mesmo lead.
        # Liberado automaticamente no commit/rollback desta transação.
        await cur.execute("select pg_advisory_xact_lock(%s)", (_lock_key(canonico),))

        await cur.execute("select 1 from blocklist where phone = %s", (canonico,))
        if await cur.fetchone():
            logger.info("gate_descartado", motivo="blocklist", telefone=canonico)
            return ResultadoGate(False, "blocklist", canonico)

        lead = await _resolver_lead(cur, canonico, com_9, sem_9)

        if key.get("fromMe") is True:
            if lead:
                await cur.execute(
                    "update leads_crm set agent_active = false, "
                    "followup_active = false, agent_reactivate_at = null "
                    "where phone = %s",
                    (lead["phone"],),
                )
            logger.info("gate_descartado", motivo="from_me", telefone=canonico)
            return ResultadoGate(False, "from_me", canonico)

        if lead and lead["agent_active"] is False:
            logger.info("gate_descartado", motivo="agente_desligado", telefone=canonico)
            return ResultadoGate(False, "agente_desligado", canonico, lead)

        if lead:
            await cur.execute(
                "update leads_crm set "
                "  phone = %s,"
                "  last_interaction_at = now(),"
                "  followup_count = 0,"
                "  followup_active = true,"
                "  name = coalesce(nullif(%s, ''), name),"
                "  source = coalesce(source, 'whatsapp_direct'::lead_source),"
                "  phase = case when phase = 'formulario_preenchido'"
                "               then 'iniciou_conversa'::lead_phase else phase end "
                "where phone = %s returning *",
                (sem_9, push_name or "", lead["phone"]),
            )
        else:
            await cur.execute(
                "insert into leads_crm (phone, name, source, phase) "
                "values (%s, nullif(%s, ''), 'whatsapp_direct', 'iniciou_conversa') "
                "returning *",
                (sem_9, push_name or ""),
            )

        atualizado = await cur.fetchone()

    return ResultadoGate(True, None, canonico, atualizado)
