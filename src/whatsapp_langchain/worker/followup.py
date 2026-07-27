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

**`leads_crm.phone` é sempre a forma canônica** — garantido pelo banco
(migração `014_uma_linha_por_pessoa.sql`, CHECK `leads_crm_phone_canonico_check`)
desde que a base foi consolidada em 27/07/2026. Antes disso, uma mesma
pessoa podia ter até três linhas físicas (com/sem o 9º dígito, e a forma de
zero de tronco), e `variacoes()` só alcançava duas delas por enumeração — o
claim precisava travar o "irmão" fora do predicado de elegibilidade numa
segunda consulta, senão a mesma pessoa podia receber a mesma mensagem duas
vezes. Com a invariante garantida pelo CHECK, essa duplicata deixou de ser
representável e o claim voltou a ser a consulta única abaixo.
"""

from __future__ import annotations

import re
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
from whatsapp_langchain.shared.phone import canonicalizar, to_e164, variacoes

logger = structlog.get_logger()

_MINUTOS_POR_DIA = 24 * 60


class ClienteOutbound(Protocol):
    """`**kwargs` de propósito fora daqui: `_enviar_reivindicados` só chama
    `send_message(to=..., body=...)`, nunca kwarg extra nenhum. Um
    catch-all `**kwargs: Any` no protocolo obrigaria cada implementação
    concreta a aceitar kwargs arbitrários também — e nenhum cliente
    outbound real (`TwilioClient`, `MetaClient`, `UazapiClient`,
    `EvolutionClient`) faz isso; cada um só tem parâmetros opcionais
    nomeados além de `to`/`body`. Com a assinatura estreita, os quatro
    satisfazem este protocolo estruturalmente."""

    async def send_message(self, to: str, body: str) -> str | None: ...


@dataclass(frozen=True)
class LeadReivindicado:
    phone: str
    name: str | None
    nivel: int  # 1, 2 ou 3 — já é o novo followup_count


# `for update skip locked` fica numa consulta simples (sem CTE, sem
# `DISTINCT`) direto em `leads_crm` — medido neste Postgres (16.14): `FOR
# UPDATE` sobre uma REFERÊNCIA de CTE é descartado em silêncio (a CTE não
# é uma relação travável; confere mesmo com `AS MATERIALIZED`, então não é
# efeito de inlining), e `DISTINCT ON` no mesmo nível do `FOR UPDATE` dá
# erro alto (`FOR UPDATE is not allowed with DISTINCT clause`).
#
# Trava (via `FOR UPDATE SKIP LOCKED`) até `limite` leads que JÁ satisfazem
# o predicado de elegibilidade completo, e devolve exatamente os que vão
# avançar — não há mais uma segunda consulta para "alcançar o irmão fora
# do lote": com `leads_crm.phone` garantido canônico pelo banco (CHECK
# `leads_crm_phone_canonico_check`, migração `014_uma_linha_por_pessoa.sql`),
# cada identidade é UMA linha física só, então não existe irmão para
# alcançar. Antes da 014, uma mesma pessoa podia ter até três linhas
# físicas (com/sem o 9º dígito, e a forma de zero de tronco) e esta
# consulta sozinha não alcançava a que ficasse fora do predicado (pausada,
# `last_inbound_at` mais novo, cortada pelo `LIMIT`) — dedup em Python e
# uma segunda consulta travada existiam só para cobrir essa lacuna; a
# migração 014 (Task 7) fechou a lacuna na origem, e este arquivo voltou a
# ser a consulta única que era antes da Task 3 precisar se defender dela.
#
# `FOR UPDATE SKIP LOCKED`, não `FOR UPDATE` puro: duas transações
# concorrentes pegam fatias DISJUNTAS do trabalho — cada uma pula o que a
# outra já travou e segue para o próximo candidato livre, sem nunca
# esperar. Nenhuma das duas pode ficar presa segurando o que a outra quer,
# a pré-condição de todo deadlock.
#
# O predicado de janela (`last_inbound_at > now() - ... janela`) fica
# DENTRO desta cláusula — um lead fora da janela não é candidato, então
# não queima degrau; reivindicar e descartar depois gastaria o degrau à
# toa.
#
# `qualificado` está no filtro de fases e NÃO PODE sair dele: a Fase 2
# (`reverter_fase_apos_cancelamento`, em catalog/elevec_sdr/tools/crm.py)
# religa `followup_active` quando uma reunião é cancelada e devolve o lead
# para `qualificado`. Se `qualificado` entrar na régua, esse lead volta a
# ser perseguido — WhatsApp indevido para gente de verdade.
#
# O `not exists` contra `blocklist` fica DENTRO do predicado, ANTES do
# `LIMIT` — não como filtro em Python depois de já ter travado `limite`
# linhas. Um filtro pós-corte tem starvation por construção: se os
# `limite` candidatos mais urgentes forem todos bloqueados, a rodada
# reivindica zero, mesmo havendo lead elegível de verdade logo atrás na
# fila — e como bloqueado nunca avança `followup_count`, ele ocupa o
# mesmo slot de novo na rodada seguinte, indefinidamente (reproduzido: 10
# bloqueados mais antigos + 1 lead real, com FOLLOWUP_BATCH_SIZE pequeno,
# zero enviados por até 23h). Testando as duas formas (com/sem o 9º
# dígito) porque o CHECK da blocklist aceita as duas e o opt-out pode ter
# sido registrado sob qualquer uma delas — mesma regra de
# `_telefones_bloqueados`, abaixo, que `ainda_vale_enviar` ainda usa.
#
# `and followup_count <= 2` é redundante com as três condições do OR logo
# abaixo (nenhuma delas casa `followup_count >= 3`) — de propósito: um
# mutante que trocasse `= 2` por `>= 2` no braço do degrau 3 reabriria a
# régua para um lead que já recebeu as três mensagens (`followup_count =
# 3`), que `montar_mensagem` não sabe tratar (`nivel=4` levanta `ValueError`,
# capturado como falha — nenhum WhatsApp indevido sai, mas o contador sobe
# e o log enche a cada rodada, por até 23h, sem ninguém perceber). Esta
# cláusula existe para que "três degraus é o fim" não dependa de nenhuma
# das três condições estar certa — é uma barreira estrutural, não só
# convenção do desenho do OR.
_SQL_ELEGIVEIS_TRAVADOS = """
select phone, name, followup_count
from leads_crm
where followup_active
  and agent_active
  and phase not in ('agendou_sessao','desqualificado','perdido','qualificado')
  and last_inbound_at is not null
  and followup_count <= 2
  and (
        (followup_count = 0
         and last_inbound_at < now() - make_interval(mins => %(n1)s))
     or (followup_count = 1
         and last_inbound_at < now() - make_interval(mins => %(n2)s))
     or (followup_count = 2
         and last_inbound_at < now() - make_interval(mins => %(n3)s))
  )
  and last_inbound_at > now() - make_interval(mins => %(janela)s)
  and not exists (
        select 1 from blocklist b
        where b.phone = leads_crm.phone
           or b.phone = (
                case when leads_crm.phone ~ '^55[0-9]{10}$'
                     then '55' || substring(leads_crm.phone from 3 for 2)
                          || '9' || substring(leads_crm.phone from 5)
                     else leads_crm.phone
                end
              )
      )
