"""Faixas de faturamento do funil da EleveC, e como chegar nelas.

As seis faixas são as do workflow `nXuIqeQ0tBialBsR` (YAY FORMS) do n8n, nó
`4. Qualifica por faturamento` — texto verbatim, porque é o mesmo valor que
os formulários gravam em `leads_crm.faturamento_mensal`. Inventar uma
grafia diferente aqui faria o mesmo lead ter duas representações e nenhum
relatório fecharia.

**Por que isto é código e não prompt.** No n8n o faturamento vinha de
múltipla escolha: a faixa já chegava pronta. Numa conversa de WhatsApp o
lead escreve "uns 10 mil", e alguém precisa classificar. Medido com LLM
real, o modelo grava "20 mil" cru numa rodada e a faixa certa noutra — e da
faixa dependem duas consequências que mexem com gente: abaixo de R$ 5 mil a
reunião é cancelada, acima de R$ 25 mil o lead vai para o Silvio. Regra de
negócio decidida por prompt é sugestão, não garantia. Ver
`docs/AGENTE_ELEVEC.md`: "`if` garante, prompt só pede".
"""

from __future__ import annotations

import re

MENOS_DE_3K = "Menos de R$ 3 mil/mês"
DE_3K_A_5K = "R$ 3 mil a R$ 5 mil/mês"
DE_5K_A_8K = "R$ 5 mil a R$ 8 mil/mês"
DE_8K_A_15K = "R$ 8mil a R$ 15 mil/mês"  # sem espaço depois do R$, como no n8n
DE_15K_A_25K = "R$ 15 mil a R$ 25 mil/mês"
ACIMA_DE_25K = "Acima de R$25 mil/mês"

FAIXAS = (
    MENOS_DE_3K,
    DE_3K_A_5K,
    DE_5K_A_8K,
    DE_8K_A_15K,
    DE_15K_A_25K,
    ACIMA_DE_25K,
)

# Abaixo do corte de R$ 5 mil: o n8n mandava estes dois para
# `DESQUALIFICADO (<5k)`, antes mesmo de a Renata entrar na conversa.
ABAIXO_DO_CORTE = frozenset({MENOS_DE_3K, DE_3K_A_5K})

# Teto de cada faixa e se ele é inclusivo. Ordem importa: o primeiro que
# couber vence.
#
# A convenção vem dos rótulos do formulário, que são explícitos nas duas
# pontas e ambíguos no meio:
#
#   "Menos de R$ 3 mil"      -> 3.000 NÃO cabe, sobe para a próxima
#   "R$ 15 mil a R$ 25 mil"  -> 25.000 cabe, porque a próxima é...
#   "Acima de R$25 mil"      -> ...literalmente ACIMA de 25 mil
#
# No meio os rótulos se sobrepõem ("5 mil" aparece em duas faixas) e o
# formulário resolvia isso deixando a pessoa escolher. Aqui a regra é piso
# inclusivo, teto exclusivo — e a escolha não muda consequência nenhuma:
# 5.000 e 8.000 qualificam dos dois jeitos.
_LIMITES: tuple[tuple[float, bool, str], ...] = (
    (3_000, False, MENOS_DE_3K),
    (5_000, False, DE_3K_A_5K),
    (8_000, False, DE_5K_A_8K),
    (15_000, False, DE_8K_A_15K),
    (25_000, True, DE_15K_A_25K),
)

# "1 milhão", "1,5 milhões"
_MILHAO = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:milh(?:ão|ões|ao|oes))", re.I)
# "10 mil", "10mil", "10k", "R$ 8mil"
_MIL = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:mil\b|k\b)", re.I)
# Número solto com separadores: "6.000", "6000", "4.999,90"
_NUMERO = re.compile(r"\d[\d.,]*")


def _para_reais(bruto: str) -> float | None:
    """Interpreta um número escrito como brasileiro escreve.

    `6.000` é seis mil, não seis — o ponto é separador de milhar. `4.999,90`
    tem vírgula decimal. Um parser ingênuo com `float()` erraria os dois por
    ordens de grandeza, e ordem de grandeza aqui é a diferença entre manter
    e cancelar uma reunião.
    """
    texto = bruto.strip()
    if not texto:
        return None
    if "," in texto:
        inteiro, _, decimal = texto.rpartition(",")
        texto = f"{inteiro.replace('.', '')}.{decimal}"
    else:
        texto = texto.replace(".", "")
    try:
        return float(texto)
    except ValueError:
        return None


def _valores(texto: str) -> list[float]:
    """Todos os valores monetários citados, em reais.

    Devolve lista porque intervalo é comum ("entre 8 e 12 mil") e quem
    chama decide o que fazer com mais de um.
    """
    achados: list[float] = []

    for m in _MILHAO.finditer(texto):
        if (v := _para_reais(m.group(1))) is not None:
            achados.append(v * 1_000_000)

    restante = _MILHAO.sub(" ", texto)
    for m in _MIL.finditer(restante):
        if (v := _para_reais(m.group(1))) is not None:
            achados.append(v * 1_000)

    # "entre 8 e 12 mil": o `8` não tem sufixo, mas o `12 mil` tem. Sem
    # herdar a escala, o 8 viraria oito reais e o intervalo inteiro cairia
    # na faixa errada.
    escala = 1_000 if achados and _MIL.search(restante) else 1
    for m in _NUMERO.finditer(_MIL.sub(" ", restante)):
        if (v := _para_reais(m.group())) is None:
            continue
        achados.append(v * escala if v < 1_000 else v)

    return achados


def faixa_de_faturamento(bruto: str | None) -> str | None:
    """Faixa correspondente, ou `None` quando não dá para decidir.

    `None` significa "não sei", nunca "zero". Tratar "prefiro não dizer"
    como abaixo do corte cancelaria a reunião de alguém que talvez faturasse
    50 mil — por isso o indecidível sobe para quem chama, que aciona humano.

    Intervalo casa pelo MAIOR valor: "entre 3 e 6 mil" é um lead de 6k sendo
    conservador. Usar o piso desqualificaria quem passa do corte, e tirar
    reunião de quem tinha direito é o erro caro.
    """
    if not bruto:
        return None

    texto = bruto.strip()
    if not texto:
        return None

    # Texto do próprio formulário volta idêntico, sem passar pelo parser.
    for faixa in FAIXAS:
        if texto.casefold() == faixa.casefold():
            return faixa

    valores = _valores(texto)
    if not valores:
        return None

    valor = max(valores)
    for teto, inclusivo, faixa in _LIMITES:
        if valor <= teto if inclusivo else valor < teto:
            return faixa
    return ACIMA_DE_25K
