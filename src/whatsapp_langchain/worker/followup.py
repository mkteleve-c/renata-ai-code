"""Régua de follow-up do SDR — reivindicação atômica dos três degraus.

**Um envio indevido aqui é WhatsApp para gente de verdade**, não um efeito
colateral interno. O desenho inteiro existe para reduzir esse risco a zero
por construção, não por convenção de quem chama.

Os três degraus (15 min, 1h15, 23h) ancoram em `leads_crm.last_inbound_at` —
o instante em que **o lead** falou —, não em `last_interaction_at`, que
mistura o inbound dele com os nossos próprios envios. Ancorar no envio
anterior torna a escada cumulativa: medido no banco legado, o terceiro
degrau caía numa mediana de 24h28 desde a criação do lead — depois de a
janela de 24h da Cloud API já ter fechado. Ancorado no inbound, o degrau 3
dispara em inbound+23h e entrega com ~55 min de folga real antes do corte
(24h menos `FOLLOWUP_JANELA_MARGEM_MINUTOS`).

Fluxo de cada rodada (`rodada`): reivindicar → enviar → contar bloqueados.
A reivindicação (`reivindicar`) é um único `UPDATE ... RETURNING` que
commita sozinho; o envio HTTP acontece inteiramente depois, fora de
qualquer transação. `ainda_vale_enviar` é a revalidação que cobre o
intervalo entre os dois.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.agents.catalog.elevec_sdr.contexto import (
    NOME_AUSENTE,
    sanitizar_nome,
)
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.phone import to_e164

logger = structlog.get_logger()

_MINUTOS_POR_DIA = 24 * 60


class ClienteOutbound(Protocol):
    async def send_message(self, to: str, body: str, **kwargs: Any) -> str | None: ...


@dataclass(frozen=True)
class LeadReivindicado:
    phone: str
    name: str | None
    nivel: int  # 1, 2 ou 3 — já é o novo followup_count


# `for update skip locked` fica DENTRO da subquery que seleciona os
# candidatos, não no UPDATE externo: só assim duas rodadas concorrentes
# pulam as linhas uma da outra sem bloquear. O predicado de janela
# (`last_inbound_at > now() - ... janela`) fica DENTRO desta mesma cláusula
# de reivindicação — um lead fora da janela não é reivindicado, então não
# queima degrau; reivindicar e descartar depois gastaria o degrau à toa.
#
# `qualificado` está no filtro de fases e NÃO PODE sair dele: a Fase 2
# (`reverter_fase_apos_cancelamento`, em catalog/elevec_sdr/tools/crm.py)
# religa `followup_active` quando uma reunião é cancelada e devolve o lead
# para `qualificado`. Se `qualificado` entrar na régua, esse lead volta a
# ser perseguido — WhatsApp indevido para gente de verdade.
_SQL_REIVINDICAR = """
update leads_crm
set followup_count = followup_count + 1,
    last_interaction_at = now()
where phone in (
    select phone from leads_crm
    where followup_active
      and agent_active
      and phase not in ('agendou_sessao','desqualificado','perdido','qualificado')
      and last_inbound_at is not null
      and (
            (followup_count = 0
             and last_inbound_at < now() - make_interval(mins => %(n1)s))
         or (followup_count = 1
             and last_inbound_at < now() - make_interval(mins => %(n2)s))
         or (followup_count = 2
             and last_inbound_at < now() - make_interval(mins => %(n3)s))
      )
      and last_inbound_at > now() - make_interval(mins => %(janela)s)
    order by last_inbound_at
    limit %(limite)s
    for update skip locked
)
returning phone, name, followup_count
"""

_SQL_BLOQUEADOS_POR_JANELA = """
select count(*) from leads_crm
where followup_active
  and agent_active
  and phase not in ('agendou_sessao','desqualificado','perdido','qualificado')
  and last_inbound_at is not null
  and (
        (followup_count = 0
         and last_inbound_at < now() - make_interval(mins => %(n1)s))
     or (followup_count = 1
         and last_inbound_at < now() - make_interval(mins => %(n2)s))
     or (followup_count = 2
         and last_inbound_at < now() - make_interval(mins => %(n3)s))
  )
  and last_inbound_at <= now() - make_interval(mins => %(janela)s)