order by last_inbound_at
limit %(limite)s
for update skip locked
"""

# Único `UPDATE` do claim: avança o degrau e grava `last_interaction_at` —
# o "instante do claim" que `ainda_vale_enviar` usa para saber se o lead
# falou de novo entre a reivindicação e o envio.
_SQL_AVANCAR = """
update leads_crm
set followup_count = followup_count + 1,
    last_interaction_at = now()
where phone = any(%s)
returning phone, name, followup_count
"""

_SQL_BLOQUEADOS_POR_JANELA = """
select count(*) from leads_crm
where followup_active
  and agent_active
  and phase not in ('agendou_sessao','desqualificado','perdido','qualificado')
  and last_inbound_at is not null
  and followup_count <= 2
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


async def _telefones_bloqueados(
    conn: AsyncConnection[Any], telefones: set[str]
) -> set[str]:
    """Subconjunto de `telefones` presente na `blocklist`, em qualquer forma.

    Usada só por `ainda_vale_enviar` — a reivindicação filtra `blocklist`
    direto em `_SQL_ELEGIVEIS_TRAVADOS` (dentro do predicado, antes do
    `LIMIT`; ver o comentário lá). Aqui a checagem é sobre um telefone só,
    revalidado entre o claim e o envio: o lead pode ter pedido opt-out
    NESSE intervalo, e é isso que este caminho cobre. Usa as mesmas duas
    variações (com/sem o 9º dígito) que o gate usa — o `CHECK` da
    blocklist aceita as duas formas, e uma pessoa pode ter pedido opt-out
    sob qualquer uma delas.
    """
    if not telefones:
        return set()

    por_variacao: dict[str, set[str]] = {}
    for telefone in telefones:
        canonico = canonicalizar(telefone) or telefone
        for variacao in variacoes(canonico):
            por_variacao.setdefault(variacao, set()).add(telefone)

    cur = await conn.execute(
        "select phone from blocklist where phone = any(%s)",
        (list(por_variacao.keys()),),
    )
    bloqueados: set[str] = set()
    for (variacao,) in await cur.fetchall():
        bloqueados |= por_variacao.get(variacao, set())
    return bloqueados


