"""Migração dos leads do Supabase legado para o harness (Fase 4).

Leitura única, sem dependência permanente do Supabase: normaliza os
3.373 telefones da base legada, funde duplicatas, importa em `leads_crm`
e `legacy_chat_history`, valida, e emite `relatorio_migracao.md` para
revisão humana antes do cutover. Idempotente -- rodar duas vezes dá o
mesmo resultado.

Este módulo concentra hoje a normalização (`normalizar_telefone`) e a
fusão de duplicatas em memória (`agrupar_por_canonico`, `fundir_grupo`,
`gerar_relatorio`) -- nenhuma das duas depende de rede nem de banco, e
por isso são testáveis isoladamente. A leitura via REST do Supabase e a
escrita em `leads_crm`/`legacy_chat_history`/`leads_descartados` entram
na task seguinte da Fase 4 (importação e validações bloqueantes).

A credencial do Supabase é segredo: entra por variável de ambiente,
nunca é versionada, e nunca deve ser impressa -- nem em log, nem no
relatório. O `relatorio_migracao.md` gerado também não é versionado --
carrega telefone e nome de gente real.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from whatsapp_langchain.shared.phone import (
    _digitos_do_jid,
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


def _montar_destaques(
    fundidas: dict[str, LinhaFundida], descartes: Sequence[Descarte]
) -> list[str]:
    """Reúne os achados que exigem decisão humana antes do cutover.

    Deliberadamente SEM hardcode de telefone específico: a base é viva (a
    contagem já mudou entre duas consultas na mesma sessão de medição, ver
    o plano da Fase 4), então fixar os dígitos exatos de Moçambique, EUA e
    Portugal quebraria numa reexecução contra dados que mudaram. Em vez
    disso, três regras genéricas que continuam cobrindo os três casos
    medidos em 27/07/2026:

    1. Todo lead migrado cujo canônico NÃO começa com "55" é estrangeiro --
       cobre o caso de Moçambique (`qualificado`, ativo) e o de Portugal
       (follow-up ativo).
    2. Todo descarte com motivo `colide_com_forma_local_br` é um possível
       estrangeiro perdido -- cobre o caso dos EUA.
    3. Todo grupo cuja fusão mudou `phase` ou `agent_active` em relação à
       linha mais recente do grupo -- fusão que muda estado de lead ativo é
       o que mais pode dar errado em silêncio.
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

    destaques = _montar_destaques(fundidas, descartes)
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
    esperado: dict[int, int], persistido: dict[int, int]
) -> None:
    """A fusão só promove, nunca rebaixa -- e a gravação não pode perder isso.

    `esperado` vem do resultado em memória de `fundir_grupo`: por
    construção (`_fase_vencedora`), o rank final de um grupo é o MAIOR rank
    entre seus membros -- nunca cai. `persistido` vem de uma leitura real
    do banco DEPOIS do `INSERT`/`UPDATE`, para o mesmo conjunto de
    canônicos. Comparar contra o banco, e não só contra a própria memória,
    é o que pega um bug na escrita em si (coluna trocada, upsert que
    silenciosamente não atualiza `phase`) -- comparar só em memória nunca
    falharia, porque seria comparar o resultado contra ele mesmo.

    `>=`, não `==`, de propósito: a base de origem está viva (ver "O que
    foi medido" no plano da Fase 4) -- um lead migrado pode ter avançado de
    fase por uma conversa real acontecendo durante a janela da migração, e
    isso não é regressão. O que não pode acontecer é o banco mostrar MENOS
    leads em fase avançada do que a fusão calculou.
    """
    for rank in _RANKS_AVANCADOS:
        minimo = esperado.get(rank, 0)
        real = persistido.get(rank, 0)
        if real < minimo:
            raise MigracaoAbortada(
                f"contagem de leads com fase rank>={rank} caiu na gravação: "
                f"esperado >= {minimo}, achado {real} -- abortando"
            )


