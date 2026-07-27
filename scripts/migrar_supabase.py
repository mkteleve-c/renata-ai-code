"""Migração dos leads do Supabase legado para o harness (Fase 4).

Leitura única, sem dependência permanente do Supabase: normaliza os
3.373 telefones da base legada, funde duplicatas, importa em `leads_crm`
e `legacy_chat_history`, valida, e emite `relatorio_migracao.md` para
revisão humana antes do cutover. Idempotente -- rodar duas vezes dá o
mesmo resultado.

Este módulo concentra a normalização (`normalizar_telefone`), a fusão de
duplicatas em memória (`agrupar_por_canonico`, `fundir_grupo`,
`gerar_relatorio`), a importação validada de `leads_crm`/`leads_descartados`
(`importar_leads`) e a extração/importação do histórico de conversa
(`montar_historico_por_sessao`, `importar_historico`) para
`legacy_chat_history`. A leitura via REST do Supabase que alimenta este
módulo com `LinhaOrigem`/`LinhaHistoricoOrigem` reais, e a orquestração de
ponta a ponta (`main()`), ficam para a execução do cutover (Fase 5) -- as
funções puras (normalização, fusão, filtro de histórico) não dependem de
rede nem de banco, e por isso são testáveis isoladamente; as que escrevem
(`importar_leads`, `importar_historico`) são testadas contra o Postgres
real, nunca monkeypatchadas.

A credencial do Supabase é segredo: entra por variável de ambiente,
nunca é versionada, e nunca deve ser impressa -- nem em log, nem no
relatório. O `relatorio_migracao.md` gerado também não é versionado --
carrega telefone e nome de gente real.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.agents.catalog.elevec_sdr.saida import extrair_baloes
from whatsapp_langchain.shared.phone import (
    _digitos_do_jid,
    _e_endereco_sem_telefone,
    _sem_zero_de_tronco,
    canonicalizar,
)

# Abaixo disso não fecha com nenhuma forma de telefone real medida na base
# legada -- nem brasileira (sempre 12 dígitos canônicos), nem estrangeira
# plausível (as três achadas em produção têm 11 ou 12 dígitos).
# `canonicalizar` aceita a partir de 8 porque esse é o mínimo tecnicamente
# representável; a migração impõe um piso mais estrito porque aqui o custo
# de importar lixo como lead é maior que perder um número raro de 8-9
# dígitos -- e o relatório mostra cada descarte para revisão humana.
_MINIMO_DIGITOS_PLAUSIVEL = 10

# Mesmo intervalo que o CHECK de `leads_crm.phone` proíbe desde a migração
# 014 (`phone !~ '^[0-9]{10,11}$'`): um valor de 10 ou 11 dígitos é
# indistinguível entre "brasileiro sem DDI" e "estrangeiro com DDI" -- ver
# o comentário de `canonicalizar` em shared/phone.py. Não é copiado do
# texto do CHECK (isso é o que `tests/unit/test_migrar_supabase.py::
# test_saida_sempre_satisfaz_o_check_do_banco` verifica, extraindo a regra
# direto do arquivo da migração) -- é a MESMA decisão de negócio expressa
# duas vezes: aqui como motivo de descarte, lá como constraint de banco.
_COLIDE_COM_FORMA_LOCAL_BR = re.compile(r"^[0-9]{10,11}$")

# A base legada tem uma linha cujo `phone` é a string literal "null" -- o
# `null` de algum export virou texto em vez de virar NULL de verdade.
# Tratado à parte de vazio/None para não depender de `canonicalizar`
# devolver o mesmo motivo por acidente (ver docstring de `_esta_ausente`).
_TEXTOS_DE_AUSENCIA = {"", "null"}


@dataclass(frozen=True)
class Normalizado:
    """Resultado da normalização de um `phone` bruto da base legada.

    `canonico is None` sse `motivo` está preenchido -- nunca os dois
    `None`, nunca os dois preenchidos ao mesmo tempo.
    """

    canonico: str | None
    motivo: str | None


def _esta_ausente(bruto: str | None) -> bool:
    """`None`, vazio/só espaços, ou a string "null" (case-insensitive).

    Sem esta checagem dedicada, `"null"` cairia no ramo genérico de
    `canonicalizar`: a extração de dígitos de "null" dá string vazia, que
    `canonicalizar` também rejeita -- mas por um caminho que este módulo
    classificaria como `digitos_insuficientes`, escondendo que a causa
    real é um valor nulo malformado na origem, não um telefone curto.
    """
    if bruto is None:
        return True
    return bruto.strip().lower() in _TEXTOS_DE_AUSENCIA


def normalizar_telefone(bruto: str | None) -> Normalizado:
    """Decide a chave canônica de um telefone da base legada, ou o descarte.

    Reusa `shared/phone.py::canonicalizar` para toda a conversão de forma
    -- BR com/sem 9º dígito, zero de tronco, máscara, DDI. Esta função
    acrescenta só a CLASSIFICAÇÃO de por que um valor não entra, que
    `phone.py` não precisa ter porque nunca escreve em
    `leads_descartados`:

    - `telefone_ausente`: nulo, vazio, ou a string "null".
    - `endereco_sem_telefone`: `@g.us` (grupo) ou `@lid` (LinkedID do
      WhatsApp) -- não é telefone malformado, é um endereçamento que nunca
      teve telefone por trás. Checado ANTES de `canonicalizar` (que também
      recusa os dois, mas devolve só `None` -- sem essa checagem dedicada
      aqui, o motivo cairia no ramo genérico de dígito curto/implausível
      pelo comprimento do que sobra depois de cortar o `@...`, escondendo
      que a causa real não tem nada a ver com contagem de dígitos. Achado
      no fix round 1: um `@lid`/`@g.us` saía rotulado `sequencia_implausivel`,
      que sugere lixo de digitação, não "isto nunca foi um telefone").
    - `colide_com_forma_local_br`: `canonicalizar` aceitou um valor de 10
      ou 11 dígitos -- só acontece no ramo estrangeiro (formas brasileiras
      sempre saem com 12 dígitos, prefixadas "55"). Nesse comprimento não
      há como distinguir um estrangeiro real (ex.: EUA, `+14242123771`) de
      um brasileiro sem DDI -- e o CHECK de `leads_crm` (migração 014)
      proíbe os dois por igual. Relaxar o CHECK desfaz a invariante de
      telefone único que levou quatro rodadas de revisão na Fase 3; a
      perda entra no relatório para decisão humana, não é escondida.
    - `digitos_insuficientes`: menos de `_MINIMO_DIGITOS_PLAUSIVEL`
      dígitos -- curto demais para ser um número real de qualquer país da
      amostra medida, mesmo quando `canonicalizar` aceitaria como
      estrangeiro plausível.
    - `sequencia_implausivel`: `canonicalizar` recusou porque a entrada se
      declarou brasileira (DDI 55 ou zero de tronco) e não fechou com
      nenhuma das quatro formas válidas -- não é BR malformado corrigível,
      é lixo com comprimento parecido o suficiente para enganar.
    """
    if _esta_ausente(bruto):
        return Normalizado(None, "telefone_ausente")

    assert bruto is not None  # _esta_ausente já cobriu o caso None

    if _e_endereco_sem_telefone(bruto):
        return Normalizado(None, "endereco_sem_telefone")

    canonico = canonicalizar(bruto)

    if canonico is None:
        digitos = _sem_zero_de_tronco(_digitos_do_jid(bruto))
        if len(digitos) < _MINIMO_DIGITOS_PLAUSIVEL:
            return Normalizado(None, "digitos_insuficientes")
        return Normalizado(None, "sequencia_implausivel")

    if _COLIDE_COM_FORMA_LOCAL_BR.fullmatch(canonico):
        return Normalizado(None, "colide_com_forma_local_br")

    if len(canonico) < _MINIMO_DIGITOS_PLAUSIVEL:
        return Normalizado(None, "digitos_insuficientes")

    return Normalizado(canonico, None)


# =============================================================================
# Fusão de duplicatas e relatório (Task 3)
# =============================================================================
#
# ESTA É A TERCEIRA CÓPIA DA MESMA REGRA DE MERGE. As outras duas são
# `shared/leads.py::_fundir`/`_vencedor_pausa`/`_FASE_RANK` (fusão em tempo
# real, uma linha por vez, dentro da transação do gate de ingestão) e
# `db/migrations/014_uma_linha_por_pessoa.sql` (consolidação em massa, em
# PL/pgSQL, do que já estava no banco). AS TRÊS PRECISAM ANDAR JUNTAS: se a
# regra de desempate mudar aqui (novo campo coalescível, novo rank de fase,
# nova regra de recência), as outras duas ficam desatualizadas em silêncio
# até alguém notar em produção -- e "notar em produção" nesta regra
# especificamente significa mandar mensagem pra quem pediu silêncio, ou
# reabrir um handover que já devia estar fechado.
#
# Não foi extraída pra uma função compartilhada de propósito: os três
# ambientes são grandes demais pra uma função só (uma roda por linha sob
# `pg_advisory_xact_lock`, outra roda como operação de conjunto em SQL puro
# sobre uma base inteira, esta roda uma vez em memória, sem banco, sobre um
# export do Supabase). É o *acordo* entre as três, não código compartilhado,
# que precisa ser mantido -- e é por isso que cada cópia carrega esta mesma
# advertência no cabeçalho.


# Reimplementação independente de `shared/leads.py::_FASE_RANK`/`_rank_fase`
# -- mesma tabela, mesma regra (None fica abaixo de qualquer fase, real ou
# desconhecida; fase desconhecida usa rank 0, não -1, pra nunca perder de
# `None`).
_FASE_RANK: dict[str, int] = {
    "formulario_preenchido": 1,
    "iniciou_conversa": 2,
    "qualificado": 3,
    "desqualificado": 4,
    "perdido": 4,
    "agendou_sessao": 5,
}

# Campos coalescíveis por recência: valor da linha MAIS RECENTE do grupo
# quando não-nulo, caindo pra linha mais antiga quando nulo -- mesma regra
# de `shared/leads.py::_fundir` (`recente[campo] if recente[campo] is not
# None else antiga[campo]`) e da etapa 2 de 014 (COALESCE processado em
# ordem ascendente de `last_interaction_at`, o processado por último
# vence). `google_event_id`, `faturamento_mensal` e `qualificacao_notas`
# NÃO entram aqui -- a tabela de origem do Supabase não tem essas três
# colunas (ver "Erro 2" no plano da Fase 4).
_CAMPOS_COALESCIVEIS: tuple[str, ...] = (
    "pipedriveid",
    "email",
    "name",
    "username",
    "source",
)

# Campos do resultado da fusão que o relatório expõe com procedência
# (`origem_por_campo`), na ordem em que aparecem na tabela do relatório.
_CAMPOS_COM_PROCEDENCIA: tuple[str, ...] = (
    "phase",
    "created_at",
    "last_interaction_at",
    *_CAMPOS_COALESCIVEIS,
    "followup_count",
    "agent_active",
    "followup_active",
    "agent_reactivate_at",
)

_EPOCA = datetime.min.replace(tzinfo=UTC)


def _rank_fase(fase: str | None) -> int:
    if fase is None:
        return -1
    return _FASE_RANK.get(fase, 0)


@dataclass(frozen=True)
class LinhaOrigem:
    """Uma linha bruta da tabela de leads do Supabase legado.

    `phone` é o valor BRUTO, tal como veio da origem -- ainda não passou
    por `normalizar_telefone`. O relatório precisa mostrar o telefone
    exatamente como estava na origem, ao lado do canônico de destino, pra
    quem revisar conseguir voltar no Supabase e conferir.
    """

    phone: str | None
    phase: str | None
    created_at: datetime | None
    last_interaction_at: datetime | None
    pipedriveid: str | None
    email: str | None
    name: str | None
    username: str | None
    source: str | None
    followup_count: int | None
    agent_active: bool | None
    followup_active: bool | None
    agent_reactivate_at: datetime | None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class Descarte:
    """Uma linha de origem que `normalizar_telefone` recusou.

    Guarda a linha inteira (não só o telefone) porque é o único rastro do
    que existia na origem depois que o relatório é emitido -- a Task 4 usa
    o mesmo par (`phone_origem`, `motivo`) pra gravar em
    `leads_descartados`.
    """

    phone_origem: str | None
    motivo: str
    linha: LinhaOrigem


@dataclass(frozen=True)
class LinhaFundida:
    """Resultado de fundir um grupo de `LinhaOrigem` que convergem pro mesmo canônico.

    `origem_por_campo` mapeia cada campo de `_CAMPOS_COM_PROCEDENCIA` pro
    telefone BRUTO da linha que contribuiu o valor final -- é o que o
    relatório usa pra mostrar "qual campo veio de qual linha". `mudou_phase`
    e `mudou_agent_active` comparam o resultado da fusão contra a linha
    fisicamente mais recente do grupo (por `last_interaction_at`): um "sim"
    em qualquer um dos dois significa que a fusão promoveu ou pausou um
    lead de um jeito que a última mensagem, sozinha, não sugeriria -- é
    exatamente o tipo de mudança que precisa aparecer na seção de decisão
    humana do relatório, porque é o que mais pode dar errado em silêncio.
    """

    canonico: str
    phase: str | None
    created_at: datetime | None
    last_interaction_at: datetime | None
    pipedriveid: str | None
    email: str | None
    name: str | None
    username: str | None
    source: str | None
    followup_count: int
    agent_active: bool | None
    followup_active: bool | None
    agent_reactivate_at: datetime | None
    metadata: dict[str, Any]
    telefones_origem: tuple[str, ...]
    origem_por_campo: dict[str, str]
    mudou_phase: bool
    mudou_agent_active: bool


@dataclass(frozen=True)
class _VencedorPausa:
    agent_active: bool | None
    followup_active: bool | None
    agent_reactivate_at: datetime | None
    phone: str | None


def _chave_recencia(linha: LinhaOrigem) -> tuple[int, datetime, str]:
    """Ordena por `last_interaction_at` ascendente, NULL primeiro.

    Telefone bruto como desempate final, só pra determinismo total quando
    duas linhas do mesmo grupo compartilham o mesmo instante -- o que os
    testes desta task nunca fazem de propósito (duas tasks da Fase 3
    produziram bugs autoconfirmatórios exatamente com timestamp idêntico
    entre linhas do mesmo grupo), mas dados reais de uma base viva podem.
    """
    marca = linha.last_interaction_at
    telefone = linha.phone or ""
    if marca is None:
        return (0, _EPOCA, telefone)
    return (1, marca, telefone)


def _coalesce_com_procedencia(
    ordenadas: Sequence[LinhaOrigem], campos: Sequence[str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Generaliza `recente if not None else antiga` de `_fundir` pra N linhas.

    `ordenadas` já vem da mais antiga pra mais recente (`_chave_recencia`);
    processar nessa ordem e deixar o valor não-nulo mais recente sobrescrever
    o anterior é o mesmo resultado de "cai pra trás até achar um não-nulo,
    a partir da linha mais recente".
    """
    valores: dict[str, Any] = dict.fromkeys(campos)
    origem: dict[str, str] = {}
    for linha in ordenadas:
        if linha.phone is None:
            continue
        for campo in campos:
            valor = getattr(linha, campo)
            if valor is not None:
                valores[campo] = valor
                origem[campo] = linha.phone
    return valores, origem


