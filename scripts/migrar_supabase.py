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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