"""

_SQL_AINDA_VALE_ENVIAR = """
select agent_active, followup_active, followup_count,
       last_inbound_at, last_interaction_at
from leads_crm
where phone = %s
"""

_SQL_REATIVAR = """
update leads_crm
set agent_active = true, agent_reactivate_at = null
where agent_active = false and agent_reactivate_at < now()
"""


def _janela_minutos(janela_min: int) -> int:
    return _MINUTOS_POR_DIA - janela_min


async def _reivindicar_na_conexao(
    conn: AsyncConnection[Any],
    *,
    limite: int = settings.followup_batch_size,
    n1_min: int = settings.followup_nivel1_minutos,
    n2_min: int = settings.followup_nivel2_minutos,
    n3_min: int = settings.followup_nivel3_minutos,
    janela_min: int = settings.followup_janela_margem_minutos,
) -> list[LeadReivindicado]:
    """Executa a reivindicação na conexão/transação dada. Não commita.

    Exposta separada de `reivindicar` só para o teste de concorrência real
    (`test_duas_transacoes_realmente_sobrepostas_nao_pegam_o_mesmo_lead`),
    que precisa manter a transação aberta manualmente com um
    `asyncio.Event` — `asyncio.gather` de duas corrotinas no mesmo pool não
    garante sobreposição, então sem essa barreira o teste passaria mesmo
    sem `for update skip locked`.
    """
    params = {
        "n1": n1_min,
        "n2": n2_min,
        "n3": n3_min,
        "janela": _janela_minutos(janela_min),
        "limite": limite,
    }
    cur = await conn.execute(_SQL_REIVINDICAR, params)
    linhas = await cur.fetchall()
    return [LeadReivindicado(phone=p, name=n, nivel=c) for p, n, c in linhas]


async def reivindicar(
    pool: AsyncConnectionPool,
    *,
    limite: int = settings.followup_batch_size,
    n1_min: int = settings.followup_nivel1_minutos,
    n2_min: int = settings.followup_nivel2_minutos,
    n3_min: int = settings.followup_nivel3_minutos,
    janela_min: int = settings.followup_janela_margem_minutos,
) -> list[LeadReivindicado]:
    """Reivindica até `limite` leads vencidos e devolve o degrau de cada um.

    **Nenhuma transação fica aberta durante HTTP.** O `UPDATE ... RETURNING`
    roda e commita aqui dentro (o `async with pool.connection()` do psycopg
    commita ao sair sem exceção); o envio pelo canal de WhatsApp acontece
    inteiramente depois, em `rodada`/`_enviar_reivindicados`, sem segurar
    linha nenhuma. Segurar `FOR UPDATE` enquanto se espera a Evolution
    responder é exatamente o defeito que este desenho evita.

    **Sem advisory lock.** O lock do Postgres é por sessão: com pool, a
    conexão volta para o pool ainda segurando o lock, e ele evapora sem
    aviso se essa conexão for reciclada ou reaproveitada por outra rotina.
    `FOR UPDATE SKIP LOCKED` dentro da própria transação curta é suficiente
    e não tem esse risco.

    **`followup_count` do `RETURNING` já é o novo valor** (1, 2 ou 3) —
    é o nível da mensagem a enviar, o mesmo `next_level` que o n8n calculava
    separado como `followup_count + 1`.

    Os degraus (`n1_min`, `n2_min`, `n3_min`) e a janela entram como
    parâmetros com default vindo de `settings`, não lidos lá dentro: testar
    um degrau isolado não pode exigir mexer em env global.
    """
    async with pool.connection() as conn:
        return await _reivindicar_na_conexao(
            conn,
            limite=limite,
            n1_min=n1_min,
            n2_min=n2_min,
            n3_min=n3_min,
            janela_min=janela_min,
        )


async def contar_bloqueados_por_janela(
    pool: AsyncConnectionPool,
    *,
    n1_min: int = settings.followup_nivel1_minutos,
    n2_min: int = settings.followup_nivel2_minutos,
    n3_min: int = settings.followup_nivel3_minutos,
    janela_min: int = settings.followup_janela_margem_minutos,
) -> int:
    """Conta leads que venceram um degrau mas têm a janela de 24h fechada.

    Sem esta métrica, "a régua morreu" (bug) e "não havia ninguém vencido"
    (dia calmo) são indistinguíveis no log de uma rodada.
    """
    params = {
        "n1": n1_min,
        "n2": n2_min,
        "n3": n3_min,
        "janela": _janela_minutos(janela_min),
    }
    async with pool.connection() as conn:
        cur = await conn.execute(_SQL_BLOQUEADOS_POR_JANELA, params)
        linha = await cur.fetchone()
    return linha[0] if linha else 0


async def ainda_vale_enviar(pool: AsyncConnectionPool, phone: str, nivel: int) -> bool:
    """Revalida um lead reivindicado imediatamente antes do envio.

    `reivindicar` commita e só então o envio HTTP acontece — com
    `FOLLOWUP_BATCH_SIZE=10` e envio serial, o último lead do lote pode ser
    tratado minutos depois do claim. Nesse intervalo o lead pode ter escrito
    de novo, e mandar "Fulano?" em cima da mensagem dele é o pior desfecho
    desta régua: é justamente 15 minutos depois de falar que ele mais tende
    a voltar. Aborta se, desde o claim: o lead sumiu (linha não existe mais
    sob este `phone` — uma fusão de duplicata pode ter renomeado a chave),
    `agent_active` virou `false` (inclui um humano pausando pelo ChatWoot no
    meio do lote), `followup_active` virou `false`, `followup_count` não é
    mais o nível reivindicado (o gate de ingestão zera o contador em todo
    inbound aceito), ou `last_inbound_at` ficou mais recente que
    `last_interaction_at` — que `reivindicar` acabou de gravar como o
    instante do claim. Essa última comparação é o que pega o caso em que o
    lead escreveu durante um handover pausado: ali o gate só toca
    `last_inbound_at`, sem mexer em `followup_count`, então só a comparação
    de relógios revela que ele falou de novo.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(_SQL_AINDA_VALE_ENVIAR, (phone,))
        linha = await cur.fetchone()

    if linha is None:
        logger.info("followup_abortado", phone=phone, nivel=nivel, motivo="lead_sumiu")
        return False

    (
        agent_active,
        followup_active,
        followup_count,
        last_inbound_at,
        last_interaction_at,
    ) = linha

    if not agent_active:
        logger.info(
            "followup_abortado",
            phone=phone,
            nivel=nivel,
            motivo="agent_active_false",
        )
        return False

    if not followup_active:
        logger.info(
            "followup_abortado",
            phone=phone,
            nivel=nivel,
            motivo="followup_active_false",
        )
        return False

    if followup_count != nivel:
        logger.info(
            "followup_abortado",
            phone=phone,
            nivel=nivel,
            motivo="nivel_mudou",
            followup_count_atual=followup_count,
        )
        return False

    if (
        last_inbound_at is not None
        and last_interaction_at is not None
        and last_inbound_at > last_interaction_at
    ):
        logger.info(
            "followup_abortado",
            phone=phone,
            nivel=nivel,
            motivo="lead_falou_apos_o_claim",
        )
        return False

    return True