def _fase_vencedora(ordenadas: Sequence[LinhaOrigem]) -> tuple[str | None, str | None]:
    """A fase mais avançada do grupo vence -- NUNCA a mais recente.

    Empate de rank (`desqualificado`/`perdido`, ambos 4) desempata pela
    mais recente -- mesma regra de `IF linha_rank >= v_phase_rank` na 014,
    processando `ordenadas` (mais antiga -> mais recente) e deixando o
    último a bater o rank vencer.
    """
    melhor_rank = -2
    melhor_fase: str | None = None
    melhor_phone: str | None = None
    for linha in ordenadas:
        rank = _rank_fase(linha.phase)
        if rank >= melhor_rank:
            melhor_rank = rank
            melhor_fase = linha.phase
            melhor_phone = linha.phone
    return melhor_fase, melhor_phone


def _extremo(
    ordenadas: Sequence[LinhaOrigem], campo: str, *, minimo: bool
) -> tuple[datetime | None, str | None]:
    """`created_at`: o mais antigo (`minimo=True`).

    `last_interaction_at`: o mais recente (`minimo=False`).
    """
    melhor_valor: datetime | None = None
    melhor_phone: str | None = None
    for linha in ordenadas:
        valor = getattr(linha, campo)
        if valor is None:
            continue
        vence = melhor_valor is None or (
            valor < melhor_valor if minimo else valor > melhor_valor
        )
        if vence:
            melhor_valor = valor
            melhor_phone = linha.phone
    return melhor_valor, melhor_phone


def _followup_vencedor(ordenadas: Sequence[LinhaOrigem]) -> tuple[int, str | None]:
    """O MAIOR `followup_count` do grupo vence.

    A escada de follow-up já percorrida não pode ser esquecida por uma
    linha que nasceu com 0 -- mesma regra de `max()` em `_fundir` e
    `GREATEST()` na 014. Em empate, a linha mais recente fica como
    procedência (convenção local: a especificação não define desempate
    para este campo, e as outras duas cópias não precisam dele porque
    tratam só pares, nunca N linhas com mesmo valor).
    """
    melhor = 0
    melhor_phone: str | None = None
    for linha in ordenadas:
        valor = linha.followup_count or 0
        if valor >= melhor:
            melhor = valor
            melhor_phone = linha.phone
    return melhor, melhor_phone


def _vencedor_pausa(linhas: Sequence[LinhaOrigem]) -> _VencedorPausa:
    """Decide de qual linha vêm `agent_active`/`followup_active`/`agent_reactivate_at`.

    Mesma regra de `_vencedor_pausa` em `shared/leads.py`: **`False` vence
    sempre** -- errar pro lado de não mandar mensagem é recuperável, mandar
    pra quem pediu silêncio não é. Entre linhas no mesmo estado (as duas
    pausadas ou as duas ativas), a de `last_interaction_at` mais recente
    desempata. `agent_reactivate_at` acompanha SEMPRE essa mesma linha
    vencedora -- nunca é coalescido à parte: ali `NULL` é estado
    significativo ("nenhuma reativação agendada"), não ausência de dado, e
    preenchê-lo com o valor da linha perdedora ressuscitaria um handover
    expirado.
    """
    pausadas = [linha for linha in linhas if linha.agent_active is False]
    candidatas = pausadas or list(linhas)
    vencedora = max(candidatas, key=_chave_recencia)
    return _VencedorPausa(
        agent_active=vencedora.agent_active,
        followup_active=vencedora.followup_active,
        agent_reactivate_at=vencedora.agent_reactivate_at,
        phone=vencedora.phone,
    )


