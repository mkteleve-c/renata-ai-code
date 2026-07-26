"""Acesso a leads_crm e o gate de ingestão do SDR.

A ORDEM das regras importa e replica o SQL `Add New Lead` do n8n:
a checagem de agent_active vem ANTES do upsert. Um lead em handover não
pode ter followup_count zerado nem last_interaction_at renovado — se
tivesse, ao ser reativado receberia a escada de follow-up do zero.
"""

from dataclasses import dataclass
from typing import Any

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.phone import resolver_telefone, variacoes

logger = structlog.get_logger()


@dataclass
class ResultadoGate:
    aceito: bool
    motivo: str | None = None
    canonico: str | None = None
    lead: dict[str, Any] | None = None


async def aplicar_gate(
    pool: AsyncConnectionPool,
    key: dict[str, Any],
    push_name: str | None,
) -> ResultadoGate:
    canonico = resolver_telefone(key)
    if not canonico:
        return ResultadoGate(False, "telefone_invalido")

    com_9, sem_9 = variacoes(canonico)

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("select 1 from blocklist where phone = %s", (canonico,))
        if await cur.fetchone():
            return ResultadoGate(False, "blocklist", canonico)

        await cur.execute(
            "select * from leads_crm where phone in (%s, %s) "
            "order by last_interaction_at desc nulls last limit 1",
            (com_9, sem_9),
        )
        lead = await cur.fetchone()

        if key.get("fromMe") is True:
            if lead:
                await cur.execute(
                    "update leads_crm set agent_active = false, "
                    "followup_active = false, agent_reactivate_at = null "
                    "where phone = %s",
                    (lead["phone"],),
                )
            return ResultadoGate(False, "from_me", canonico)

        if lead and lead["agent_active"] is False:
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