# `int | None = None` em vez de `int = settings.followup_x` de propósito:
# um default lido de `settings` no cabeçalho da função é resolvido UMA VEZ,
# na importação do módulo — `monkeypatch.setattr(settings, "followup_x",
# ...)` num teste não alcança essa cópia congelada. Com `None` como
# sentinela e a leitura de `settings` movida para dentro do corpo, o
# default acompanha `settings` em tempo de chamada, e quem quiser um
# degrau isolado ainda pode passar o valor explícito por kwarg — as duas
# necessidades (override por chamada e override por env/monkeypatch)
# ficam atendidas ao mesmo tempo.
async def _reivindicar_na_conexao(
    conn: AsyncConnection[Any],
    *,
    limite: int | None = None,
    n1_min: int | None = None,
    n2_min: int | None = None,
    n3_min: int | None = None,
    janela_min: int | None = None,
) -> list[LeadReivindicado]:
    """Executa a reivindicação na conexão/transação dada. Não commita.

    Exposta separada de `reivindicar` só para o teste de concorrência real
    (`test_duas_transacoes_realmente_sobrepostas_nao_pegam_o_mesmo_lead`),
    que precisa manter a transação aberta manualmente com um
    `asyncio.Event` — `asyncio.gather` de duas corrotinas no mesmo pool não
    garante sobreposição, então sem essa barreira o teste passaria mesmo
    sem `for update skip locked`.

    Dois passos, não mais três: `_SQL_ELEGIVEIS_TRAVADOS` trava (via
    `FOR UPDATE SKIP LOCKED`) até `limite` leads que já satisfazem o
    predicado de elegibilidade inteiro — incluindo blocklist, embutida no
    próprio predicado — e `_SQL_AVANCAR` avança o degrau e devolve todos
    eles para envio. Não há passo de "alcançar o irmão": com
    `leads_crm.phone` garantido canônico pelo CHECK da migração 014, cada
    linha travada já É a identidade inteira.
    """
    if limite is None:
        limite = settings.followup_batch_size
    if n1_min is None:
        n1_min = settings.followup_nivel1_minutos
    if n2_min is None:
        n2_min = settings.followup_nivel2_minutos
    if n3_min is None:
        n3_min = settings.followup_nivel3_minutos
    if janela_min is None:
        janela_min = settings.followup_janela_margem_minutos

    params = {
        "n1": n1_min,
        "n2": n2_min,
        "n3": n3_min,
        "janela": _janela_minutos(janela_min),
        "limite": limite,
    }
    cur = await conn.execute(_SQL_ELEGIVEIS_TRAVADOS, params)
    candidatos = await cur.fetchall()
    if not candidatos:
        return []

    telefones = [linha[0] for linha in candidatos]
    cur = await conn.execute(_SQL_AVANCAR, (telefones,))
    linhas = await cur.fetchall()

    return [LeadReivindicado(phone=p, name=n, nivel=c) for p, n, c in linhas]