def _fundir_metadata(ordenadas: Sequence[LinhaOrigem]) -> dict[str, Any]:
    """Mescla os `metadata` JSONB do grupo, chave a chave.

    `ordenadas` vem da mais antiga pra mais recente -- quem vem depois
    vence em conflito de chave, mesma regra de `_fundir_metadata` em
    `shared/leads.py` (`{**base, **topo}`). `linhas_fundidas` guarda os
    telefones BRUTOS de origem: depois da fusão é a única prova de quais
    linhas físicas viraram este lead.
    """
    fundido: dict[str, Any] = {}
    for linha in ordenadas:
        if isinstance(linha.metadata, dict):
            fundido.update(linha.metadata)
    fundido["linhas_fundidas"] = sorted(
        {linha.phone for linha in ordenadas if linha.phone is not None}
    )
    return fundido


def fundir_grupo(canonico: str, linhas: Sequence[LinhaOrigem]) -> LinhaFundida:
    """Funde um grupo de linhas do Supabase legado que convergem pro mesmo `canonico`.

    *** ESTA É A TERCEIRA CÓPIA DA MESMA REGRA DE MERGE -- ver o cabeçalho
    da seção "Fusão de duplicatas e relatório" acima. As outras duas são
    `shared/leads.py::_fundir` (fusão em tempo real no gate de ingestão,
    por par) e a etapa 2 de `db/migrations/014_uma_linha_por_pessoa.sql`
    (consolidação em massa, em SQL, do que já estava no banco). AS TRÊS
    PRECISAM ANDAR JUNTAS. ***

    Duas regras que valem repetir aqui porque são fáceis de "simetrizar"
    por engano com o resto dos campos:

    - `agent_active`/`followup_active`: **`False` vence.** Se qualquer
      linha do grupo está pausada, o lead fica pausado.
    - `agent_reactivate_at`: **fica FORA do coalesce.** Acompanha SEMPRE a
      mesma linha que ganhou `agent_active` (ver `_vencedor_pausa`) --
      nunca é escolhido campo a campo à parte.

    Divergência resolvida conscientemente em relação à prosa do plano
    (ver relatório da task): os campos coalescíveis (`pipedriveid`, `email`,
    `name`, `username`, `source`) vencem por RECÊNCIA, não pela linha de
    fase mais avançada -- é o que `shared/leads.py::_fundir` e a etapa 2 da
    014 realmente implementam (as duas concordam entre si), mesmo a prosa
    da especificação e do plano desta task dizendo "priorizando a fase mais
    avançada" para esses campos. `phase`, por sua vez, continua sendo
    sempre a mais avançada -- essa regra não muda.
    """
    if not linhas:
        raise ValueError(f"grupo vazio para canônico {canonico}")

    ordenadas = sorted(linhas, key=_chave_recencia)

    valores, origem = _coalesce_com_procedencia(ordenadas, _CAMPOS_COALESCIVEIS)

    fase, origem_fase = _fase_vencedora(ordenadas)
    if origem_fase is not None:
        origem["phase"] = origem_fase

    criada_em, origem_criada = _extremo(ordenadas, "created_at", minimo=True)
    if origem_criada is not None:
        origem["created_at"] = origem_criada
    recente_em, origem_recente = _extremo(
        ordenadas, "last_interaction_at", minimo=False
    )
    if origem_recente is not None:
        origem["last_interaction_at"] = origem_recente

    followup, origem_followup = _followup_vencedor(ordenadas)
    if origem_followup is not None:
        origem["followup_count"] = origem_followup

    vencedor_pausa = _vencedor_pausa(linhas)
    if vencedor_pausa.phone is not None:
        origem["agent_active"] = vencedor_pausa.phone
        origem["followup_active"] = vencedor_pausa.phone
        origem["agent_reactivate_at"] = vencedor_pausa.phone

    metadata = _fundir_metadata(ordenadas)

    telefones_origem = tuple(
        sorted({linha.phone for linha in linhas if linha.phone is not None})
    )

    linha_mais_recente = ordenadas[-1]
    mudou_phase = fase != linha_mais_recente.phase
    mudou_agent_active = vencedor_pausa.agent_active != linha_mais_recente.agent_active

    return LinhaFundida(
        canonico=canonico,
        phase=fase,
        created_at=criada_em,
        last_interaction_at=recente_em,
        pipedriveid=valores.get("pipedriveid"),
        email=valores.get("email"),
        name=valores.get("name"),
        username=valores.get("username"),
        source=valores.get("source"),
        followup_count=followup,
        agent_active=vencedor_pausa.agent_active,
        followup_active=vencedor_pausa.followup_active,
        agent_reactivate_at=vencedor_pausa.agent_reactivate_at,
        metadata=metadata,
        telefones_origem=telefones_origem,
        origem_por_campo=origem,
        mudou_phase=mudou_phase,
        mudou_agent_active=mudou_agent_active,
    )


def agrupar_por_canonico(
    linhas: Sequence[LinhaOrigem],
) -> tuple[dict[str, list[LinhaOrigem]], list[Descarte]]:
    """Agrupa as linhas de origem pelo telefone canônico, via `normalizar_telefone`.

    Cada linha ou entra num grupo (chave = canônico), ou vira `Descarte`
    com motivo nomeado -- nunca as duas coisas, nunca desaparece. É a
    garantia que sustenta "total na origem = migrados + descartados" no
    relatório.
    """
    grupos: dict[str, list[LinhaOrigem]] = defaultdict(list)
    descartes: list[Descarte] = []
    for linha in linhas:
        resultado = normalizar_telefone(linha.phone)
        if resultado.canonico is None:
            assert resultado.motivo is not None
            descartes.append(Descarte(linha.phone, resultado.motivo, linha))
        else:
            grupos[resultado.canonico].append(linha)
    return dict(grupos), descartes


def fundir_todos(grupos: dict[str, list[LinhaOrigem]]) -> dict[str, LinhaFundida]:
    """Aplica `fundir_grupo` em cada grupo -- singleton ou duplicata."""
    return {
        canonico: fundir_grupo(canonico, linhas) for canonico, linhas in grupos.items()
    }


def _grupos_com_pipedriveid_conflitante(
    grupos: dict[str, list[LinhaOrigem]],
) -> list[str]:
    """Canônicos cujo grupo bruto carrega mais de um `pipedriveid` distinto.

    `fundir_grupo` escolhe o `pipedriveid` por RECÊNCIA (ver a divergência
    documentada no cabeçalho de `fundir_grupo`), independentemente de qual
    linha ganhou `phase`. Isso pode desacoplar os dois: um grupo com a
    linha ANTIGA em `agendou_sessao`/`DEAL-13` e a linha RECENTE em
    `formulario_preenchido`/`DEAL-NOVO` funde para `agendou_sessao` (fase,
    por rank) + `DEAL-NOVO` (pipedriveid, por recência) -- e uma
    `update_crm` subsequente moveria o card ERRADO no Pipedrive. Achado no
    fix round 1 -- não corrigido automaticamente (não há regra óbvia de
    qual DEAL é o certo), só destacado para decisão humana.
    """
    conflitantes = []
    for canonico, linhas in grupos.items():
        distintos = {linha.pipedriveid for linha in linhas if linha.pipedriveid}
        if len(distintos) > 1:
            conflitantes.append(canonico)
    return sorted(conflitantes)


def _montar_destaques(
    grupos: dict[str, list[LinhaOrigem]],
    fundidas: dict[str, LinhaFundida],
    descartes: Sequence[Descarte],
) -> list[str]:
    """Reúne os achados que exigem decisão humana antes do cutover.

    Deliberadamente SEM hardcode de telefone específico: a base é viva (a
    contagem já mudou entre duas consultas na mesma sessão de medição, ver
    o plano da Fase 4), então fixar os dígitos exatos de Moçambique, EUA e
    Portugal quebraria numa reexecução contra dados que mudaram. Em vez
    disso, cinco regras genéricas:

    1. Todo lead migrado cujo canônico NÃO começa com "55" é estrangeiro --
       cobre o caso de Moçambique (`qualificado`, ativo) e o de Portugal
       (follow-up ativo).
    2. Todo descarte com motivo `colide_com_forma_local_br` é um possível
       estrangeiro perdido -- cobre o caso dos EUA.
    3. Todo descarte com motivo `digitos_insuficientes` -- pode ser DDD +
       telefone real com um dígito faltando (ex.: `519985344`), não
       necessariamente lixo. Achado no fix round 1: só `colide_com_forma_
       local_br` aparecia aqui antes, e esse motivo também perde gente
       real.
    4. Todo grupo cuja fusão mudou `phase` ou `agent_active` em relação à
       linha mais recente do grupo -- fusão que muda estado de lead ativo é
       o que mais pode dar errado em silêncio.
    5. Todo grupo com mais de um `pipedriveid` distinto entre as linhas
       físicas -- ver `_grupos_com_pipedriveid_conflitante`.
    """
    linhas: list[str] = []

    for canonico in sorted(c for c in fundidas if not c.startswith("55")):
        f = fundidas[canonico]
        linhas.append(
            f"- **Lead estrangeiro migrado** `{canonico}` -- "
            f"phase=`{f.phase}`, agent_active=`{f.agent_active}` -- "
            "confirmar que é lead legítimo antes do cutover."
        )

    descartes_colisao = sorted(
        (d for d in descartes if d.motivo == "colide_com_forma_local_br"),
        key=lambda d: d.phone_origem or "",
    )
    for descarte in descartes_colisao:
        linhas.append(
            f"- **Descartado por colidir com forma local BR** "
            f"`{descarte.phone_origem}` -- pode ser estrangeiro legítimo "
            "perdido (ver `colide_com_forma_local_br` no plano da Fase 4), "
            "não relaxamos o CHECK de propósito."
        )

    descartes_curtos = sorted(
        (d for d in descartes if d.motivo == "digitos_insuficientes"),
        key=lambda d: d.phone_origem or "",
    )
    for descarte in descartes_curtos:
        linhas.append(
            f"- **Descartado por dígitos insuficientes** "
            f"`{descarte.phone_origem}` -- pode ser telefone real com um "
            "dígito faltando (DDD + assinante incompleto), não "
            "necessariamente lixo."
        )

    for f in sorted(
        (f for f in fundidas.values() if f.mudou_phase or f.mudou_agent_active),
        key=lambda f: f.canonico,
    ):
        campos = [
            c
            for c, mudou in (
                ("phase", f.mudou_phase),
                ("agent_active", f.mudou_agent_active),
            )
            if mudou
        ]
        linhas.append(
            f"- **Fusão mudou estado do lead** `{f.canonico}` -- "
            f"campo(s) afetado(s): {', '.join(campos)} "
            "(comparado com a linha fisicamente mais recente do grupo)."
        )

    for canonico in _grupos_com_pipedriveid_conflitante(grupos):
        f = fundidas.get(canonico)
        deal_escolhido = f.pipedriveid if f is not None else "?"
        linhas.append(
            f"- **`pipedriveid` conflitante no grupo** `{canonico}` -- "
            f"deal escolhido por recência: `{deal_escolhido}` -- confirmar "
            "que é o deal certo antes de qualquer `update_crm` mover card."
        )

    return linhas