_NIVEL_2 = (
    "Opa, imagino que esteja corrido ai! Só para não perdermos o timing da "
    "sua aplicação, consegue falar agora?"
)


def primeiro_nome(name: str | None) -> str | None:
    """Primeiro nome do lead, ou `None` — nunca texto de preenchimento.

    `sanitizar_nome` (`contexto.py`) não serve sozinha aqui: ela devolve o
    nome inteiro colapsado, e devolve a string literal `"não informado"`
    quando não há nome. Usada sem este filtro, `montar_mensagem(1, None)`
    mandaria "não informado?" para uma pessoa de verdade — o sentinel do
    contexto do prompt vazando para uma mensagem de WhatsApp real.
    """
    limpo = sanitizar_nome(name)
    if limpo == NOME_AUSENTE:
        return None
    primeiro = limpo.split(" ")[0]
    return primeiro or None


def montar_mensagem(nivel: int, name: str | None) -> str:
    """Texto do WhatsApp para o degrau `nivel`. Nunca vaza `None` nem o sentinel."""
    nome = primeiro_nome(name)

    if nivel == 1:
        return f"{nome}?" if nome else "Oi?"

    if nivel == 2:
        return _NIVEL_2

    if nivel == 3:
        resto = "Ainda faz sentido falarmos sobre o seu momento de carreira?"
        return f"{nome}, tudo bem? {resto}" if nome else f"Tudo bem? {resto}"

    raise ValueError(f"nível de follow-up inválido: {nivel}")


