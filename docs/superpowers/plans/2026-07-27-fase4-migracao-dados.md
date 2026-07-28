# Fase 4 — Migração dos dados do Supabase

> **Para agentes:** SUB-SKILL OBRIGATÓRIA: use `superpowers:subagent-driven-development`.
> Os passos usam checkbox (`- [ ]`).

**Goal:** Trazer 3.373 leads, 8.920 mensagens de histórico e a blocklist do
Supabase legado para o harness, com chave canônica, duplicatas fundidas e
validações que **bloqueiam** o cutover se algo não fechar.

**Architecture:** Um script de leitura única (`scripts/migrar_supabase.py`), sem
dependência permanente do Supabase. Normaliza → funde → importa → valida →
emite relatório para revisão humana. Idempotente: rodar duas vezes dá o mesmo
resultado.

**Tech Stack:** psycopg async, httpx (REST do Supabase), Python 3.11+.

---

## Global Constraints

- Python 3.11+, deps por `uv`. Português brasileiro. Conventional Commits.
- Banco de dev na porta **5440**. Não rodar `make up`.
- **Não usar `make check`.** `ruff check`, `ruff format --check`, `pyright` só
  nos arquivos tocados.
- **Não fazer `git push`.** Nunca `git add -A` nem `git add .`.
- **A credencial do Supabase é segredo.** Ela entra por variável de ambiente,
  nunca versionada, e **nunca é impressa** — nem em log, nem em relatório.
- Migrações nunca são editadas depois de aplicadas. A última é a **014**.
- Camada de banco testada contra o Postgres real, nunca monkeypatchada.
- **Nos testes, nunca use timestamps idênticos entre linhas do mesmo grupo.**
  Duas tasks da Fase 3 produziram testes autoconfirmatórios exatamente assim.
- Suíte de partida: **743 passed, 13 skipped, 11 deselected** com
  `uv run pytest -m "not docker_demo"` (sem paths).

---

## O que foi medido, e onde a especificação erra

Tudo abaixo foi verificado contra o Supabase em **27/07/2026**. A base está
**viva** — a contagem mudou entre duas consultas na mesma sessão.

### Distribuição

| fase | linhas | pausados | sem follow-up |
|---|---|---|---|
| `formulario_preenchido` | 2.613 | 1 | 1.894 |
| `iniciou_conversa` | 354 | 11 | 15 |
| `qualificado` | 175 | 5 | 6 |
| `agendou_sessao` | 152 | 11 | 39 |
| `desqualificado` | 79 | 1 | 28 |
| `perdido` | **0** | — | — |

Origens: `respondiapp_form` 2.206, `linkedin_form` 1.018, `whatsapp_direct` 149.
Histórico: **8.920 mensagens em 736 sessões**, e **as 736 casam com um lead** —
a afirmação da especificação se confirma.

### Formas de telefone

| forma | linhas | ativos |
|---|---|---|
| BR com 9º dígito | 2.574 | 0 |
| BR canônico | 776 | 680 |
| outro plausível | 18 | **1** |
| local sem DDI | 2 | 0 |
| zero de tronco | 2 | 0 |
| irrecuperável | 1 | 0 |

### Erro 1 — existem estrangeiros legítimos

A especificação afirma que os ~18 restantes são erros de digitação e que
"**nenhum** é estrangeiro legítimo". Três parecem números reais:

| telefone | país | situação |
|---|---|---|
| `+14242123771` | EUA (+1 424) | `formulario_preenchido` |
| `351914355881` | Portugal (+351 914) | `formulario_preenchido`, follow-up ativo |
| `258864038352` | Moçambique (+258 86) | **`qualificado`**, ativo |

**E o `CHECK` da Fase 3 recusa o americano**, porque `phone !~ '^[0-9]{10,11}$'`
existe para barrar a forma local brasileira sem DDI — que tem exatamente o mesmo
comprimento de um número dos EUA com DDI. Não há como distinguir os dois só pelo
tamanho.

**Decisão: não relaxar o `CHECK`.** Ele protege a invariante que custou quatro
rodadas da Fase 3. Números de 10–11 dígitos vão para `leads_descartados` com o
motivo `colide_com_forma_local_br`, e o relatório os destaca para decisão humana.
Um lead americano em `formulario_preenchido` que nunca respondeu é perda
aceitável; o relatório existe justamente para que essa perda seja **vista**.

### Erro 2 — `google_event_id` não existe na origem

A tabela legada **não tem** `google_event_id`, `faturamento_mensal` nem
`qualificacao_notas`. As três são colunas novas.