def gerar_relatorio(
    total_origem: int,
    grupos: dict[str, list[LinhaOrigem]],
    fundidas: dict[str, LinhaFundida],
    descartes: Sequence[Descarte],
) -> str:
    """Monta o `relatorio_migracao.md` -- entregável de revisão humana, não log.

    Uma pessoa lê isto pra decidir se o cutover acontece. Por isso a
    validação central (origem = migrados + descartados) é reforçada aqui de
    novo com um `raise`, mesmo a Task 4 repetindo-a como validação
    bloqueante da importação real -- um relatório que não fecha é pior que
    nenhum relatório, porque parece confiável sem ser.

    `migrados`, aqui, conta LINHAS de origem que convergiram pra algum
    canônico (antes da fusão reduzir o grupo a uma linha só) -- não o
    número de leads finais em `leads_crm`. É essa contagem, e não a de
    leads finais, que fecha a soma com `total_origem` e `descartados`: a
    fusão não faz nenhuma linha desaparecer, só reduz quantas linhas
    FÍSICAS representam a mesma pessoa.
    """
    migrados = sum(len(linhas) for linhas in grupos.values())
    total_descartes = len(descartes)
    if migrados + total_descartes != total_origem:
        raise ValueError(
            f"a soma não fecha: origem={total_origem}, migrados={migrados}, "
            f"descartados={total_descartes} (soma={migrados + total_descartes}) "
            "-- alguma linha da origem sumiu sem virar grupo nem descarte"
        )

    grupos_multiplos = {c: g for c, g in grupos.items() if len(g) > 1}

    md: list[str] = [
        "# Relatório de migração -- Supabase legado -> leads_crm",
        "",
        "## Resumo",
        "",
        f"- Total na origem: **{total_origem}**",
        f"- Migrados (linhas de origem com telefone canônico): **{migrados}**",
        f"- Descartados: **{total_descartes}**",
        f"- Checagem: {migrados} + {total_descartes} = "
        f"{migrados + total_descartes} == {total_origem} (fecha)",
        f"- Leads finais após fusão: **{len(grupos)}** "
        f"({len(grupos_multiplos)} grupo(s) com mais de uma linha física)",
        "",
        "## Decisão humana necessária",
        "",
    ]

    destaques = _montar_destaques(grupos, fundidas, descartes)
    md.extend(destaques if destaques else ["(nenhum item encontrado nesta execução)"])
    md.append("")

    md.append("## Grupos fundidos")
    md.append("")
    if not grupos_multiplos:
        md.append("(nenhuma duplicata encontrada nesta execução)")
    for canonico in sorted(grupos_multiplos):
        fundida = fundidas[canonico]
        md.append(f"### {canonico}")
        md.append("")
        md.append(f"Telefones de origem: {', '.join(fundida.telefones_origem)}")
        md.append("")
        md.append("| campo | valor final | veio de |")
        md.append("|---|---|---|")
        for campo in _CAMPOS_COM_PROCEDENCIA:
            valor = getattr(fundida, campo)
            veio_de = fundida.origem_por_campo.get(campo, "--")
            md.append(f"| {campo} | {valor} | {veio_de} |")
        md.append("")

    md.append("## Descartes")
    md.append("")
    if not descartes:
        md.append("(nenhum descarte nesta execução)")
    else:
        md.append("| telefone de origem | motivo |")
        md.append("|---|---|")
        for descarte in descartes:
            md.append(f"| {descarte.phone_origem!r} | {descarte.motivo} |")
    md.append("")

    return "\n".join(md)


# =============================================================================
# Importação e validações bloqueantes (Task 4)
# =============================================================================
#
# Toda validação desta seção ABORTA -- levanta `MigracaoAbortada` -- em vez
# de logar um aviso e seguir. É o contrato explícito da task: um relatório
# que mostra o problema depois não substitui uma gravação que se recusa a
# acontecer com o problema ainda presente. `importar_leads` roda dentro de
# uma ÚNICA transação (`async with pool.connection() as conn:` já aplica
# commit/rollback normal do psycopg no fim do bloco -- ver
# `AsyncConnectionPool.connection`): se qualquer validação pós-escrita
# falhar, a exceção propaga e o Postgres desfaz TUDO que este `run` gravou.
# Não existe "migração parcial" como desfecho possível.


class MigracaoAbortada(RuntimeError):
    """Uma validação bloqueante da Task 4 falhou -- a migração para aqui."""


# Marca todo descarte que ESTA função grava em `leads_descartados`, para que
# uma reexecução consiga limpar só o que ela mesma escreveu antes de
# reinserir -- descartes do gate de ingestão em produção (mesma tabela,
# `shared/leads.py::_reter_descarte`) nunca carregam esta chave e por isso
# nunca são tocados pelo DELETE de idempotência abaixo.
_FONTE_MIGRACAO_DESCARTE = "migracao_supabase_fase4"

# Os ranks de `_FASE_RANK` que contam como "fase avançada" para a validação
# de não-retrocesso -- qualificado (3), desqualificado/perdido (4) e
# agendou_sessao (5). formulario_preenchido (1) e iniciou_conversa (2) ficam
# de fora: são o início do funil, não há "retrocesso" a proteger ali.
_RANKS_AVANCADOS: tuple[int, ...] = (3, 4, 5)


def marcar_reunioes_legadas(
    fundidas: dict[str, LinhaFundida],
) -> dict[str, LinhaFundida]:
    """Grava `metadata["reuniao_legada"] = True` em todo lead fundido cuja
    fase final é `agendou_sessao`.

    Incondicional para essa fase -- não "quando `google_event_id` está
    nulo", porque a tabela do Supabase legado NUNCA teve essa coluna (ver
    "Erro 2" no plano da Fase 4). Não existe um subconjunto "com evento" na
    origem para excluir: todo lead que chega aqui em `agendou_sessao` tem
    reunião REAL na agenda do Silvio e nenhum rastro do id dela.

    `crm.py::gravar_fase` passa a exigir esta marca ausente para aceitar a
    relaxação `agendou_sessao -> qualificado`. Sem a marca, o
    `google_event_id is null` desses leads seria lido como "reunião
    cancelada" e devolveria o card do Pipedrive ao estágio 12 com a sessão
    ainda marcada na agenda -- ver o docstring de `gravar_fase`.
    """
    marcadas: dict[str, LinhaFundida] = {}
    for canonico, fundida in fundidas.items():
        if fundida.phase == "agendou_sessao":
            metadata = {**fundida.metadata, "reuniao_legada": True}
            marcadas[canonico] = replace(fundida, metadata=metadata)
        else:
            marcadas[canonico] = fundida
    return marcadas


def validar_soma_fecha(
    total_origem: int,
    grupos: dict[str, list[LinhaOrigem]],
    descartes: Sequence[Descarte],
) -> None:
    """origem = migrados + descartados -- nada pode sumir em silêncio.

    Mesma soma que `gerar_relatorio` já verifica -- repetida aqui de
    propósito: o relatório é para leitura humana e pode nunca ser aberto
    antes do cutover rodar. Esta chamada é o que impede a GRAVAÇÃO de
    acontecer quando a soma não fecha, relatório lido ou não.
    """
    migrados = sum(len(linhas) for linhas in grupos.values())
    total_descartes = len(descartes)
    if migrados + total_descartes != total_origem:
        raise MigracaoAbortada(
            f"a soma não fecha: origem={total_origem}, migrados={migrados}, "
            f"descartados={total_descartes} (soma={migrados + total_descartes}) "
            "-- abortando antes de gravar qualquer coisa"
        )