async def reativar_agentes(pool: AsyncConnectionPool) -> int:
    """Religa `agent_active` para leads cuja `agent_reactivate_at` já passou.

    Mantida por paridade com a especificação — **hoje é inerte**: nada em
    `src/` escreve `agent_reactivate_at`. `human_handover` a deixa `NULL`
    de propósito (o handover é permanente, quem religa é uma pessoa) e
    nenhum outro caminho a preenche. Não é bug: se este `UPDATE` afetar
    zero linhas em produção, é o esperado.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(_SQL_REATIVAR)
        return cur.rowcount


async def _enviar_reivindicados(
    reivindicados: list[LeadReivindicado], cliente: ClienteOutbound
) -> dict[str, int]:
    """Envia a mensagem de cada lead já reivindicado, revalidando antes.

    O contador (`followup_count`) já subiu no claim, antes deste envio —
    divergência consciente do n8n, que só incrementava depois de enviar com
    sucesso. Aqui, uma falha de envio faz o lead pular um nível em vez de
    tentar de novo na próxima rodada: mandar a mesma mensagem duas vezes é
    pior que perder um follow-up.
    """
    pool = await get_pool()
    enviados = 0
    falhas = 0
    abortados = 0

    for lead in reivindicados:
        if not await ainda_vale_enviar(pool, lead.phone, lead.nivel):
            abortados += 1
            continue

        mensagem = montar_mensagem(lead.nivel, lead.name)
        try:
            await cliente.send_message(to=to_e164(lead.phone), body=mensagem)
        except Exception as erro:
            falhas += 1
            logger.warning(
                "followup_envio_falhou",
                phone=lead.phone,
                nivel=lead.nivel,
                erro=str(erro),
            )
            continue

        enviados += 1
        logger.info("followup_enviado", phone=lead.phone, nivel=lead.nivel)

    return {"enviados": enviados, "falhas": falhas, "abortados": abortados}


async def rodada(
    pool: AsyncConnectionPool,
    cliente: ClienteOutbound,
    *,
    limite: int = settings.followup_batch_size,
    n1_min: int = settings.followup_nivel1_minutos,
    n2_min: int = settings.followup_nivel2_minutos,
    n3_min: int = settings.followup_nivel3_minutos,
    janela_min: int = settings.followup_janela_margem_minutos,
) -> dict[str, int]:
    """Uma rodada completa: reivindica, envia e mede quem ficou bloqueado.

    Devolve `{"enviados", "falhas", "abortados", "bloqueados_por_janela"}`.
    """
    reivindicados = await reivindicar(
        pool,
        limite=limite,
        n1_min=n1_min,
        n2_min=n2_min,
        n3_min=n3_min,
        janela_min=janela_min,
    )
    resultado = await _enviar_reivindicados(reivindicados, cliente)
    bloqueados = await contar_bloqueados_por_janela(
        pool, n1_min=n1_min, n2_min=n2_min, n3_min=n3_min, janela_min=janela_min
    )
    return {**resultado, "bloqueados_por_janela": bloqueados}