async def _validar_nenhum_last_inbound_at(conn: Any, canonicos: Iterable[str]) -> None:
    """`last_inbound_at` nasce `NULL` -- o contrato que a Fase 3 deixou escrito.

    Copiar `last_interaction_at` pareceria óbvio e é errado: para quem já
    recebeu follow-up aquele valor é o NOSSO envio, não o inbound do lead
    -- superestimar a janela de 24h da Cloud API manda mensagem que a Meta
    rejeita, em escala (ver `013_last_inbound_at.sql`). O importador nunca
    inclui esta coluna em `_SQL_UPSERT_LEAD`; esta validação confirma
    contra o banco real, dentro da mesma transação da escrita -- não só
    contra a leitura do código-fonte.
    """
    lista = list(canonicos)
    if not lista:
        return
    cur = await conn.execute(
        "select phone from leads_crm"
        " where phone = any(%s) and last_inbound_at is not null",
        (lista,),
    )
    linhas = await cur.fetchall()
    if linhas:
        vazados = sorted(linha[0] for linha in linhas)
        raise MigracaoAbortada(
            f"{len(vazados)} lead(s) importado(s) já têm last_inbound_at "
            f"preenchido: {vazados[:10]} -- contrato da Fase 3 violado, "
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


_SQL_UPSERT_LEAD = """
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
    pipedriveid = excluded.pipedriveid,
    name = excluded.name,
    username = excluded.username,
    email = excluded.email,
    source = excluded.source,
    phase = excluded.phase,
    followup_count = excluded.followup_count,
    followup_active = excluded.followup_active,
    agent_active = excluded.agent_active,
    agent_reactivate_at = excluded.agent_reactivate_at,
    created_at = excluded.created_at,
    last_interaction_at = excluded.last_interaction_at,
    metadata = excluded.metadata
"""

# NUNCA lista `last_inbound_at` -- nem no INSERT nem no `do update set`. Um
# upsert que a incluísse (mesmo com `excluded.last_inbound_at`, que seria
# sempre NULL vindo do INSERT) reabriria o mesmo furo que 013 fechou: a
# coluna tem que nascer e permanecer NULL até o gate de ingestão real
# escrever nela. `google_event_id`, `faturamento_mensal` e
# `qualificacao_notas` também ficam de fora -- a origem não tem essas três
# colunas (ver "Erro 2" no plano), e omiti-las do `do update set` protege
# qualquer valor real que `update_crm`/`calendar_agendar` já tenham escrito
# numa reexecução contra um lead que passou a existir em produção.

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
    3. Todo `INSERT`/`UPDATE` acontece dentro do MESMO bloco de conexão.
       `async with pool.connection() as conn:` já aplica o comportamento
       normal de conexão do psycopg -- commit no fim sem exceção, rollback
       se qualquer coisa levantar dentro do bloco (documentado em
       `AsyncConnectionPool.connection`) -- então não há necessidade de uma
       transação aninhada explícita.
    4. As validações que DEPENDEM do banco -- `last_inbound_at`, fases não
       retrocederam -- rodam por último, ainda dentro do bloco. Se
       qualquer uma falhar, a exceção propaga e desfaz TUDO que este `run`
       gravou.

    Idempotente: `leads_crm` usa `ON CONFLICT (phone) DO UPDATE` com os
    MESMOS valores computados -- rodar duas vezes com a mesma fusão produz
    a mesma linha, nunca duplica. `leads_descartados` não tem chave única
    (é depósito de qualquer coisa, ver a migração 015), então a
    idempotência aqui é um `DELETE` restrito ao marcador
    `_FONTE_MIGRACAO_DESCARTE` seguido de reinserção -- nunca toca em
    descartes do gate de ingestão em produção, que não carregam essa
    chave.
    """
    validar_soma_fecha(total_origem, grupos, descartes)
    validar_canonicos_no_check(fundidas)
    if session_ids_historico:
        validar_session_ids_historico(session_ids_historico, set(fundidas))

    marcadas = marcar_reunioes_legadas(fundidas)
    reunioes_legadas = sum(
        1 for f in marcadas.values() if f.metadata.get("reuniao_legada") is True
    )
    esperado = _contagem_cumulativa_por_rank(f.phase for f in marcadas.values())
    momento = agora or datetime.now(UTC)

    async with pool.connection() as conn:
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

        await _validar_nenhum_last_inbound_at(conn, marcadas.keys())

        cur = await conn.execute(
            "select phase from leads_crm where phone = any(%s)",
            (list(marcadas.keys()),),
        )
        linhas_persistidas = await cur.fetchall()
        persistido = _contagem_cumulativa_por_rank(
            linha[0] for linha in linhas_persistidas
        )
        validar_fases_nao_retrocederam(esperado, persistido)

    return ResultadoImportacao(
        total_origem=total_origem,
        leads_gravados=len(marcadas),
        descartes_gravados=len(descartes),
        reunioes_legadas_marcadas=reunioes_legadas,
    )