# As quatro regras do CHECK real de `leads_crm.phone` (007_elevec.sql +
# 014_uma_linha_por_pessoa.sql), duplicadas aqui DE PROPÓSITO -- não
# importadas de um extrator que lê o SQL do disco, porque o script de
# produção não deve depender de abrir um arquivo de migração em runtime
# para decidir se pode escrever. `tests/unit/test_migrar_supabase.py::
# test_saida_sempre_satisfaz_o_check_do_banco` é quem mantém as cópias
# honestas (extrai o CHECK do SQL e compara contra `normalizar_telefone`).
_CHECK_BASICO = re.compile(r"^[0-9]{8,15}$")
_CHECK_9_DIGITO_BR = re.compile(r"^55[0-9]{2}9[0-9]{8}$")
_CHECK_10_11_DIGITOS = re.compile(r"^[0-9]{10,11}$")


def _satisfaz_check_leads_crm(canonico: str) -> bool:
    return (
        bool(_CHECK_BASICO.fullmatch(canonico))
        and not _CHECK_9_DIGITO_BR.fullmatch(canonico)
        and not canonico.startswith("550")
        and not _CHECK_10_11_DIGITOS.fullmatch(canonico)
    )


def validar_canonicos_no_check(fundidas: dict[str, LinhaFundida]) -> None:
    """Toda linha que vai para `leads_crm` passa no CHECK -- verificado
    ANTES de qualquer `INSERT`, para a importação nunca quebrar no meio com
    parte dos leads dentro e parte fora.

    Redundante com a garantia de `normalizar_telefone` (Task 2) por
    desenho: aquela é a primeira linha de defesa e depende de
    `agrupar_por_canonico` ter sido chamada sobre TODO `LinhaOrigem`. Esta
    roda sobre o resultado já fundido -- o que de fato vira `INSERT` -- e é
    o que pega um bug futuro que quebre a primeira sem que um teste
    unitário isolado dela note.
    """
    invalidos = sorted(c for c in fundidas if not _satisfaz_check_leads_crm(c))
    if invalidos:
        raise MigracaoAbortada(
            f"{len(invalidos)} canônico(s) fundido(s) não satisfazem o CHECK "
            f"de leads_crm.phone: {invalidos[:10]} -- abortando antes de "
            "gravar qualquer coisa"
        )


def validar_session_ids_historico(
    session_ids: Iterable[str], canonicos_migrados: set[str]
) -> None:
    """100% dos `session_id` do histórico legado precisam casar com um lead
    migrado.

    A FK de `legacy_chat_history.phone -> leads_crm(phone)` (migração 015)
    já garante isso no banco quando a Task 5 escrever o histórico -- mas o
    erro de uma `ForeignKeyViolation` no meio de um `INSERT` em massa não
    diz QUAL `session_id` sobrou de fora. Esta validação roda antes de
    qualquer escrita e nomeia os órfãos. `session_id` é o telefone bruto do
    n8n e passa pela MESMA normalização dos leads (Task 2) -- dois
    `session_id` distintos que convergem para o mesmo canônico não são
    órfãos, é o telefone gravado em duas formas.
    """
    orfaos = sorted(
        {
            session_id
            for session_id in session_ids
            if normalizar_telefone(session_id).canonico not in canonicos_migrados
        }
    )
    if orfaos:
        raise MigracaoAbortada(
            f"{len(orfaos)} session_id(s) do histórico não casam com nenhum "
            f"lead migrado: {orfaos[:10]} -- abortando antes de gravar "
            "qualquer coisa"
        )


def _contagem_cumulativa_por_rank(fases: Iterable[str | None]) -> dict[int, int]:
    """Para cada rank de `_RANKS_AVANCADOS`, quantas fases têm rank >= ele."""
    contagem = dict.fromkeys(_RANKS_AVANCADOS, 0)
    for fase in fases:
        rank = _rank_fase(fase)
        for alvo in _RANKS_AVANCADOS:
            if rank >= alvo:
                contagem[alvo] += 1
    return contagem


def validar_fases_nao_retrocederam(
    antes: dict[int, int], depois: dict[int, int]
) -> None:
    """A gravação não pode fazer a contagem de fases avançadas CAIR.

    `antes` e `depois` são a mesma leitura real do banco -- `_contagem_
    cumulativa_por_rank` sobre `leads_crm.phase`, para o mesmo conjunto de
    canônicos -- tirada respectivamente ANTES e DEPOIS do `INSERT`/`UPDATE`
    desta chamada, dentro da MESMA transação.

    Fix round 1 -- reinterpretação corrigida: a primeira versão comparava o
    banco (`persistido`) contra o resultado EM MEMÓRIA que a própria
    escrita ia gravar (`esperado`, derivado de `marcadas`). Isso é
    tautológico -- `persistido` sempre bate com `esperado` porque é
    exatamente o que a escrita gravou, mesmo quando o `UPDATE` sobrescreve
    (de forma incorreta) um lead que já estava mais avançado em produção
    do que a linha reimportada do Supabase diz. Provado contra o banco
    real: um lead em `agendou_sessao` (produção) caía para
    `formulario_preenchido` (Supabase desatualizado) numa reexecução, e a
    validação antiga não via nada de errado, porque comparava a gravação
    contra ela mesma.

    Comparar o banco ANTES contra o banco DEPOIS, na mesma transação, é o
    que de fato pega isso -- com o `ON CONFLICT DO UPDATE` de
    `_SQL_UPSERT_LEAD` já escolhendo `phase` pelo rank (nunca a linha
    reimportada sozinha), esta validação nunca deveria disparar em operação
    normal; ela é a segunda camada de defesa, para um bug FUTURO na
    própria cláusula SQL -- e é assim que os testes de mutação a
    exercitam (mutando o `case` do `phase` de volta para sobrescrita cega).

    `>=`, não `==`, de propósito: a base de origem está viva -- um lead
    pode ter avançado de fase por uma conversa real acontecendo durante a
    própria chamada (outra conexão, fora desta transação, antes do
    commit). O que não pode acontecer é a contagem CAIR.
    """
    for rank in _RANKS_AVANCADOS:
        minimo = antes.get(rank, 0)
        real = depois.get(rank, 0)
        if real < minimo:
            raise MigracaoAbortada(
                f"contagem de leads com fase rank>={rank} caiu na gravação: "
                f"antes={minimo}, depois={real} -- abortando"
            )


async def _capturar_fases(conn: Any, canonicos: Iterable[str]) -> dict[int, int]:
    """Lê `leads_crm.phase` do banco real para os canônicos dados, agregado
    por `_contagem_cumulativa_por_rank`. Usado antes e depois da escrita
    por `validar_fases_nao_retrocederam` -- ver seu docstring."""
    lista = list(canonicos)
    if not lista:
        return dict.fromkeys(_RANKS_AVANCADOS, 0)
    cur = await conn.execute(
        "select phase from leads_crm where phone = any(%s)", (lista,)
    )
    linhas = await cur.fetchall()
    return _contagem_cumulativa_por_rank(linha[0] for linha in linhas)


async def _capturar_last_inbound_at(
    conn: Any, canonicos: Iterable[str]
) -> dict[str, Any]:
    """`{canonico: last_inbound_at}` do banco real, para os canônicos dados.

    Canônico sem linha ainda (primeira importação) simplesmente não entra
    no dict -- `_validar_last_inbound_at_intocado` trata ausência como
    `None` via `.get()`, o mesmo valor que um `INSERT` fresco produz para
    uma coluna sem `DEFAULT`.
    """
    lista = list(canonicos)
    if not lista:
        return {}
    cur = await conn.execute(
        "select phone, last_inbound_at from leads_crm where phone = any(%s)",
        (lista,),
    )
    linhas = await cur.fetchall()
    return {linha[0]: linha[1] for linha in linhas}


async def _validar_last_inbound_at_intocado(conn: Any, antes: dict[str, Any]) -> None:
    """`last_inbound_at` nasce `NULL` e o importador nunca escreve nela --
    esta validação confirma que a coluna, para cada canônico importado,
    tem EXATAMENTE o mesmo valor depois da escrita que tinha antes.

    Fix round 1 -- corrige um bug real do desenho anterior. A versão
    original abortava se QUALQUER lead importado tivesse `last_inbound_at`
    preenchido, ponto -- o que soa como o contrato certo ("a coluna nasce
    NULL"), mas quebra a premissa da task ("o cutover pode precisar rodar
    isto duas vezes") contra uma base viva: um lead que já recebeu
    mensagem real de um humano ANTES da reexecução tem a coluna preenchida
    de propósito, e uma reexecução legítima abortava por isso -- tudo ou
    nada, sem meio-termo possível. Pior: a suposta rede de proteção contra
    "religar em silêncio" um lead pausado não cobria o caminho `fromMe`
    (`shared/leads.py`), que pausa sem nunca tocar `last_inbound_at` --
    então o cenário mais perigoso (handover humano via WhatsApp, lead
    ainda não respondeu) passava batido pela validação de qualquer jeito.

    A proteção correta contra "religar em silêncio" é a doutrina de merge
    do `ON CONFLICT DO UPDATE` (`agent_active`/`followup_active` com
    `AND`, ver `_SQL_UPSERT_LEAD`) -- não esta validação. O papel real
    desta função é mais estreito e mais honesto: confirmar que O
    IMPORTADOR EM SI nunca grava nesta coluna, comparando o valor antes e
    depois da MESMA escrita -- o que funciona tanto na primeira importação
    (antes: ausente/`None`, depois: `NULL` de um `INSERT` que não lista a
    coluna -- iguais) quanto numa reexecução contra um lead com inbound
    real (antes: `<timestamp>`, depois: o MESMO `<timestamp>`, porque o
    `UPDATE` nunca lista essa coluna -- iguais).
    """
    if not antes:
        return
    cur = await conn.execute(
        "select phone, last_inbound_at from leads_crm where phone = any(%s)",
        (list(antes.keys()),),
    )
    linhas = await cur.fetchall()
    mudaram = sorted(phone for phone, depois in linhas if antes.get(phone) != depois)
    if mudaram:
        raise MigracaoAbortada(
            f"{len(mudaram)} lead(s) tiveram last_inbound_at ALTERADO pela "
            f"própria importação: {mudaram[:10]} -- contrato da Fase 3 "
            "violado (o importador nunca deveria tocar esta coluna), "
            "abortando"
        )