async def reivindicar(
    pool: AsyncConnectionPool,
    *,
    limite: int | None = None,
    n1_min: int | None = None,
    n2_min: int | None = None,
    n3_min: int | None = None,
    janela_min: int | None = None,
) -> list[LeadReivindicado]:
    """Reivindica até `limite` leads vencidos, um por identidade canônica.

    **Nenhuma transação fica aberta durante HTTP.** As duas consultas desta
    função (busca de candidatos + avanço) rodam e commitam aqui dentro (o
    `async with pool.connection()` do psycopg commita ao sair sem exceção);
    o envio pelo canal de WhatsApp acontece inteiramente depois, em
    `rodada`/`_enviar_reivindicados`, sem segurar linha nenhuma. Segurar
    `FOR UPDATE` enquanto se espera a Evolution responder é exatamente o
    defeito que este desenho evita.

    **Sem advisory lock.** O lock do Postgres é por sessão: com pool, a
    conexão volta para o pool ainda segurando o lock, e ele evapora sem
    aviso se essa conexão for reciclada ou reaproveitada por outra rotina.
    `FOR UPDATE SKIP LOCKED` dentro da própria transação curta é suficiente
    e não tem esse risco.

    **`followup_count` do `RETURNING` já é o novo valor** (1, 2 ou 3) —
    é o nível da mensagem a enviar, o mesmo `next_level` que o n8n calculava
    separado como `followup_count + 1`.

    **Um telefone É uma pessoa, agora.** `leads_crm.phone` é garantido
    canônico pelo CHECK da migração `014_uma_linha_por_pessoa.sql` — não
    há mais linha física duplicada para juntar nem irmão para sincronizar.
    Telefones na `blocklist` (em qualquer variação) nunca são devolvidos —
    filtrados dentro do próprio predicado de `_SQL_ELEGIVEIS_TRAVADOS`.

    Os degraus (`n1_min`, `n2_min`, `n3_min`) e a janela aceitam override
    explícito por chamada e, sem ele, caem no valor corrente de `settings`
    (lido dentro do corpo — ver o comentário acima de
    `_reivindicar_na_conexao`): testar um degrau isolado não exige mexer em
    env global, e testar com `monkeypatch.setattr(settings, ...)` também
    funciona.
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
    n1_min: int | None = None,
    n2_min: int | None = None,
    n3_min: int | None = None,
    janela_min: int | None = None,
) -> int:
    """Conta leads que venceram um degrau mas têm a janela de 24h fechada.

    Sem esta métrica, "a régua morreu" (bug) e "não havia ninguém vencido"
    (dia calmo) são indistinguíveis no log de uma rodada.
    """
    if n1_min is None:
        n1_min = settings.followup_nivel1_minutos
    if n2_min is None:
        n2_min = settings.followup_nivel2_minutos
    if n3_min is None:
        n3_min = settings.followup_nivel3_minutos
    if janela_min is None:
        janela_min = settings.followup_janela_margem_minutos

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
    a voltar.

    Seis checagens, cada uma cobrindo um caminho diferente — medido contra
    o código real dos outros módulos, não hipótese:

    - **telefone entrou na `blocklist`** desde o claim: a régua é o único
      caminho do sistema que fala sem o lead ter falado, então é onde o
      opt-out mais importa. Mesma checagem de `_telefones_bloqueados` usada
      na reivindicação — ver o comentário lá.
    - **lead sumiu** (linha não existe mais sob este `phone`): uma fusão de
      duplicata em `shared/leads.py` pode ter renomeado a chave.
    - **`agent_active` virou `false`**: é o que pega, hoje, tanto
      `human_handover` quanto um humano pausando pelo ChatWoot quanto o
      caminho `agente_desligado` do gate quando a linha revalidada É a
      mesma que ficou com `agent_active=false`.
    - **`followup_active` virou `false`**: caminho de desligamento
      independente do `agent_active` (ex.: religamento futuro do ChatWoot
      que reative o agente sem reativar a régua).
    - **`followup_count` não é mais o nível reivindicado**: é o que pega,
      **na prática**, o lead que respondeu de novo entre o claim e o envio
      pelo caminho normal do gate — `aplicar_gate` zera `followup_count`
      em todo inbound aceito, e é esse zeramento, não o relógio, que este
      código enxerga primeiro.
    - **`last_inbound_at` mais recente que `last_interaction_at`** (que
      `reivindicar` acabou de gravar como o instante do claim): guarda
      defensiva para um caminho FUTURO, hoje inalcançável por qualquer
      código real. Até a Task 7, o caminho real era um par duplicado com um
      lado pausado — `com_9` com `agent_active=false` e `sem_9` (já
      reivindicado) recebendo inbound novo fundia as duas linhas
      (`_fundir`/`_vencedor_pausa`) e bumpava `last_inbound_at` de `sem_9`
      sem tocar `agent_active`/`followup_active`/`followup_count` dela — as
      quatro checagens anteriores passavam limpo, só esta pegava. O CHECK
      `leads_crm_phone_canonico_check` (migração 014) tornou essa duplicata
      irrepresentável, e o caminho fechou. A justificativa que sobra é a
      mesma do bullet anterior: um webhook do ChatWoot (Task 5) que grave
      `last_inbound_at` (inbound real chegando por canal lateral) sem
      passar pelo zeramento de `followup_count` que `aplicar_gate` faz hoje
      abriria esse mesmo ramo de novo.
    """
    async with pool.connection() as conn:
        if phone in await _telefones_bloqueados(conn, {phone}):
            logger.info(
                "followup_abortado", phone=phone, nivel=nivel, motivo="blocklist"
            )
            return False
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