Isso invalida a mitigação que as Fases 2 e 3 registraram ("backfill de
`google_event_id` na migração"): **não há de onde copiar**. Os 152 leads em
`agendou_sessao` chegam com a coluna nula, tendo reunião real na agenda do Silvio.

A consequência, herdada da Fase 2: a relaxação de `update_crm` lê `google_event_id`
nulo como "não tem reunião" e aceitaria devolver o lead para `qualificado`,
puxando o card do Pipedrive de 13 para 12 com a reunião marcada.

**Decisão: marcar, não adivinhar.** A migração grava
`metadata->>'reuniao_legada' = true` nos leads que chegam em `agendou_sessao` sem
`google_event_id`, e a relaxação passa a exigir que essa marca esteja ausente.
Buscar os eventos na API do Google por e-mail do participante é possível, mas
casar evento com lead sem chave é adivinhação — e adivinhar aqui cancela reunião
de gente real.

### Erro 3 — um telefone é a string `"null"`

Uma linha tem `phone = 'null'` — o texto, não o valor. Vai para
`leads_descartados` com motivo próprio, não pode virar `55null` nem string vazia.

### O que a especificação acerta

As regras de fusão, a lista de validações bloqueantes, o casamento das 736
sessões, e a observação de que **um lead ativo** (o de Moçambique, `qualificado`)
precisa de resolução manual antes do cutover.

---

## Task 0 (BLOQUEANTE): os 28 números da blocklist

Estão hardcoded no nó `Filtro e Permissao v002` do n8n, e **não existem no
repositório nem no Supabase**. Sem eles, gente que pediu para parar de receber
mensagem volta a receber — e a régua da Fase 3 é o único caminho que fala sem o
lead ter falado.

**Entregável:** `docs/evidencias/blocklist-n8n.md`, com os números como aparecem
no nó, sem edição. Redija nomes se houver.

A Task 6 não começa sem o arquivo. As Tasks 1 a 5 não dependem dele.

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `db/migrations/015_legacy_chat_history.sql` | `legacy_chat_history` + `leads_descartados` |
| `scripts/migrar_supabase.py` | leitura, normalização, fusão, import, validação |
| `src/whatsapp_langchain/agents/middleware/historico_legado.py` | injeção no primeiro turno |
| `src/whatsapp_langchain/agents/catalog/elevec_sdr/tools/crm.py` | relaxação passa a checar `reuniao_legada` |
| `tests/integration/test_migracao_015.py`, `test_migrar_supabase.py`, `test_historico_legado.py` | |

---

## Task 1: tabelas de destino

**Files:** `db/migrations/015_legacy_chat_history.sql`, `tests/integration/test_migracao_015.py`

- [ ] **Step 1: Escrever o teste que falha** — as duas tabelas existem, com as
  constraints certas; `legacy_chat_history.phone` referencia `leads_crm` e cai
  junto no `DELETE`; `leads_descartados` aceita telefone **em qualquer forma**,
  inclusive `'null'` e string vazia, porque é o depósito do que não passou.

- [ ] **Step 2: A migração**

```sql
-- 015_legacy_chat_history.sql
-- Destino do que vem do Supabase legado. Duas tabelas com contratos opostos:
-- legacy_chat_history só aceita telefone canônico (FK para leads_crm);
-- leads_descartados aceita QUALQUER coisa, porque é onde mora o que não
-- converge — inclusive a linha cujo phone é a string 'null'.

CREATE TABLE IF NOT EXISTS legacy_chat_history (
    id          BIGSERIAL PRIMARY KEY,
    phone       TEXT NOT NULL REFERENCES leads_crm(phone) ON DELETE CASCADE,
    ordem       INT  NOT NULL,
    papel       TEXT NOT NULL CHECK (papel IN ('human','ai')),
    conteudo    TEXT NOT NULL,
    UNIQUE (phone, ordem)
);
CREATE INDEX IF NOT EXISTS idx_legacy_chat_phone ON legacy_chat_history (phone, ordem);

CREATE TABLE IF NOT EXISTS leads_descartados (
    id           BIGSERIAL PRIMARY KEY,
    phone_origem TEXT,
    motivo       TEXT NOT NULL,
    linha        JSONB NOT NULL,
    descartado_em TIMESTAMPTZ DEFAULT now()
);
```

- [ ] **Step 3: Rodar, ver passar, mutação (mínimo 3), commit**

Mutações: tirar a FK; tirar o `UNIQUE (phone, ordem)`; deixar
`leads_descartados.phone_origem` com `NOT NULL`.

---

## Task 2: normalização e descarte

**Files:** `scripts/migrar_supabase.py`, `tests/unit/test_migrar_supabase.py`

**Interfaces:**
```python
@dataclass(frozen=True)
class Normalizado:
    canonico: str | None      # None => descartado
    motivo: str | None        # preenchido sse canonico is None

def normalizar_telefone(bruto: str | None) -> Normalizado
```

- [ ] **Step 1: Os testes, com os casos reais medidos**

```python
@pytest.mark.parametrize("bruto,esperado", [
    ("+5511987654321", "551187654321"),   # BR com 9 e com +
    ("5511987654321",  "551187654321"),
    ("551187654321",   "551187654321"),   # já canônico
    ("11987654321",    "551187654321"),   # local com 9
    ("1187654321",     "551187654321"),   # local sem 9
    ("55011987654321", "551187654321"),   # zero de tronco
    ("(11) 98765-4321","551187654321"),   # máscara
])
def test_formas_brasileiras_convergem(bruto, esperado):
    assert normalizar_telefone(bruto).canonico == esperado


@pytest.mark.parametrize("bruto,motivo", [
    ("null",            "telefone_ausente"),
    ("",                "telefone_ausente"),
    (None,              "telefone_ausente"),
    ("+14242123771",    "colide_com_forma_local_br"),   # EUA, 11 dígitos
    ("519985344",       "digitos_insuficientes"),
    ("5511666666665",   "sequencia_implausivel"),
])
def test_descartes_tem_motivo_nomeado(bruto, motivo):
    r = normalizar_telefone(bruto)
    assert r.canonico is None and r.motivo == motivo


def test_estrangeiro_de_12_digitos_passa_intacto():
    """Moçambique e Portugal cabem no CHECK; EUA não, por colisão de tamanho."""
    assert normalizar_telefone("258864038352").canonico == "258864038352"
    assert normalizar_telefone("351914355881").canonico == "351914355881"


def test_saida_sempre_satisfaz_o_check_do_banco():
    """Qualquer canônico devolvido tem que poder ser inserido em leads_crm."""
    import re
    for bruto in TODAS_AS_FORMAS_REAIS:          # fixture com as 3.373 formas
        c = normalizar_telefone(bruto).canonico
        if c is None:
            continue
        assert re.fullmatch(r"[0-9]{8,15}", c)
        assert not re.match(r"^55[0-9]{2}9[0-9]{8}$", c)
        assert not c.startswith("550")
        assert not re.fullmatch(r"[0-9]{10,11}", c)
```

O último é o teste que importa mais: ele amarra o script ao `CHECK` do banco.
Se divergirem, a importação falha no meio.

- [ ] **Step 2: Implementar, reusando `shared/phone.py`**

`canonicalizar` já faz a maior parte. **Não reimplemente** — importe. O que o
script acrescenta é a classificação do descarte, que `phone.py` não tem porque
não precisa.

- [ ] **Step 3: Mutação (mínimo 6), commit**

Cada motivo de descarte tem que morrer sob mutação, e a colisão de 10–11 dígitos
em especial.

---

## Task 3: fusão de duplicatas e relatório

**Files:** `scripts/migrar_supabase.py`, `tests/unit/test_migrar_supabase.py`

Regra de merge, **idêntica** à da especificação e à da migração 014 — e esta é a
**terceira** cópia, depois de `shared/leads.py` e da 014. Escreva isso no
cabeçalho da função: **as três precisam andar juntas**.

| campo | regra |
|---|---|
| `phase` | `agendou_sessao` > `desqualificado` = `perdido` > `qualificado` > `iniciou_conversa` > `formulario_preenchido`; `NULL` perde de tudo |
| `created_at` / `last_interaction_at` | mais antigo / mais recente |
| `pipedriveid`, `email`, `name`, `username`, `source` | primeiro não-nulo, priorizando a fase mais avançada |
| `followup_count` | o maior |
| `agent_active`, `followup_active` | **`false` vence** |
| `agent_reactivate_at` | **fora do coalesce** — acompanha quem ganhou `agent_active` |
| `metadata` | merge, com `linhas_fundidas` guardando os telefones originais |

- [ ] **Step 1: Testes** — par; trio; grupo com um lado pausado; grupo onde a
  fase mais avançada está na linha **mais antiga** (`agendou_sessao` não pode ser
  enterrado por um `perdido` recente); `agent_reactivate_at` da linha perdedora
  **não** ressuscitando.

- [ ] **Step 2: Implementar. Step 3: o relatório**

`relatorio_migracao.md`, para leitura humana antes do cutover:
- total na origem, migrados, descartados — e a soma tem que fechar
- cada grupo fundido: telefones de origem, canônico de destino, e **qual campo
  veio de qual linha**
- todos os descartes, com motivo
- **uma seção destacada** para o que exige decisão humana: o lead de Moçambique
  em `qualificado`, os três estrangeiros, e qualquer grupo em que a fusão tenha
  mudado `phase` ou `agent_active`

- [ ] **Step 4: Mutação (mínimo 8), commit**

---

## Task 4: importação e validações bloqueantes

**Files:** `scripts/migrar_supabase.py`, `tests/integration/test_migrar_supabase.py`

- [ ] **Step 1: Testes das validações** — cada uma tem que **abortar** a migração,
  não avisar:

| validação | por quê |
|---|---|
| origem = migrados + descartados | nada some em silêncio |
| toda linha em `leads_crm` passa no `CHECK` | senão a importação quebra no meio |
| contagem por fase no destino ≥ origem, para as fases avançadas | a fusão só promove, nunca rebaixa |
| 100% dos `session_id` do histórico existem em `leads_crm` | FK garante, mas o erro tem que ser legível |
| **nenhuma linha tem `last_inbound_at`** | contrato da Fase 3 |

A última é o contrato que a Fase 3 deixou escrito: **o importador nunca escreve
`last_inbound_at`.** Ele nasce `NULL` e só o gate o preenche. Copiar
`last_interaction_at` daqui ressuscitaria em escala o problema que a 013 evita —
lead que recebe follow-up rejeitado pela Meta porque a janela parecia aberta.

- [ ] **Step 2: Marcar as reuniões legadas**

Leads que chegam em `agendou_sessao` recebem `metadata->>'reuniao_legada' = true`,
porque o `google_event_id` não existe na origem.

- [ ] **Step 3: A relaxação de `update_crm` passa a respeitar a marca**

Em `crm.py`, a condição que hoje permite `agendou_sessao → qualificado` quando
`google_event_id is null` passa a exigir **também** que `reuniao_legada` não
esteja marcado. Teste: lead legado em `agendou_sessao` **não** pode ser rebaixado.

- [ ] **Step 4: Idempotência** — rodar duas vezes dá o mesmo resultado, sem
  duplicar nem falhar. Teste explícito.

- [ ] **Step 5: Mutação (mínimo 6), commit**

Cada validação, mutada para "avisa em vez de abortar", tem que derrubar teste.

---

## Task 5: histórico e continuidade

Sem isto, **529 leads ativos** (`iniciou_conversa` + `qualificado`) recebem a
saudação da Fase 1 outra vez — inclusive quem já passou pelo portão de faturamento.

**Files:** `scripts/migrar_supabase.py`, `agents/middleware/historico_legado.py`,
`tests/integration/test_historico_legado.py`

- [ ] **Step 1: Importar as últimas 12 mensagens por sessão**

12 é a janela que o n8n usava (`docs/evidencias/prompt-renata-n8n.md`). O
`session_id` é o telefone e passa pela **mesma** normalização da Task 2.

O formato do `message` JSONB do n8n precisa ser inspecionado antes — leia uma
amostra real e trate o que encontrar, não o que você espera encontrar.

- [ ] **Step 2: O middleware**

No primeiro turno de um `thread_id` **sem checkpoint**, carrega o histórico
daquele telefone, injeta, e marca `metadata->>'historico_injetado'`. A marca é o
que impede injeção repetida — e ela tem que ser gravada **antes** do turno, não
depois, senão uma falha no meio injeta de novo.

- [ ] **Step 3: Testes** — lead com histórico e sem checkpoint recebe; segunda
  chamada **não** recebe; lead sem histórico não quebra; lead com checkpoint
  existente é ignorado.

- [ ] **Step 4: Mutação (mínimo 5), commit**

---

## Task 6: blocklist

> Não comece sem `docs/evidencias/blocklist-n8n.md` (Task 0).

- [ ] Os 28 números passam pela **mesma** normalização e entram em `blocklist`.
- [ ] Teste: cada número da evidência, nas quatro formas reais (com `+`, com 9,
      sem 9, com máscara), é barrado pelo gate **e** pela régua de follow-up.
- [ ] Documentar em `docs/AGENTE_ELEVEC.md` e atualizar `CLAUDE.md`.

---

## Auto-revisão

**Cobertura:** todos os itens da seção "Export único do Supabase" e
"Continuidade das conversas" da especificação têm task.

**Divergências conscientes:**

| o quê | por quê |
|---|---|
| Estrangeiro de 10–11 dígitos é descartado | colide com a forma local BR; relaxar o `CHECK` desfaz a invariante da Fase 3 |
| `google_event_id` não é adivinhado | casar evento com lead sem chave cancelaria reunião de gente real |
| `reuniao_legada` em vez de backfill | marca o que não se sabe, em vez de fingir que se sabe |

**Pendências que ficam para a Fase 5 (cutover):**

- A base está **viva**. A migração precisa rodar com os workflows do n8n já
  desligados, e a validação `origem = migrados + descartados` só fecha se nada
  escrever durante a janela.
- O lead de Moçambique em `qualificado` precisa de decisão humana.
- Ligar `FOLLOWUP_ENABLED` exige antes cobrir o wiring de `main()`, que hoje tem
  cobertura zero (dívida registrada na Fase 3).