def _linha_para_payload(linha: LinhaOrigem) -> dict[str, Any]:
    """`LinhaOrigem` -> `dict` serializável em JSON, para `leads_descartados.payload`.

    `datetime` não é serializável pelo `json.dumps` que `Jsonb` usa por
    baixo -- converte para ISO 8601 aqui, uma vez, em vez de todo chamador
    ter que lembrar disso.
    """

    def _iso(valor: datetime | None) -> str | None:
        return valor.isoformat() if valor is not None else None

    return {
        "phone": linha.phone,
        "phase": linha.phase,
        "created_at": _iso(linha.created_at),
        "last_interaction_at": _iso(linha.last_interaction_at),
        "pipedriveid": linha.pipedriveid,
        "email": linha.email,
        "name": linha.name,
        "username": linha.username,
        "source": linha.source,
        "followup_count": linha.followup_count,
        "agent_active": linha.agent_active,
        "followup_active": linha.followup_active,
        "agent_reactivate_at": _iso(linha.agent_reactivate_at),
        "metadata": linha.metadata,
    }


@dataclass(frozen=True)
class ResultadoImportacao:
    """Contagens da importação real -- o que o `main()` da Task 6 reporta."""

    total_origem: int
    leads_gravados: int
    descartes_gravados: int
    reunioes_legadas_marcadas: int


# Rank de `phase` em SQL puro -- a MESMA tabela de `_FASE_RANK`, embutida
# duas vezes na cláusula `on conflict` abaixo (uma para `leads_crm.phase`,
# a linha que JÁ está no banco; outra para `excluded.phase`, o que a
# reimportação está tentando escrever). `{campo}` é substituído por
# `str.format` antes de virar SQL -- nunca por dado do usuário, então não
# há risco de injeção aqui (os únicos dois valores possíveis são literais
# de coluna fixados neste módulo).
_SQL_RANK_FASE = (
    "case"
    " when {campo} is null then -1"
    " when {campo} = 'agendou_sessao' then 5"
    " when {campo} in ('desqualificado', 'perdido') then 4"
    " when {campo} = 'qualificado' then 3"
    " when {campo} = 'iniciou_conversa' then 2"
    " when {campo} = 'formulario_preenchido' then 1"
    " else 0"
    " end"
)
_RANK_FASE_ATUAL = _SQL_RANK_FASE.format(campo="leads_crm.phase")
_RANK_FASE_NOVA = _SQL_RANK_FASE.format(campo="excluded.phase")

# `on conflict do update set`: a QUARTA cópia da doutrina de merge (as
# outras três são `shared/leads.py::_fundir`, a etapa 2 de
# `014_uma_linha_por_pessoa.sql` e `fundir_grupo` acima -- ver o cabeçalho
# da seção "Fusão de duplicatas e relatório"). Existe porque o `INSERT`
# sozinho não basta: numa reexecução, `phone` já existe em `leads_crm` com
# valores que podem ter mudado em PRODUÇÃO desde a importação anterior
# (handover, follow-up, avanço de fase, injeção de histórico) -- e
# `excluded` carrega só o que a reimportação do Supabase recalculou, que
# não sabe nada sobre essas mudanças.
#
# Achado no fix round 1, contra o banco real: a versão anterior deste
# UPSERT fazia `campo = excluded.campo` incondicional para TODO campo --
# uma reexecução revertia `phase` para trás, religava `agent_active`/
# `followup_active` de um lead pausado, zerava `followup_count` e apagava
# `metadata->>'historico_injetado'` que a Task 5 tinha marcado. Nenhuma
# das cinco validações bloqueantes pegava isso, porque nenhuma lia o
# ESTADO ANTERIOR do banco -- só comparavam o que foi escrito contra o que
# se pretendia escrever, e as duas coisas sempre batiam.
#
# A correção aplica a MESMA regra das outras três cópias, com
# `leads_crm.<campo>` (o que já está gravado) no lugar de "a linha mais
# antiga do grupo" e `excluded.<campo>` (o que a reimportação calculou) no
# lugar de "a linha mais recente":
#
# - `agent_active`/`followup_active`: **`false` vence.** `AND` booleano
#   implementa isso direto -- se qualquer lado é `false`, o resultado é
#   `false`.
# - `agent_reactivate_at`: acompanha SEMPRE o lado que está pausado (nunca
#   os dois `true` ao mesmo tempo tendo um `agent_reactivate_at`
#   sobrevivente) -- se os dois estão pausados, o `CASE` cai para o lado
#   com `last_interaction_at` mais recente, mesmo desempate de
#   `_vencedor_pausa`.
# - `phase`: o MAIOR rank vence -- nunca a reimportação sozinha.
# - `followup_count`: o maior dos dois.
# - `created_at`: o mais antigo (`least`). `last_interaction_at`: o mais
#   recente (`greatest`).
# - `metadata`: `||` do Postgres faz `{**leads_crm.metadata, **excluded.
#   metadata}` -- chaves só em `leads_crm.metadata` (como
#   `historico_injetado`, que só a Task 5 grava, nunca a origem) SOBREVIVEM
#   à reimportação; chaves em comum (como `reuniao_legada`, `linhas_
#   fundidas`) são atualizadas pelo valor recém-calculado.
# - `pipedriveid`/`email`/`name`/`username`/`source`: `coalesce(leads_crm.
#   <campo>, excluded.<campo>)` -- o valor JÁ gravado em produção vence
#   quando existe (protege um e-mail real capturado por `update_crm`, ou
#   um `name` real vindo do `pushName` do WhatsApp, de ser revertido por
#   um snapshot do Supabase mais velho); só cai para o valor reimportado
#   quando a coluna em produção está vazia.
#
# `last_inbound_at`, `google_event_id`, `faturamento_mensal` e
# `qualificacao_notas` NUNCA aparecem aqui -- nem no `INSERT`, nem no `do
# update set`. As primeiras três colunas continuam nascendo/permanecendo
# como estavam (NULL ou o que o gate/CRM já tenha escrito); a origem não
# tem as últimas três (ver "Erro 2" no plano).
_SQL_UPSERT_LEAD = f"""
insert into leads_crm (
    phone, pipedriveid, name, username, email, source,
    phase, followup_count, followup_active, agent_active,
    agent_reactivate_at, created_at, last_interaction_at, metadata
) values (
    %s, %s, %s, %s, %s, %s::lead_source,
    %s::lead_phase, %s, %s, %s,
    %s, %s, %s, %s
)
on conflict (phone) do update set
    pipedriveid = coalesce(leads_crm.pipedriveid, excluded.pipedriveid),
    name = coalesce(leads_crm.name, excluded.name),
    username = coalesce(leads_crm.username, excluded.username),
    email = coalesce(leads_crm.email, excluded.email),
    source = coalesce(leads_crm.source, excluded.source),
    phase = case
        when {_RANK_FASE_NOVA} >= {_RANK_FASE_ATUAL} then excluded.phase
        else leads_crm.phase
    end,
    followup_count = greatest(leads_crm.followup_count, excluded.followup_count),
    followup_active = leads_crm.followup_active and excluded.followup_active,
    agent_active = leads_crm.agent_active and excluded.agent_active,
    agent_reactivate_at = case
        when leads_crm.agent_active is false and excluded.agent_active is false
            then case
                when leads_crm.last_interaction_at >= excluded.last_interaction_at
                    then leads_crm.agent_reactivate_at
                else excluded.agent_reactivate_at
            end
        when leads_crm.agent_active is false then leads_crm.agent_reactivate_at
        when excluded.agent_active is false then excluded.agent_reactivate_at
        else null
    end,
    created_at = least(leads_crm.created_at, excluded.created_at),
    last_interaction_at = greatest(
        leads_crm.last_interaction_at, excluded.last_interaction_at
    ),
    metadata = leads_crm.metadata || excluded.metadata
"""

_SQL_INSERT_DESCARTE = """
insert into leads_descartados (phone_original, motivo, payload)
values (%s, %s, %s)
"""


