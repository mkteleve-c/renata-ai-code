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
    async def send_message(self, to: str, body: str, **kwargs: Any) -> str | None: ...


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
# erro alto (`FOR UPDATE is not allowed with DISTINCT clause`). Uma
# subquery derivada em `FROM` (sem CTE) preserva o lock — mas nem essa
# forma é usada aqui: o dedup de duplicata não roda em SQL nenhuma, roda
# em Python (`_reivindicar_na_conexao`, abaixo), reaproveitando
# `shared/phone.canonicalizar` em vez de reimplementar a regra em regex
# SQL — foi assim que a primeira versão deste dedup perdeu casos (forma
# local de 11/10 dígitos, zero de tronco) que `canonicalizar` já cobre.
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
# `limite_busca` busca mais candidatos do que `limite` pede — o suficiente
# para capturar pares/trios duplicados inteiros dentro do lote (a
# especificação registra casos reais de três linhas para a mesma pessoa).
# Isso NÃO garante exaustividade contra clusters patológicos maiores que
# a sobra, mas cobre o perfil real medido (~155 duplicatas em ~3.300
# leads, a maioria pares). Ver `_FATOR_SOBRA_BUSCA`.
_SQL_CANDIDATOS = """
select phone, name, followup_count, last_inbound_at
from leads_crm
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
limit %(limite_busca)s
for update skip locked
"""

# Avança `followup_count` de TODAS as linhas físicas decididas em Python
# (vencedor de cada identidade canônica + seus irmãos presentes no MESMO
# lote) — não só do vencedor. Sem isso, o vencedor avança e para de casar
# o predicado de elegibilidade de `_SQL_CANDIDATOS` na próxima rodada
# (mudou de degrau), enquanto o irmão — que nunca avançou — continua
# elegível e é reivindicado sozinho na rodada seguinte: duas mensagens em
# rodadas diferentes em vez de duas na mesma. Reproduzido ao vivo com a
# `Task 4` rodando a cada 5 minutos. Avançar o par inteiro junto faz as
# duas linhas saírem do degrau atual ao mesmo tempo e entrarem no próximo
# também juntas — o par anda em bloco, nunca diverge de novo.
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


# Multiplicador de sobra na busca de candidatos — ver o comentário acima de
# `_SQL_CANDIDATOS`.
_FATOR_SOBRA_BUSCA = 5


async def _telefones_bloqueados(
    conn: AsyncConnection[Any], telefones: set[str]
) -> set[str]:
    """Subconjunto de `telefones` presente na `blocklist`, em qualquer forma.

    A régua é o ÚNICO caminho do sistema que fala sem o lead ter falado —
    é exatamente onde o opt-out mais importa, e é o que faltava aqui: nem
    a reivindicação nem `ainda_vale_enviar` consultavam `blocklist` (só o
    gate consultava, `shared/leads.py:301`). Usa as mesmas duas variações
    (com/sem o 9º dígito) que o gate usa — o `CHECK` da blocklist aceita
    as duas formas, e uma pessoa pode ter pedido opt-out sob qualquer uma
    delas.
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

    Duas consultas nesta MESMA transação, não uma: `_SQL_CANDIDATOS`
    busca e trava (via `FOR UPDATE SKIP LOCKED`) uma sobra de candidatos
    físicos; o agrupamento por identidade canônica e o corte em `limite`
    identidades rodam aqui em Python; `_SQL_AVANCAR` escreve só as linhas
    decididas. Linhas travadas mas não escolhidas (sobra além de `limite`
    identidades, ou membro de um grupo não selecionado) simplesmente
    liberam o lock no commit sem nunca serem escritas — o mesmo padrão de
    "buscar mais do que processar" de qualquer fila baseada em SKIP
    LOCKED.
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
        "limite_busca": limite * _FATOR_SOBRA_BUSCA,
    }
    cur = await conn.execute(_SQL_CANDIDATOS, params)
    candidatos = await cur.fetchall()
    if not candidatos:
        return []

    bloqueados = await _telefones_bloqueados(conn, {c[0] for c in candidatos})
    candidatos = [c for c in candidatos if c[0] not in bloqueados]
    if not candidatos:
        return []

    # Agrupa por (identidade canônica, degrau atual) — não só por
    # identidade. Duas linhas do mesmo par só andam juntas quando estão no
    # MESMO degrau: uma dupla que já divergiu (uma delas mais adiantada
    # por algum motivo anterior a este fix) não pode ser empurrada para o
    # degrau da mais adiantada só por compartilhar telefone — isso pularia
    # um degrau legítimo da mais atrasada. `followup_count` no agrupamento
    # é o que faz esse fix funcionar em qualquer degrau (1, 2 ou 3), não
    # só no primeiro.
    grupos: dict[tuple[str, int], list[tuple[str, str | None, int, Any]]] = {}
    for phone, name, followup_count, last_inbound_at in candidatos:
        chave = canonicalizar(phone) or phone
        grupos.setdefault((chave, followup_count), []).append(
            (phone, name, followup_count, last_inbound_at)
        )

    grupos_ordenados = sorted(
        grupos.values(), key=lambda grupo: min(item[3] for item in grupo)
    )[:limite]

    telefones_a_avancar: list[str] = []
    vencedores: set[str] = set()
    for grupo in grupos_ordenados:
        grupo_ordenado = sorted(grupo, key=lambda item: (item[3], item[0]))
        vencedores.add(grupo_ordenado[0][0])
        telefones_a_avancar.extend(item[0] for item in grupo_ordenado)

    if not telefones_a_avancar:
        return []

    cur = await conn.execute(_SQL_AVANCAR, (telefones_a_avancar,))
    linhas = [linha for linha in await cur.fetchall() if linha[0] in vencedores]
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
    """Reivindica até `limite` leads (identidades canônicas) vencidos.

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

    **Um telefone não é uma pessoa.** Duas ou mais linhas físicas
    compartilhando a mesma identidade canônica (com/sem o 9º dígito, ou
    formas legadas malformadas) avançam `followup_count` juntas; só a
    linha mais urgente do grupo é devolvida para envio. Ver o comentário
    acima de `_SQL_AVANCAR` para o porquê. Telefones na `blocklist` (em
    qualquer variação) nunca são devolvidos.

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
      `reivindicar` acabou de gravar como o instante do claim): **é
      alcançável hoje**, e não só por um caminho futuro — o caminho real é
      um par duplicado com um lado pausado. Se `com_9` está com
      `agent_active=false` e `sem_9` (já reivindicado, `agent_active=true`)
      recebe um inbound novo, `aplicar_gate` funde as duas linhas
      (`_fundir`/`_vencedor_pausa`) e o `agent_active` da FUSÃO vale
      `false` (pausa vence, mesmo vindo da linha irmã) — o gate cai no
      ramo `agente_desligado`, cujo `UPDATE` roda `WHERE phone IN (com_9,
      sem_9)` e bumpa `last_inbound_at` das DUAS linhas físicas, inclusive
      a de `sem_9`, sem tocar em `agent_active`/`followup_active`/
      `followup_count` dela. Nesse caso as quatro checagens anteriores
      passam limpo em `sem_9` — só esta comparação de relógios pega. Log
      real capturado: `followup_abortado motivo=lead_falou_apos_o_claim`.
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
