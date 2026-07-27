"""Migração dos leads do Supabase legado para o harness (Fase 4).

Leitura única, sem dependência permanente do Supabase: normaliza os
3.373 telefones da base legada, funde duplicatas, importa em `leads_crm`
e `legacy_chat_history`, valida, e emite `relatorio_migracao.md` para
revisão humana antes do cutover. Idempotente -- rodar duas vezes dá o
mesmo resultado.

Este módulo concentra hoje só a etapa pura de normalização
(`normalizar_telefone`), que não depende de rede nem de banco e por isso
é testável isoladamente. A leitura via REST do Supabase, a fusão de
grupos e a escrita em `leads_crm`/`leads_descartados` entram nas tasks
seguintes da Fase 4.

A credencial do Supabase é segredo: entra por variável de ambiente,
nunca é versionada, e nunca deve ser impressa -- nem em log, nem no
relatório.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