async def importar_leads(
    pool: AsyncConnectionPool,
    total_origem: int,
    grupos: dict[str, list[LinhaOrigem]],
    fundidas: dict[str, LinhaFundida],
    descartes: Sequence[Descarte],
    session_ids_historico: Sequence[str] = (),
    *,
    agora: datetime | None = None,
) -> ResultadoImportacao:
    """Grava a fusão em `leads_crm`/`leads_descartados`, atrás de validações
    bloqueantes que abortam -- nunca avisam.

    Ordem:

    1. Validações PURAS que não tocam o banco -- soma fecha, CHECK,
       `session_id`s do histórico -- rodam ANTES de qualquer escrita e
       abortam sem que uma única linha entre no Postgres.
    2. `marcar_reunioes_legadas` computa o `metadata` final em memória.
    3. Dentro do bloco de conexão, captura o ESTADO ANTERIOR do banco
       (`_capturar_fases`, `_capturar_last_inbound_at`) para os canônicos
       que estão prestes a ser tocados -- é a base de comparação das duas
       validações pós-escrita (fix round 1: comparar contra o próprio
       resultado da escrita, como a versão anterior fazia, nunca pega uma
       reversão real -- ver os docstrings de `validar_fases_nao_
       retrocederam` e `_validar_last_inbound_at_intocado`).
    4. Todo `INSERT`/`UPDATE` acontece dentro do MESMO bloco de conexão.
       `async with pool.connection() as conn:` já aplica o comportamento
       normal de conexão do psycopg -- commit no fim sem exceção, rollback
       se qualquer coisa levantar dentro do bloco (documentado em
       `AsyncConnectionPool.connection`) -- então não há necessidade de uma
       transação aninhada explícita. O `ON CONFLICT DO UPDATE` de
       `_SQL_UPSERT_LEAD` já aplica a doutrina de merge (nunca sobrescreve
       cegamente um lead que avançou em produção) -- ver o comentário
       acima dele.
    5. As validações que DEPENDEM do banco -- fases não caíram,
       `last_inbound_at` intocado -- comparam o estado capturado no passo
       3 contra uma nova leitura pós-escrita. Se qualquer uma falhar, a
       exceção propaga e desfaz TUDO que este `run` gravou.

    Idempotente NO SENTIDO que a task pede -- "rodar o MESMO cálculo de
    fusão duas vezes dá o mesmo resultado, sem duplicar" -- e também
    SEGURO contra uma reexecução depois de tráfego real ter acontecido:
    o `ON CONFLICT DO UPDATE` nunca rebaixa `phase`, nunca religa
    `agent_active`/`followup_active` de um lead pausado, nunca zera
    `followup_count`, e preserva chaves de `metadata` que só existem em
    produção (como `historico_injetado`, gravado pela Task 5). Continua
    havendo uma pendência real, e ela é da Fase 5, não desta task: se DOIS
    processos escrevem no MESMO canônico ao mesmo tempo (esta migração e o
    gate de ingestão real), não há lock coordenando os dois -- só a
    convenção de que a migração roda com os workflows do n8n desligados.
    `leads_descartados` não tem chave única (é depósito de qualquer coisa,
    ver a migração 015), então a idempotência aqui é um `DELETE` restrito
    ao marcador `_FONTE_MIGRACAO_DESCARTE` seguido de reinserção -- nunca
    toca em descartes do gate de ingestão em produção, que não carregam
    essa chave.
    """
    validar_soma_fecha(total_origem, grupos, descartes)
    validar_canonicos_no_check(fundidas)
    if session_ids_historico:
        validar_session_ids_historico(session_ids_historico, set(fundidas))

    marcadas = marcar_reunioes_legadas(fundidas)
    reunioes_legadas = sum(
        1 for f in marcadas.values() if f.metadata.get("reuniao_legada") is True
    )
    momento = agora or datetime.now(UTC)

    async with pool.connection() as conn:
        fases_antes = await _capturar_fases(conn, marcadas.keys())
        inbound_antes = await _capturar_last_inbound_at(conn, marcadas.keys())

        for canonico, fundida in marcadas.items():
            await conn.execute(
                _SQL_UPSERT_LEAD,
                (
                    canonico,
                    fundida.pipedriveid,
                    fundida.name,
                    fundida.username,
                    fundida.email,
                    fundida.source,
                    fundida.phase,
                    fundida.followup_count,
                    fundida.followup_active,
                    fundida.agent_active,
                    fundida.agent_reactivate_at,
                    fundida.created_at or momento,
                    fundida.last_interaction_at or momento,
                    Jsonb(fundida.metadata),
                ),
            )

        await conn.execute(
            "delete from leads_descartados where payload->>'fonte_migracao' = %s",
            (_FONTE_MIGRACAO_DESCARTE,),
        )
        for descarte in descartes:
            payload = _linha_para_payload(descarte.linha)
            payload["fonte_migracao"] = _FONTE_MIGRACAO_DESCARTE
            await conn.execute(
                _SQL_INSERT_DESCARTE,
                (descarte.phone_origem, descarte.motivo, Jsonb(payload)),
            )

        await _validar_last_inbound_at_intocado(conn, inbound_antes)

        fases_depois = await _capturar_fases(conn, marcadas.keys())
        validar_fases_nao_retrocederam(fases_antes, fases_depois)

    return ResultadoImportacao(
        total_origem=total_origem,
        leads_gravados=len(marcadas),
        descartes_gravados=len(descartes),
        reunioes_legadas_marcadas=reunioes_legadas,
    )


# =============================================================================
# Histórico e continuidade (Task 5)
# =============================================================================
#
# `n8n_chat_histories` guarda o formato serializado do LangChain: cada linha
# é `{type, content, additional_kwargs, response_metadata}`. Medido contra a
# origem em 27/07/2026 -- 8.920 linhas em 736 sessões --, a distribuição por
# `type` é:
#
#   ai:    4.460 linhas, das quais 3.316 têm `content` que PARECE JSON
#   human: 3.316 linhas, nenhuma com `content` em formato JSON
#   tool:  1.144 linhas
#
# `4460 - 3316 = 1144`, exatamente a contagem de `tool` -- mas essa soma não
# prova, sozinha, que os 1.144 são "todos chamada de ferramenta"; a
# decomposição real (fix round 2, corrigida em `docs/evidencias/
# formato-historico-n8n.md`) é **1.137** com `content` string NÃO-JSON (ex.:
# `Calling update_crm1 with input: {...}`) mais **7** com `content` igual a
# `[]` -- um array JSON, não uma string. Os dois grupos são PEDIDOS de
# chamada de ferramenta, e os dois caem no mesmo `isinstance(bruto, str)`
# antes mesmo de `_eh_json_valido` rodar -- `extrair_turno_de_conversa`
# descarta os dois pelo mesmo motivo (não é string), então o resultado do
# filtro não muda; só a decomposição que se escreveu por engano no primeiro
# round mudou. Nenhuma das 8.920 linhas tem `tool_calls` em
# `additional_kwargs`. Os `ai` cujo `content` PARECE JSON são as respostas
# FINAIS. `extrair_turno_de_conversa` classifica pela MESMA distinção --
# tenta `json.loads` no `content` de um `ai` (que primeiro precisa SER uma
# string); sucesso é resposta final, qualquer outra coisa é ruído de tool
# call e o turno é descartado.
#
# FIX ROUND 1 -- o envelope da resposta final está ANINHADO sob `output`,
# não `{"messages": [...]}` no topo como a primeira versão deste módulo
# assumia (extrapolando do formato que `extrair_baloes`, elevec_sdr/saida.py,
# desembrulha HOJE em produção para o agente NOVO). Medido contra a base real
# via MCP do Supabase (não fixture, não suposição): as 3.316 linhas `ai` com
# `content` JSON têm TODAS, sem exceção, a forma
# `{"output": {"messages": ["Oi, Diego!", "Recebi sua mensagem..."]}}` --
# 0 têm `messages` no topo, 0 têm `output` sem `messages` array, 0 têm item
# não-string ou lista vazia dentro de `messages`. `_desaninhar_output`
# resolve esse embrulho ANTES de chamar `extrair_baloes` -- que continua
# INTOCADO (é código de produção da Fase 2, e o formato do agente novo é
# `messages` no topo mesmo; `output` é embrulho do n8n, problema só da
# importação). Sem desaninhar, as 3.316 respostas da Renata entrariam no
# histórico como blobs de JSON cru (`extrair_baloes` cai no fallback de
# balão único quando não acha `messages` no topo) -- e o modelo passaria a
# ver as próprias falas passadas nesse formato, o pior tipo de ruído possível
# para um agente cuja saída é justamente um envelope JSON. Evidência
# completa, com as queries: `docs/evidencias/formato-historico-n8n.md`.
#
# `tool` nunca entra: é resultado de execução de ferramenta, nunca texto que
# o lead leu. As tools referenciadas por esse ruído mudaram de assinatura
# nesta migração (ver plano da Fase 4) -- injetar essas chamadas como
# histórico confundiria o modelo com contratos que não existem mais, além de
# nunca ter sido conversa que o lead viveu.
#
# DECISÃO DOCUMENTADA sobre a "janela de 12": o n8n usava uma janela de 12
# mensagens na Postgres Chat Memory (docs/evidencias/prompt-renata-n8n.md),
# contando TODA linha -- inclusive `tool` e os `ai` de chamada de ferramenta.
# Esta migração conta os 12 DEPOIS do filtro de ruído -- ou seja, até 12
# TURNOS DE CONVERSA REAL (`human` + `ai` final), não 12 linhas brutas da
# origem. A diferença importa: 12 linhas brutas podiam conter só 4 ou 5
# turnos reais de conversa quando o resto era ruído de tool em volta; 12
# turnos filtrados é estritamente MAIS contexto de conversa do que o n8n
# jamais teve na janela dele -- nunca menos. Isso é intencional (ver o plano
# da Fase 4, seção "A janela de 12"): o lead "lembra" mais depois da
# migração do que a Renata do n8n lembrava, nunca menos.


def _eh_json_valido(texto: str) -> bool:
    try:
        json.loads(texto)
    except (ValueError, TypeError):
        return False
    return True


