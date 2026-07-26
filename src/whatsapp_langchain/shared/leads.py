"""Acesso a leads_crm e o gate de ingestão do SDR.

A ORDEM das regras importa e replica o SQL `Add New Lead` do n8n:
a checagem de agent_active vem ANTES do upsert. Um lead em handover não
pode ter followup_count zerado nem last_interaction_at renovado — se
tivesse, ao ser reativado receberia a escada de follow-up do zero. Pela
mesma razão, uma duplicata (telefone gravado com e sem o 9º dígito) só é
consolidada em disco DEPOIS dessa checagem — a fusão nunca pode escrever
nada para um lead pausado.

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

# agendou_sessao vence tudo: reunião marcada é fato verificável (existe
# evento no Google Calendar), enquanto desqualificado/perdido são julgamento.
# Fases desconhecidas ou nulas (a coluna é nullable) ficam abaixo de
# qualquer fase real — nunca derrubam uma fase concreta na fusão.
_FASE_RANK = {
    "formulario_preenchido": 1,
    "iniciou_conversa": 2,
    "qualificado": 3,
    "desqualificado": 4,
    "perdido": 4,
    "agendou_sessao": 5,
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


def _rank_fase(fase: str | None) -> int:
    """None fica abaixo de qualquer fase, real ou desconhecida.

    Fase desconhecida (um valor novo do enum que este código ainda não
    conhece) usa 0, não -1: precisa continuar vencendo `None` para que uma
    fase nova nunca seja apagada por uma linha sem fase nenhuma.
    """
    if fase is None:
        return -1
    return _FASE_RANK.get(fase, 0)


def _mais_avancada(fase_a: str | None, fase_b: str | None) -> str | None:
    return fase_a if _rank_fase(fase_a) >= _rank_fase(fase_b) else fase_b


def _mais_recente(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    ta, tb = a["last_interaction_at"], b["last_interaction_at"]
    if ta is None:
        return b
    if tb is None:
        return a
    return a if ta >= tb else b


def _vencedor_pausa(canonica: dict[str, Any], legada: dict[str, Any]) -> dict[str, Any]:
    """Decide de qual linha vêm agent_active, followup_active e agent_reactivate_at.

    False vence: errar para o lado de não mandar mensagem é recuperável,
    mandar para quem pediu silêncio não é. agent_reactivate_at acompanha
    essa mesma linha e nunca é coalescido com a outra — ali NULL é estado
    significativo ("nenhuma reativação agendada"), não ausência de dado.
    """
    if canonica["agent_active"] is False and legada["agent_active"] is False:
        return _mais_recente(canonica, legada)
    if canonica["agent_active"] is False:
        return canonica
    if legada["agent_active"] is False:
        return legada
    return _mais_recente(canonica, legada)


def _fundir(canonica: dict[str, Any], legada: dict[str, Any]) -> dict[str, Any]:
    """Funde duas linhas do mesmo lead num dict só, sem tocar o banco.

    Campos de conteúdo: vale o valor de quem tem last_interaction_at mais
    recente, caindo para o valor da outra linha quando nulo. Fase: a mais
    avançada das duas — nunca a mais recente. Pausa do agente: ver
    `_vencedor_pausa`. Puro por design — quem chama decide se e quando
    persistir o resultado.
    """
    recente = _mais_recente(canonica, legada)
    antiga = legada if recente is canonica else canonica

    mesclado = {
        campo: recente[campo] if recente[campo] is not None else antiga[campo]
        for campo in _CAMPOS_MESCLAVEIS
    }

    vencedor = _vencedor_pausa(canonica, legada)
    mesclado["agent_active"] = vencedor["agent_active"]
    # Não minimizado à parte: segue o vencedor de agent_active, não é
    # observável em nenhum caminho (descarte não escreve, aceite força true).
    mesclado["followup_active"] = vencedor["followup_active"]
    mesclado["agent_reactivate_at"] = vencedor["agent_reactivate_at"]

    mesclado["phase"] = _mais_avancada(canonica["phase"], legada["phase"])
    return mesclado


async def _persistir_consolidacao(
    cur: AsyncCursor[DictRow],
    canonico: str,
    mesclado: dict[str, Any],
    legada_phone: str,
) -> dict[str, Any]:
    """Grava a fusão de `_fundir` na linha canônica e apaga a legada.

    Um UPDATE que movesse `phone` de uma linha para a outra colidiria com a
    PK — por isso a identidade que sobrevive é sempre a canônica, e a legada
    é apagada à parte.
    """
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
    if consolidada is None:
        raise RuntimeError(f"consolidação não encontrou a linha canônica de {canonico}")

    await cur.execute("delete from leads_crm where phone = %s", (legada_phone,))

    logger.info(
        "leads_duplicata_consolidada",
        canonico=canonico,
        legada=legada_phone,
        fase_final=mesclado["phase"],
    )

    return consolidada


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

        await cur.execute(
            "select * from leads_crm where phone in (%s, %s) order by phone",
            (com_9, sem_9),
        )
        linhas = await cur.fetchall()

        # Fusão só computada em memória aqui — nada é escrito até sabermos
        # que a mensagem não vai ser descartada por fromMe ou agent_active.
        legada: dict[str, Any] | None = None
        mesclado: dict[str, Any] | None = None
        if not linhas:
            lead_view = None
        elif len(linhas) == 1:
            lead_view = linhas[0]
        else:
            canonica = next(linha for linha in linhas if linha["phone"] == sem_9)
            legada = next(linha for linha in linhas if linha["phone"] == com_9)
            mesclado = _fundir(canonica, legada)
            lead_view = {**mesclado, "phone": canonico}

        if key.get("fromMe") is True:
            # Curto-circuito quando o UPDATE seria no-op: fromMe é o único
            # caminho do gate com escrita destrutiva e o único sem rate limit
            # (ele não pode ter — é o handover do atendente). Sem isto, uma
            # rajada de fromMe reescreve as mesmas linhas indefinidamente.
            ja_pausado = bool(linhas) and all(
                linha["agent_active"] is False
                and linha["followup_active"] is False
                and linha["agent_reactivate_at"] is None
                for linha in linhas
            )
            if linhas and not ja_pausado:
                await cur.execute(
                    "update leads_crm set agent_active = false, "
                    "followup_active = false, agent_reactivate_at = null "
                    "where phone in (%s, %s)",
                    (com_9, sem_9),
                )
            # Três estados distintos, não dois: "sem lead" não é "não estava
            # pausado" — não havia o que pausar.
            if not linhas:
                pausa = "sem_lead"
            elif ja_pausado:
                pausa = "ja_pausado"
            else:
                pausa = "aplicada"
            logger.info(
                "gate_descartado",
                motivo="from_me",
                telefone=canonico,
                pausa=pausa,
            )
            return ResultadoGate(False, "from_me", canonico)

        if lead_view and lead_view["agent_active"] is False:
            logger.info("gate_descartado", motivo="agente_desligado", telefone=canonico)
            return ResultadoGate(False, "agente_desligado", canonico, lead_view)

        if legada is not None and mesclado is not None:
            lead = await _persistir_consolidacao(
                cur, canonico, mesclado, legada["phone"]
            )
        else:
            lead = lead_view

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