# `pushName` é atributo do WhatsApp escolhido pelo REMETENTE, sem validação
# nenhuma no caminho até aqui — é texto de um estranho que vira parte de
# uma mensagem enviada pelo número comercial da empresa. `.split(" ")[0]`
# sozinho não segura nada sem espaço: "Ganhe1000reais>>http://evil.tld"
# vira o "primeiro nome" inteiro, e o teto de 60 do `sanitizar_nome` deixa
# passar. Um primeiro nome de verdade é só letras (com `-`/`'` entre
# partes, tipo "Ana-Maria" ou "O'Brien") e curto — qualquer token com
# dígito, símbolo ou comprimento implausível não é nome, é o sentinel
# genérico (`None` → "Oi?"/"Tudo bem?").
_PRIMEIRO_NOME_VALIDO = re.compile(r"^[^\W\d_]+(?:['-][^\W\d_]+)*$")
_LIMITE_PRIMEIRO_NOME = 30


def primeiro_nome(name: str | None) -> str | None:
    """Primeiro nome do lead, ou `None` — nunca texto de preenchimento nem lixo.

    `sanitizar_nome` (`contexto.py`) não serve sozinha aqui: ela devolve o
    nome inteiro colapsado, e devolve a string literal `"não informado"`
    quando não há nome. Usada sem este filtro, `montar_mensagem(1, None)`
    mandaria "não informado?" para uma pessoa de verdade — o sentinel do
    contexto do prompt vazando para uma mensagem de WhatsApp real. Ver o
    comentário acima de `_PRIMEIRO_NOME_VALIDO` para a segunda blindagem,
    contra `pushName` malicioso ou só pontuação.
    """
    limpo = sanitizar_nome(name)
    if limpo == NOME_AUSENTE:
        return None
    primeiro = limpo.split(" ")[0]
    if not primeiro or len(primeiro) > _LIMITE_PRIMEIRO_NOME:
        return None
    if not _PRIMEIRO_NOME_VALIDO.match(primeiro):
        return None
    return primeiro


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

    **O corpo inteiro do loop — `ainda_vale_enviar`, `montar_mensagem` e o
    envio — fica dentro do mesmo `try` por lead.** `ainda_vale_enviar` bate
    no banco; se ela levantar por qualquer motivo (conexão caiu, timeout),
    uma exceção não tratada aqui propagaria para fora do loop inteiro — o
    lote pararia no meio, o resumo se perderia, e os leads seguintes
    ficariam com `followup_count` já incrementado pelo claim, sem mensagem
    e **sem log de falha nenhum**. Isolar por lead troca isso por uma linha
    de log e o lote continua.
    """
    pool = await get_pool()
    enviados = 0
    falhas = 0
    abortados = 0

    for lead in reivindicados:
        try:
            if not await ainda_vale_enviar(pool, lead.phone, lead.nivel):
                abortados += 1
                continue

            mensagem = montar_mensagem(lead.nivel, lead.name)
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
    limite: int | None = None,
    n1_min: int | None = None,
    n2_min: int | None = None,
    n3_min: int | None = None,
    janela_min: int | None = None,
) -> dict[str, int]:
    """Uma rodada completa: reativa, reivindica, envia e mede quem ficou bloqueado.

    Devolve `{"enviados", "falhas", "abortados", "bloqueados_por_janela"}`.
    `reativar_agentes` roda primeiro, por paridade com a especificação (lá é
    o mesmo ciclo). É seguro chamar mesmo com a coluna inerte hoje: o
    `UPDATE` afeta zero linhas e não muda nada até algum caminho passar a
    escrever `agent_reactivate_at`.
    """
    await reativar_agentes(pool)

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