def _desaninhar_output(bruto: str) -> str:
    """Desembrulha o envelope aninhado do n8n ANTES de `extrair_baloes` ver
    o texto -- fix round 1, ver o cabeçalho da seção acima.

    O `content` medido de um `ai` final é `{"output": {"messages": [...]}}`,
    não `{"messages": [...]}` no topo. `extrair_baloes` (elevec_sdr/saida.py)
    procura `messages` no TOPO -- é o formato que o agente NOVO produz, sem
    embrulho, e ele continua correto para esse caso; não é alterado aqui. O
    que este módulo faz é reduzir o formato ANTIGO ao formato que
    `extrair_baloes` já sabe ler: se `bruto` parsear como um objeto com uma
    chave `output` que também é um objeto, devolve o JSON SERIALIZADO desse
    objeto interno (`{"messages": [...]}`) -- pronto para `extrair_baloes`
    desembrulhar normalmente.

    Quando `bruto` NÃO tem essa forma -- já vem sem `output` (ex.: uma
    migração futura, ou dado de outra origem), ou `output` não é um objeto
    -- devolve `bruto` sem alterar. `extrair_baloes` então tenta o caminho
    normal dele: acha `messages` no topo se houver, ou cai no fallback de
    balão único (o texto JSON inteiro como uma string) se não achar em lugar
    nenhum. Medido contra a base real (27/07/2026, via MCP do Supabase): das
    3.316 linhas `ai` com `content` JSON, 100% têm `output.messages` como
    array de strings não vazio -- nenhuma cai no fallback hoje. O fallback
    existe mesmo assim porque este módulo processa uma base viva (ver o
    plano da Fase 4) -- uma linha futura com `content` JSON mas sem a lista
    de balões em lugar nenhum não pode travar a importação; vira um balão
    único com o JSON cru (mesmo comportamento — e mesmo aviso de log — que
    `extrair_baloes` já aplica para qualquer JSON sem `messages`), não uma
    exceção.
    """
    try:
        dados = json.loads(bruto)
    except (ValueError, TypeError):
        return bruto
    if isinstance(dados, dict) and isinstance(dados.get("output"), dict):
        return json.dumps(dados["output"])
    return bruto


@dataclass(frozen=True)
class LinhaHistoricoOrigem:
    """Uma linha bruta de `n8n_chat_histories`.

    `id_origem` é o autoincremento (`id serial`) da tabela de origem -- o
    schema padrão da Postgres Chat Memory do LangChain (`id`, `session_id`,
    `message`) não tem coluna de timestamp. `id_origem` é por isso a ÚNICA
    fonte de ordem cronológica disponível, inclusive entre duas formas
    diferentes de `session_id` (bruto) que convergem para o mesmo telefone
    canônico -- ele cresce globalmente na tabela, não só dentro de uma
    sessão, então continua válido como chave de ordenação depois da fusão.

    `session_id` é o valor BRUTO da origem (o telefone tal como o n8n
    gravou) -- passa por `normalizar_telefone` (mesma função da Task 2) só
    dentro de `montar_historico_por_sessao`, nunca aqui.
    """

    id_origem: int
    session_id: str | None
    mensagem: dict[str, Any]


def extrair_turno_de_conversa(mensagem: dict[str, Any]) -> tuple[str, str] | None:
    """Decide se uma linha de `n8n_chat_histories` faz parte da conversa que
    o lead viveu, e devolve `(papel, conteudo)` pronto para
    `legacy_chat_history` -- ou `None` se é ruído de ferramenta.

    - `type == "human"`: sempre entra, com o `content` tal como está (texto
      puro do lead, nunca passa por `extrair_baloes` -- esse desembrulho é
      só para o envelope de balões que o parser do n8n produzia nas
      respostas da Renata).
    - `type == "ai"`: só entra se `content` parsear como JSON -- é a
      distinção medida acima entre resposta final (JSON) e pedido de
      chamada de ferramenta (não-JSON). Quando entra, `content` passa
      primeiro por `_desaninhar_output` (desembrulha o `{"output": {...}}`
      medido na origem -- fix round 1) e SÓ DEPOIS por `extrair_baloes`
      (reusado de `elevec_sdr/saida.py`, nunca reimplementado, nunca
      alterado); os balões saem juntos numa string só, separados por linha
      em branco -- é UM turno de conversa, mesmo tendo sido enviado como
      vários balões de WhatsApp.
    - `type == "tool"`, ou qualquer outro valor: nunca entra.

    `content` vazio ou só espaço, em qualquer ramo, também descarta o
    turno -- um turno vazio não é conversa, é lixo de formatação.
    """
    tipo = mensagem.get("type")

    if tipo == "human":
        bruto = mensagem.get("content")
        conteudo = str(bruto).strip() if isinstance(bruto, str) else ""
        return ("human", conteudo) if conteudo else None

    if tipo == "ai":
        bruto = mensagem.get("content")
        if not isinstance(bruto, str) or not _eh_json_valido(bruto):
            return None
        desaninhado = _desaninhar_output(bruto)
        conteudo = "\n\n".join(extrair_baloes(desaninhado)).strip()
        return ("ai", conteudo) if conteudo else None

    return None


def montar_historico_por_sessao(
    linhas: Sequence[LinhaHistoricoOrigem], janela: int = 12
) -> dict[str, list[tuple[str, str]]]:
    """Agrupa `n8n_chat_histories` por telefone CANÔNICO, filtra ruído de
    ferramenta e mantém só os `janela` turnos mais recentes por telefone --
    contados DEPOIS do filtro (ver a decisão documentada acima do módulo).

    `janela <= 0` devolve NENHUM turno (lista vazia) para todo canônico --
    fix round 2: a versão anterior tratava `janela <= 0` como "sem limite"
    (`else turnos`, devolvendo TUDO), semântica invertida do que o nome do
    parâmetro sugere. Hoje inalcançável (o único chamador usa o default de
    12), mas corrigido porque uma janela de tamanho zero ou negativo só faz
    sentido como "nada cabe", nunca como "cabe infinito".

    `session_id` passa por `normalizar_telefone` (mesma função da Task 2) --
    duas formas brutas que convergem para o mesmo canônico têm seus turnos
    fundidos e reordenados juntos por `id_origem`, não tratadas como
    sessões separadas. Uma sessão cujo `session_id` não normaliza (mesmos
    motivos de descarte de leads: telefone ausente, colide com forma local
    BR, etc.) simplesmente não contribui história nenhuma -- ela não tem
    como ter virado um lead migrado, então não há `phone` em `leads_crm`
    para a FK de `legacy_chat_history` apontar; `validar_session_ids_
    historico` (Task 4) é quem torna esse caso um abort visível, não esta
    função.
    """
    por_canonico: dict[str, list[tuple[int, str, str]]] = defaultdict(list)

    for linha in linhas:
        canonico = normalizar_telefone(linha.session_id).canonico
        if canonico is None:
            continue
        turno = extrair_turno_de_conversa(linha.mensagem)
        if turno is None:
            continue
        papel, conteudo = turno
        por_canonico[canonico].append((linha.id_origem, papel, conteudo))

    resultado: dict[str, list[tuple[str, str]]] = {}
    for canonico, turnos in por_canonico.items():
        turnos.sort(key=lambda t: t[0])
        janela_final = turnos[-janela:] if janela > 0 else []
        resultado[canonico] = [(papel, conteudo) for _, papel, conteudo in janela_final]
    return resultado


_SQL_DELETE_HISTORICO_DO_TELEFONE = "delete from legacy_chat_history where phone = %s"
_SQL_INSERT_HISTORICO = """
insert into legacy_chat_history (phone, ordem, papel, conteudo)
values (%s, %s, %s, %s)
"""


async def importar_historico(
    pool: AsyncConnectionPool,
    historico_por_sessao: dict[str, list[tuple[str, str]]],
) -> int:
    """Grava o histórico filtrado (Task 5) em `legacy_chat_history`.

    Chamar DEPOIS de `importar_leads` -- a FK de `legacy_chat_history.phone`
    (migração 015) exige que o canônico já exista em `leads_crm`; escrever
    antes quebraria com `ForeignKeyViolation` no primeiro telefone.

    Idempotente POR TELEFONE: apaga as linhas existentes daquele canônico e
    reinsere na ordem corrente -- mais simples e mais correto que um
    `ON CONFLICT (phone, ordem) DO UPDATE`, porque o TAMANHO da janela pode
    encolher entre execuções (ex.: uma reexecução contra uma origem que
    ganhou mais ruído de tool no meio da janela anterior). Um UPSERT por
    `ordem` deixaria sobrar linhas de uma execução anterior com `ordem`
    maior que a nova contagem -- `UNIQUE (phone, ordem)` não detecta isso
    sozinho, e sobra vira história fantasma (mensagens de uma execução
    anterior que a nova janela já não inclui, mas que continuam no banco).
    DELETE-then-INSERT por telefone, dentro do mesmo bloco de conexão,
    evita esse resíduo -- e cada telefone é uma unidade atômica pequena o
    bastante para não precisar de uma transação explícita além da que o
    pool já aplica por bloco.
    """
    total = 0
    async with pool.connection() as conn:
        for canonico, turnos in historico_por_sessao.items():
            await conn.execute(_SQL_DELETE_HISTORICO_DO_TELEFONE, (canonico,))
            for ordem, (papel, conteudo) in enumerate(turnos, start=1):
                await conn.execute(
                    _SQL_INSERT_HISTORICO, (canonico, ordem, papel, conteudo)
                )
                total += 1
    return total
