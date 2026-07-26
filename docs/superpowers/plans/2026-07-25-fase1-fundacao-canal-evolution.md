# Fase 1 — Fundação e Canal Evolution: Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer uma mensagem de WhatsApp entrar pela Evolution API, passar pelo gate de ingestão da EleveC (canonicalização, blocklist, `fromMe`, `agent_active`), ser enfileirada e respondida no mesmo canal.

**Architecture:** A API ganha `/webhook/evolution`, que resolve e canonicaliza o telefone, aplica as regras de negócio contra `leads_crm` e enfileira com `channel='evolution'`. O Worker ganha `EvolutionClient` para envio e um caminho de download de mídia próprio. Toda a lógica de telefone vive num módulo puro e testável.

**Tech Stack:** Python 3.11+, FastAPI, psycopg (async), httpx, pytest (`asyncio_mode=auto`), `uv` para dependências.

## Global Constraints

- **Gerenciador de dependências é `uv`.** Nunca `pip install`. `uv.lock` é fonte de verdade.
- **Duas representações de telefone, nunca misturar:**
  - **Canônico** — só dígitos, brasileiro sem o 9: `5511987654321` → `551187654321`. Usado em `leads_crm.phone`, `blocklist.phone`, `legacy_chat_history.phone`.
  - **E.164 do harness** — canônico com `+` na frente: `+551187654321`. Usado em `message_queue.phone_number` e, por consequência, no `thread_id`.
  - A conversão é sempre explícita, via `to_e164()` / `from_e164()`. Nunca concatenar `"+"` na mão.
- **Números não-brasileiros passam sem canonicalização** — a regra do 9º dígito é brasileira; aplicá-la a estrangeiro corrompe o número.
- **Idioma do código e dos commits: português brasileiro.** Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- **Sem comentários óbvios nem docstrings longas** — o código do harness é didático por design, mas não redundante.
- **Todo commit termina com:** `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- **Lint e tipos antes de cada commit, escopados aos arquivos alterados:**
  `uv run ruff check <arquivos>`, `uv run ruff format --check <arquivos>`,
  `uv run pyright <arquivos>`.
  **Não use `make check` como gate**: `stress/locustfile.py` já tem 9 erros de
  lint no commit base desta branch, herdados do template. Exigir `make check`
  limpo faria cada tarefa perseguir um problema que não é dela. Corrigir o
  locustfile é trabalho separado, fora do escopo desta fase.
- Valores fixos desta instância: servidor Evolution `https://evolution.ju39tu.easypanel.host`, instância `instancia-apioficial`, integração `WHATSAPP-BUSINESS`.

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `src/whatsapp_langchain/shared/phone.py` | **Criar.** Puro, sem I/O: canonicalização, variações do 9º dígito, score de JID, conversão E.164. |
| `src/whatsapp_langchain/shared/leads.py` | **Criar.** Único ponto de acesso a `leads_crm` e `blocklist`. Contém o gate de ingestão. |
| `db/migrations/007_elevec.sql` | **Criar.** Schema do SDR. |
| `src/whatsapp_langchain/worker/evolution_client.py` | **Criar.** Envio outbound e download de mídia da Evolution. |
| `src/whatsapp_langchain/server/routes/webhook_evolution.py` | **Criar.** Rota inbound: parseia, chama o gate, enfileira. |
| `src/whatsapp_langchain/shared/models.py` | **Modificar.** `MessagingChannel.EVOLUTION`. |
| `src/whatsapp_langchain/shared/config.py` | **Modificar.** Settings da Evolution. |
| `src/whatsapp_langchain/worker/media.py` | **Modificar.** `download_media` passa a decidir por canal. |
| `src/whatsapp_langchain/worker/processor.py` | **Modificar.** Reconhecer o cliente Evolution. |
| `src/whatsapp_langchain/server/main.py` | **Modificar.** Registrar a rota. |

O agente `elevec_sdr`, o follow-up e o webhook do ChatWoot **não** entram nesta fase — são a Fase 2 e 3. Ao final desta fase o canal funciona ponta a ponta com um agente já existente do catálogo.

---

### Task 1: Confirmar `remoteJidAlt` na instância real

**Pré-condição bloqueante do spec.** Toda a canonicalização do 9º dígito assume que `remoteJidAlt` chega preenchido. `remoteJidAlt` é conceito do Baileys e esta instância roda `WHATSAPP-BUSINESS`. Não presumir.

**Files:**
- Create: `docs/evidencias/payload-evolution-real.json`

- [ ] **Step 1: Buscar mensagens reais armazenadas na instância**

A Evolution guarda as mensagens recebidas. Este endpoint devolve a estrutura
exata do `key`, que é o que precisamos inspecionar. Substitua `<APIKEY>` pelo
valor de `evo_oficial.apikey` do arquivo de credenciais.

```bash
curl -s -X POST \
  'https://evolution.ju39tu.easypanel.host/chat/findMessages/instancia-apioficial' \
  -H 'apikey: <APIKEY>' \
  -H 'Content-Type: application/json' \
  -d '{"where":{},"limit":5}' \
  -o docs/evidencias/payload-evolution-real.json
```

- [ ] **Step 2: Inspecionar as chaves presentes**

```bash
python3 -c "
import json
d = json.load(open('docs/evidencias/payload-evolution-real.json'))
msgs = d.get('messages', {}).get('records', d if isinstance(d, list) else [])
for m in msgs[:5]:
    k = m.get('key', {})
    print('remoteJid   :', k.get('remoteJid'))
    print('remoteJidAlt:', k.get('remoteJidAlt'))
    print('fromMe      :', k.get('fromMe'))
    print('pushName    :', m.get('pushName'))
    print('---')
"
```

- [ ] **Step 3: Decidir com base na evidência**

| Resultado | Ação |
|---|---|
| `remoteJidAlt` preenchido em pelo menos uma mensagem | Segue o plano como está. |
| `remoteJidAlt` sempre ausente/`None` | `resolver_telefone()` da Task 2 usa **só** `remoteJid`. O teste `test_score_prefere_jid_oficial` deve ser ajustado para refletir isso, e o spec atualizado. |

- [ ] **Step 4: Commit da evidência**

```bash
git add docs/evidencias/payload-evolution-real.json
git commit -m "docs: payload real da Evolution para validar remoteJidAlt

Resolve a pré-condição bloqueante da Fase 1.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Módulo de telefone (`shared/phone.py`)

Módulo puro — sem banco, sem rede. É a peça de que todas as outras dependem.

**Files:**
- Create: `src/whatsapp_langchain/shared/phone.py`
- Test: `tests/unit/test_phone.py`

**Interfaces:**
- Consumes: nada.
- Produces:
  - `canonicalizar(bruto: str | None) -> str | None` — dígitos, BR sem o 9; `None` se não converge.
  - `variacoes(canonico: str) -> tuple[str, str]` — `(com_9, sem_9)`; para não-BR devolve `(canonico, canonico)`.
  - `resolver_telefone(key: dict) -> str | None` — valida e canonicaliza `key["remoteJid"]`.

> **Resultado da Task 1:** `remoteJidAlt` está **ausente em 50 de 50** mensagens
> reais desta instância — a integração `WHATSAPP-BUSINESS` não popula esse campo
> herdado do Baileys. O score entre dois candidatos, previsto originalmente,
> seria código morto. `resolver_telefone` lê apenas `remoteJid`, mantendo a
> validação de formato e a rejeição de grupos.
  - `to_e164(canonico: str) -> str` — prefixa `+`.
  - `from_e164(e164: str) -> str` — remove `+`.

- [ ] **Step 1: Escrever os testes que falham**

Arquivo `tests/unit/test_phone.py`:

```python
"""Testes do módulo de telefone — puros, sem I/O."""

import pytest

from whatsapp_langchain.shared.phone import (
    canonicalizar,
    from_e164,
    resolver_telefone,
    to_e164,
    variacoes,
)


@pytest.mark.parametrize(
    "bruto,esperado",
    [
        ("+5511987654321", "551187654321"),   # E.164 com + e com 9
        ("5511987654321", "551187654321"),    # dígitos com 9
        ("551187654321", "551187654321"),     # já canônico
        ("11987654321", "551187654321"),      # sem DDI
        ("1187654321", "551187654321"),       # sem DDI e sem 9
        ("(11) 98765-4321", "551187654321"),  # formatado
        ("5511987654321@s.whatsapp.net", "551187654321"),
    ],
)
def test_canonicaliza_numeros_brasileiros(bruto, esperado):
    assert canonicalizar(bruto) == esperado


def test_numero_estrangeiro_nao_perde_digito():
    # +1 415 555 0123 — o "9" brasileiro não pode ser aplicado aqui.
    assert canonicalizar("+14155550123") == "14155550123"


@pytest.mark.parametrize("bruto", [None, "", "abc", "123", "0" * 20])
def test_entrada_invalida_devolve_none(bruto):
    assert canonicalizar(bruto) is None


def test_variacoes_brasileiras():
    assert variacoes("551187654321") == ("5511987654321", "551187654321")


def test_variacoes_estrangeiro_sao_iguais():
    assert variacoes("14155550123") == ("14155550123", "14155550123")


def test_resolve_jid_com_sufixo_whatsapp():
    key = {"remoteJid": "5511987654321@s.whatsapp.net", "fromMe": False}
    assert resolver_telefone(key) == "551187654321"


def test_resolve_jid_sem_sufixo():
    assert resolver_telefone({"remoteJid": "5511987654321"}) == "551187654321"


def test_resolver_ignora_grupo():
    assert resolver_telefone({"remoteJid": "1234-5678@g.us"}) is None


def test_resolver_sem_candidato_valido():
    assert resolver_telefone({"remoteJid": ""}) is None
    assert resolver_telefone({}) is None


def test_resolver_ignora_remote_jid_alt():
    """A integração WHATSAPP-BUSINESS não popula remoteJidAlt (50/50 ausente).

    Se um dia aparecer, não pode influenciar o resultado sem decisão explícita.
    """
    key = {"remoteJid": "5511987654321@s.whatsapp.net", "remoteJidAlt": "5599999999999"}
    assert resolver_telefone(key) == "551187654321"


def test_conversao_e164_ida_e_volta():
    assert to_e164("551187654321") == "+551187654321"
    assert from_e164("+551187654321") == "551187654321"
    assert from_e164("551187654321") == "551187654321"
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/unit/test_phone.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'whatsapp_langchain.shared.phone'`

- [ ] **Step 3: Implementar o módulo**

Arquivo `src/whatsapp_langchain/shared/phone.py`:

```python
"""Canonicalização de telefone brasileiro e resolução de JID do WhatsApp.

Duas representações convivem no projeto:

- canônico: só dígitos, brasileiro SEM o 9º dígito (`551187654321`) —
  usado em leads_crm, blocklist e legacy_chat_history.
- E.164 do harness: canônico com "+" (`+551187654321`) — usado em
  message_queue.phone_number e no thread_id.

A regra do 9º dígito só se aplica a números brasileiros. Estrangeiros
passam apenas com os dígitos preservados.

Limitação conhecida e aceita: um número estrangeiro com exatamente 10 ou
11 dígitos e sem DDI é indistinguível de um brasileiro sem DDI, e ganharia
o prefixo 55. Medição na base de produção: 3.301 de 3.319 leads são
brasileiros válidos e nenhum é estrangeiro legítimo. Se isso mudar, a
entrada precisa trazer o DDI explícito.
"""

import re

BR_COM_9 = re.compile(r"^55(\d{2})9(\d{8})$")
BR_SEM_9 = re.compile(r"^55(\d{2})(\d{8})$")
LOCAL_COM_9 = re.compile(r"^(\d{2})9(\d{8})$")
LOCAL_SEM_9 = re.compile(r"^(\d{2})(\d{8})$")


def canonicalizar(bruto: str | None) -> str | None:
    """Reduz qualquer forma de telefone à representação canônica."""
    if not bruto:
        return None

    digitos = re.sub(r"\D", "", bruto.split("@", 1)[0])
    if not 8 <= len(digitos) <= 15:
        return None

    if m := LOCAL_COM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"
    if m := LOCAL_SEM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"
    if m := BR_COM_9.match(digitos):
        return f"55{m.group(1)}{m.group(2)}"

    return digitos


def variacoes(canonico: str) -> tuple[str, str]:
    """Devolve (com_9, sem_9). Para não-BR, ambos são o próprio número."""
    if m := BR_SEM_9.match(canonico):
        return f"55{m.group(1)}9{m.group(2)}", canonico
    return canonico, canonico


def resolver_telefone(key: dict) -> str | None:
    """Canonicaliza o remoteJid do payload da Evolution.

    Só `remoteJid` é lido: `remoteJidAlt` não é populado pela integração
    WHATSAPP-BUSINESS (verificado em 50 de 50 mensagens reais da instância).
    Grupos (@g.us) e JIDs fora do tamanho esperado são rejeitados.
    """
    valor = key.get("remoteJid")
    if not valor:
        return None

    texto = str(valor)
    if "@g.us" in texto:
        return None

    digitos = re.sub(r"\D", "", texto.split("@", 1)[0])
    if not 12 <= len(digitos) <= 14:
        return None

    return canonicalizar(digitos)


def to_e164(canonico: str) -> str:
    return canonico if canonico.startswith("+") else f"+{canonico}"


def from_e164(e164: str) -> str:
    return e164.lstrip("+")
```

- [ ] **Step 4: Rodar os testes**

Run: `uv run pytest tests/unit/test_phone.py -v`
Expected: PASS — 21 testes (7 + 5 vêm de `parametrize`).

Se `test_numero_estrangeiro_nao_perde_digito` falhar, a ordem das regexes está
errada: `LOCAL_COM_9`/`LOCAL_SEM_9` só podem casar números de 10–11 dígitos, e
`14155550123` tem 11 — verifique que `LOCAL_COM_9` exige o `9` na terceira
posição, o que `14155550123` não tem (`1`,`4`,`1`...). O teste existe justamente
para travar essa regressão.

- [ ] **Step 5: Verificar lint e tipos**

Run: `make check`
Expected: sem erros.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/shared/phone.py tests/unit/test_phone.py
git commit -m "feat: módulo de canonicalização de telefone

Centraliza a regra do 9º dígito, o score de JID do WhatsApp e a conversão
entre canônico e E.164. O n8n duplica essa lógica em três lugares, o que
é a origem das inconsistências de identidade de lead.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Migração `007_elevec.sql`

**Files:**
- Create: `db/migrations/007_elevec.sql`
- Test: `tests/integration/test_migracao_007.py`

**Interfaces:**
- Produces: tabelas `leads_crm`, `blocklist`, `legacy_chat_history`, `leads_descartados`; enums `lead_phase`, `lead_source`.

- [ ] **Step 1: Escrever o teste que falha**

Arquivo `tests/integration/test_migracao_007.py`:

```python
"""Verifica que a migração 007 cria o schema do SDR da EleveC."""

import pytest
from psycopg import errors

from whatsapp_langchain.shared.db import get_pool


@pytest.mark.asyncio
async def test_tabelas_do_sdr_existem():
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public'"
        )
        tabelas = {linha[0] for linha in await cur.fetchall()}

    assert {"leads_crm", "blocklist", "legacy_chat_history",
            "leads_descartados"} <= tabelas


@pytest.mark.asyncio
async def test_phone_rejeita_formato_invalido():
    """Espera CheckViolation, não Exception genérica.

    Com `Exception`, o teste passaria por erro de conexão, coluna inexistente
    ou qualquer outra falha — sem provar que foi o CHECK de formato.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        with pytest.raises(errors.CheckViolation):
            await conn.execute(
                "insert into leads_crm (phone) values ('+5511987654321')"
            )


@pytest.mark.asyncio
async def test_phone_aceita_canonico():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone) values ('551199990000') "
            "on conflict do nothing"
        )
        cur = await conn.execute(
            "select phase from leads_crm where phone = '551199990000'"
        )
        linha = await cur.fetchone()
        await conn.execute("delete from leads_crm where phone = '551199990000'")

    assert linha[0] == "formulario_preenchido"
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `make up && uv run pytest tests/integration/test_migracao_007.py -v`
Expected: FAIL — `relation "leads_crm" does not exist`

- [ ] **Step 3: Escrever a migração**

Arquivo `db/migrations/007_elevec.sql`:

```sql
-- 007_elevec.sql
-- Schema do SDR da EleveC: funil de leads, blocklist e histórico legado.
-- Roda automaticamente no startup da API (run_migrations).

DO $$ BEGIN
    CREATE TYPE lead_phase AS ENUM (
        'formulario_preenchido', 'iniciou_conversa', 'qualificado',
        'agendou_sessao', 'desqualificado', 'perdido');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE lead_source AS ENUM (
        'linkedin_form', 'respondiapp_form', 'whatsapp_direct', 'manual_import');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS leads_crm (
    phone                TEXT PRIMARY KEY CHECK (phone ~ '^[0-9]{8,15}$'),
    pipedriveid          TEXT,
    name                 TEXT,
    username             TEXT,
    email                TEXT,
    faturamento_mensal   TEXT,
    qualificacao_notas   TEXT,
    google_event_id      TEXT,
    phase                lead_phase  DEFAULT 'formulario_preenchido',
    source               lead_source,
    followup_count       INT         DEFAULT 0,
    followup_active      BOOLEAN     DEFAULT true,
    agent_active         BOOLEAN     DEFAULT true,
    agent_reactivate_at  TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT now(),
    last_interaction_at  TIMESTAMPTZ DEFAULT now(),
    metadata             JSONB       DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS blocklist (
    phone      TEXT PRIMARY KEY CHECK (phone ~ '^[0-9]{8,15}$'),
    motivo     TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS legacy_chat_history (
    phone   TEXT,
    idx     INT,
    role    TEXT,
    content TEXT,
    PRIMARY KEY (phone, idx)
);

CREATE TABLE IF NOT EXISTS leads_descartados (
    phone_original TEXT,
    motivo         TEXT,
    payload        JSONB,
    created_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_leads_followup
    ON leads_crm (last_interaction_at, phase)
    WHERE followup_active AND agent_active;
```

> As colunas do índice **não** repetem `followup_active`/`agent_active`: dentro
> do predicado parcial elas já são sempre `true` e não discriminam nada. Quem
> discrimina é `last_interaction_at` (o corte de tempo de cada nível) e `phase`
> (o `NOT IN` que exclui leads fechados).

- [ ] **Step 4: Aplicar e rodar os testes**

Run: `make migrate && uv run pytest tests/integration/test_migracao_007.py -v`
Expected: PASS — 3 testes.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/007_elevec.sql tests/integration/test_migracao_007.py
git commit -m "feat: migração 007 com o schema do SDR da EleveC

Cria leads_crm com CHECK de formato no telefone, blocklist,
legacy_chat_history e leads_descartados, mais o índice parcial que o
follow-up vai varrer a cada 5 minutos.

O CHECK aceita 8-15 dígitos em vez de exigir o padrão brasileiro: a
regra do 9º dígito é do Brasil e não pode rejeitar contato estrangeiro.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Acesso a leads e gate de ingestão (`shared/leads.py`)

**Files:**
- Create: `src/whatsapp_langchain/shared/leads.py`
- Test: `tests/integration/test_gate_ingestao.py`

**Interfaces:**
- Consumes: `canonicalizar`, `variacoes` de `shared.phone`.
- Produces:
  - `ResultadoGate` — dataclass com `aceito: bool`, `motivo: str | None`, `canonico: str | None`, `lead: dict | None`.
  - `async aplicar_gate(pool, key: dict, push_name: str | None) -> ResultadoGate`

- [ ] **Step 1: Escrever os testes que falham**

Arquivo `tests/integration/test_gate_ingestao.py`:

```python
"""Gate de ingestão: as regras que decidem se a mensagem vira item de fila."""

import pytest

from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate

TELEFONE = "551188887777"
JID = {"remoteJid": "5511988887777@s.whatsapp.net", "fromMe": False}


@pytest.fixture
async def limpar():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute("delete from blocklist where phone = %s", (TELEFONE,))
    yield
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute("delete from blocklist where phone = %s", (TELEFONE,))


async def test_lead_novo_e_criado_e_aceito(limpar):
    pool = await get_pool()
    r = await aplicar_gate(pool, JID, push_name="Fulano")

    assert r.aceito is True
    assert r.canonico == TELEFONE
    assert r.lead["name"] == "Fulano"
    assert r.lead["phase"] == "iniciou_conversa"


async def test_blocklist_descarta(limpar):
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("insert into blocklist (phone) values (%s)", (TELEFONE,))

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is False
    assert r.motivo == "blocklist"


async def test_from_me_desliga_agente_e_descarta(limpar):
    pool = await get_pool()
    await aplicar_gate(pool, JID, push_name="Fulano")

    r = await aplicar_gate(pool, {**JID, "fromMe": True}, push_name=None)

    assert r.aceito is False
    assert r.motivo == "from_me"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active, followup_active from leads_crm where phone = %s",
            (TELEFONE,),
        )
        agent_active, followup_active = await cur.fetchone()

    assert agent_active is False
    assert followup_active is False


async def test_agente_desligado_nao_escreve_nada(limpar):
    """A checagem vem ANTES do upsert — lead pausado não tem contador zerado."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, agent_active, followup_count, "
            "last_interaction_at) values (%s, false, 2, '2020-01-01')",
            (TELEFONE,),
        )

    r = await aplicar_gate(pool, JID, push_name="Fulano")

    assert r.aceito is False
    assert r.motivo == "agente_desligado"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select followup_count, extract(year from last_interaction_at)::int "
            "from leads_crm where phone = %s",
            (TELEFONE,),
        )
        contador, ano = await cur.fetchone()

    assert contador == 2, "followup_count não pode ser zerado para lead pausado"
    assert ano == 2020, "last_interaction_at não pode ser renovado"


async def test_telefone_invalido_descarta(limpar):
    pool = await get_pool()
    r = await aplicar_gate(pool, {"remoteJid": "abc@g.us"}, push_name=None)

    assert r.aceito is False
    assert r.motivo == "telefone_invalido"


async def test_encontra_lead_gravado_com_9(limpar):
    """Lead salvo na forma não-canônica ainda é encontrado."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into leads_crm (phone, phase) values (%s, 'qualificado')",
            ("5511988887777",),
        )

    r = await aplicar_gate(pool, JID, push_name=None)

    assert r.aceito is True
    assert r.canonico == TELEFONE
    assert r.lead["phase"] == "qualificado", "a fase não pode regredir"

    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", ("5511988887777",))
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/integration/test_gate_ingestao.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'whatsapp_langchain.shared.leads'`

- [ ] **Step 3: Implementar o gate**

Arquivo `src/whatsapp_langchain/shared/leads.py`:

```python
"""Acesso a leads_crm e o gate de ingestão do SDR.

A ORDEM das regras importa e replica o SQL `Add New Lead` do n8n:
a checagem de agent_active vem ANTES do upsert. Um lead em handover não
pode ter followup_count zerado nem last_interaction_at renovado — se
tivesse, ao ser reativado receberia a escada de follow-up do zero.
"""

from dataclasses import dataclass
from typing import Any

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# Atenção: NUNCA fazer `conn.row_factory = dict_row` numa conexão emprestada
# do pool. O AsyncConnectionPool não restaura o atributo ao devolver a conexão
# (`_reset_connection` só faz rollback), então o estado vaza para o próximo
# checkout e queries de tupla passam a devolver as chaves em vez dos valores —
# falha silenciosa. Use `conn.cursor(row_factory=dict_row)` escopado.

from whatsapp_langchain.shared.phone import canonicalizar, resolver_telefone, variacoes

logger = structlog.get_logger()


@dataclass
class ResultadoGate:
    aceito: bool
    motivo: str | None = None
    canonico: str | None = None
    lead: dict[str, Any] | None = None


async def aplicar_gate(
    pool: AsyncConnectionPool,
    key: dict[str, Any],
    push_name: str | None,
) -> ResultadoGate:
    canonico = resolver_telefone(key)
    if not canonico:
        return ResultadoGate(False, "telefone_invalido")

    com_9, sem_9 = variacoes(canonico)

    async with pool.connection() as conn:
        # Serializa o gate por lead. Sem isso, duas mensagens do mesmo telefone
        # em paralelo produzem lost update, UniqueViolation no INSERT e
        # ressurreição de followup_active durante handover. Mesmo idioma de
        # shared/queue.py:92 — lock de transação, não de sessão, então não
        # sofre com o reuso de conexões do pool.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (canonico,)
        )

        cur = await conn.execute(
            "select 1 from blocklist where phone = %s", (canonico,)
        )
        if await cur.fetchone():
            return ResultadoGate(False, "blocklist", canonico)

        cur = await conn.execute(
            "select * from leads_crm where phone in (%s, %s) "
            "order by last_interaction_at desc nulls last limit 1",
            (com_9, sem_9),
        )
        lead = await cur.fetchone()

        if key.get("fromMe") is True:
            if lead:
                await conn.execute(
                    "update leads_crm set agent_active = false, "
                    "followup_active = false, agent_reactivate_at = null "
                    "where phone = %s",
                    (lead["phone"],),
                )
            return ResultadoGate(False, "from_me", canonico)

        if lead and lead["agent_active"] is False:
            return ResultadoGate(False, "agente_desligado", canonico, lead)

        if lead:
            cur = await conn.execute(
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
            cur = await conn.execute(
                "insert into leads_crm (phone, name, source, phase) "
                "values (%s, nullif(%s, ''), 'whatsapp_direct', 'iniciou_conversa') "
                "returning *",
                (sem_9, push_name or ""),
            )

        atualizado = await cur.fetchone()

    return ResultadoGate(True, None, canonico, atualizado)
```

- [ ] **Step 4: Rodar os testes**

Run: `uv run pytest tests/integration/test_gate_ingestao.py -v`
Expected: PASS — 6 testes.

- [ ] **Step 5: Verificar lint e tipos**

Run: `make check`

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/shared/leads.py tests/integration/test_gate_ingestao.py
git commit -m "feat: gate de ingestão do SDR sobre leads_crm

Replica as regras do Add New Lead do n8n: blocklist, fromMe desligando o
agente, canonicalização do telefone e promoção de fase.

A checagem de agent_active vem antes do upsert, como no SQL original.
Se viesse depois, todo lead em handover teria o followup_count zerado a
cada mensagem recebida e receberia a escada de follow-up do zero ao ser
reativado. Há teste travando exatamente isso.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Canal Evolution nos modelos e settings

**Files:**
- Modify: `src/whatsapp_langchain/shared/models.py`
- Modify: `src/whatsapp_langchain/shared/config.py`
- Test: `tests/unit/test_config_evolution.py`

**Interfaces:**
- Produces: `MessagingChannel.EVOLUTION`; `settings.evolution_base_url`, `settings.evolution_api_key`, `settings.evolution_instance`.

- [ ] **Step 1: Escrever o teste que falha**

Arquivo `tests/unit/test_config_evolution.py`:

```python
"""Canal Evolution registrado no enum e nas settings."""

from whatsapp_langchain.shared.config import Settings
from whatsapp_langchain.shared.models import MessagingChannel


def test_enum_tem_evolution():
    assert MessagingChannel.EVOLUTION.value == "evolution"


def test_settings_tem_campos_da_evolution():
    s = Settings(
        evolution_base_url="https://evolution.exemplo.host",
        evolution_api_key="chave",
        evolution_instance="instancia-teste",
    )
    assert s.evolution_base_url == "https://evolution.exemplo.host"
    assert s.evolution_api_key == "chave"
    assert s.evolution_instance == "instancia-teste"


def test_settings_da_evolution_tem_default_vazio():
    s = Settings()
    assert s.evolution_base_url == ""
    assert s.evolution_api_key == ""
    assert s.evolution_instance == ""
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/unit/test_config_evolution.py -v`
Expected: FAIL — `AttributeError: EVOLUTION`

- [ ] **Step 3: Adicionar o valor do enum**

Em `src/whatsapp_langchain/shared/models.py`, dentro de `class MessagingChannel`,
logo após a linha `UAZAPI = "uazapi"`:

```python
    EVOLUTION = "evolution"
```

- [ ] **Step 4: Adicionar as settings**

Em `src/whatsapp_langchain/shared/config.py`, após o bloco de settings da uazapi:

```python
    # --- Evolution API (integração WHATSAPP-BUSINESS: Meta Cloud API por baixo) ---
    # A instância é fixa por deploy; o apikey autentica tanto o envio quanto o
    # download de mídia decifrada.
    evolution_base_url: str = ""
    evolution_api_key: str = ""
    evolution_instance: str = ""
```

- [ ] **Step 5: Rodar os testes**

Run: `uv run pytest tests/unit/test_config_evolution.py tests/unit/test_models.py tests/unit/test_config.py -v`
Expected: PASS — nenhum teste existente pode quebrar.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/shared/models.py src/whatsapp_langchain/shared/config.py tests/unit/test_config_evolution.py
git commit -m "feat: registra o canal evolution no enum e nas settings

message_queue.channel é TEXT sem CHECK, então o canal novo não exige
migração de coluna — só o valor no enum Python.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Cliente outbound da Evolution

**Files:**
- Create: `src/whatsapp_langchain/worker/evolution_client.py`
- Test: `tests/unit/test_evolution_client.py`

**Interfaces:**
- Consumes: `settings.evolution_*`.
- Produces:
  - `EvolutionSendError(status_code: int, detail: str)`
  - `EvolutionClient(base_url, api_key, instance, delivery_mode="real")`
  - `async send_message(to: str, body: str, token: str | None = None, delay_ms: int = 0) -> str | None`
  - `async send_typing(to: str, message_id: str | None = None, token: str | None = None) -> bool` — sempre `False`; a Evolution mostra "digitando" pelo próprio `delay` do envio. Retorna `bool` porque `processor._send_typing` é tipado assim.
  - `async baixar_midia(message_key: dict) -> bytes`

- [ ] **Step 1: Escrever os testes que falham**

Arquivo `tests/unit/test_evolution_client.py`:

```python
"""Testes do EvolutionClient com httpx mockado."""

import base64
import json

import httpx
import pytest

from whatsapp_langchain.worker.evolution_client import (
    EvolutionClient,
    EvolutionSendError,
)

BASE = "https://evolution.exemplo.host"
INSTANCIA = "instancia-teste"
CHAVE = "chave-secreta"


@pytest.fixture
def client():
    return EvolutionClient(
        base_url=BASE, api_key=CHAVE, instance=INSTANCIA, delivery_mode="real"
    )


async def test_envia_texto_com_numero_sem_mais(client, monkeypatch):
    capturado = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        capturado["url"] = str(request.url)
        capturado["apikey"] = request.headers.get("apikey")
        capturado["body"] = json.loads(request.content)
        return httpx.Response(201, json={"key": {"id": "MSG123"}})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    msg_id = await client.send_message("+551187654321", "Olá", delay_ms=1200)

    assert msg_id == "MSG123"
    assert capturado["url"] == f"{BASE}/message/sendText/{INSTANCIA}"
    assert capturado["apikey"] == CHAVE
    assert capturado["body"]["number"] == "551187654321"
    assert capturado["body"]["text"] == "Olá"
    assert capturado["body"]["delay"] == 1200


async def test_erro_http_vira_excecao(client, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))

    with pytest.raises(EvolutionSendError) as exc:
        await client.send_message("+551187654321", "Olá")

    assert exc.value.status_code == 401


async def test_modo_mock_nao_faz_requisicao():
    mock = EvolutionClient(
        base_url=BASE, api_key=CHAVE, instance=INSTANCIA, delivery_mode="mock"
    )
    assert await mock.send_message("+551187654321", "Olá") is None


async def test_baixa_midia_decifrada(client, monkeypatch):
    conteudo = b"\x00\x01audio"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith(
            f"/chat/getBase64FromMediaMessage/{INSTANCIA}"
        )
        return httpx.Response(
            200, json={"base64": base64.b64encode(conteudo).decode()}
        )

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))
    assert await client.baixar_midia({"id": "MSG123"}) == conteudo


async def test_send_typing_e_noop(client):
    """False, não None: processor._send_typing é tipado como bool."""
    assert await client.send_typing("+551187654321") is False
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/unit/test_evolution_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'whatsapp_langchain.worker.evolution_client'`

- [ ] **Step 3: Implementar o cliente**

Arquivo `src/whatsapp_langchain/worker/evolution_client.py`:

```python
"""Cliente outbound da Evolution API.

A instância desta conta roda integração WHATSAPP-BUSINESS — por baixo é a
Meta Cloud API oficial, com a Evolution fazendo de proxy. A superfície REST
é a mesma da integração Baileys.

O parâmetro `delay` do sendText é nativo e em milissegundos: ele mostra
"digitando…" durante a espera. Por isso send_typing é no-op — chamar um
endpoint de presença separado duplicaria o efeito.

Mídia recebida vem criptografada (herança do Baileys); baixá-la exige
getBase64FromMediaMessage, não um GET na URL.
"""

import base64
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

TIMEOUT = httpx.Timeout(30.0)


class EvolutionSendError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Evolution respondeu {status_code}: {detail}")


class EvolutionClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        instance: str,
        delivery_mode: str = "real",
    ):
        if delivery_mode == "real" and not (base_url and api_key and instance):
            raise ValueError(
                "base_url, api_key e instance são obrigatórios em modo real"
            )

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.instance = instance
        self.delivery_mode = delivery_mode
        self._transport: httpx.BaseTransport | None = None

    def _headers(self) -> dict[str, str]:
        return {"apikey": self.api_key, "Content-Type": "application/json"}

    async def send_message(
        self,
        to: str,
        body: str,
        token: str | None = None,
        delay_ms: int = 0,
    ) -> str | None:
        if self.delivery_mode == "mock":
            logger.info("evolution_mock_send", to=to, body=body[:80])
            return None

        payload: dict[str, Any] = {"number": to.lstrip("+"), "text": body}
        if delay_ms:
            payload["delay"] = delay_ms

        async with httpx.AsyncClient(
            transport=self._transport, timeout=TIMEOUT
        ) as client:
            response = await client.post(
                f"{self.base_url}/message/sendText/{self.instance}",
                headers=self._headers(),
                json=payload,
            )

        if response.status_code >= 400:
            raise EvolutionSendError(response.status_code, response.text[:300])

        dados = response.json()
        return (dados.get("key") or {}).get("id")

    async def send_typing(
        self,
        to: str,
        message_id: str | None = None,
        token: str | None = None,
    ) -> bool:
        return False

    async def baixar_midia(self, message_key: dict[str, Any]) -> bytes:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=TIMEOUT
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/getBase64FromMediaMessage/{self.instance}",
                headers=self._headers(),
                json={"message": {"key": message_key}, "convertToMp4": False},
            )

        if response.status_code >= 400:
            raise EvolutionSendError(response.status_code, response.text[:300])

        return base64.b64decode(response.json()["base64"])
```

- [ ] **Step 4: Rodar os testes**

Run: `uv run pytest tests/unit/test_evolution_client.py -v`
Expected: PASS — 5 testes.

- [ ] **Step 5: Verificar contra o servidor real**

Confirma que o formato do payload está certo antes de confiar nos mocks.
Substitua `<APIKEY>` e `<SEU_NUMERO>`:

```bash
curl -s -X POST \
  'https://evolution.ju39tu.easypanel.host/message/sendText/instancia-apioficial' \
  -H 'apikey: <APIKEY>' -H 'Content-Type: application/json' \
  -d '{"number":"<SEU_NUMERO>","text":"teste do harness","delay":1200}' | head -c 400
```

Expected: JSON com `key.id` e `status`. Mande para o **seu próprio** número.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/worker/evolution_client.py tests/unit/test_evolution_client.py
git commit -m "feat: cliente outbound da Evolution API

Envio via /message/sendText com o delay nativo em ms, que já exibe
'digitando…' — por isso send_typing é no-op, para não duplicar o efeito.

Inclui baixar_midia via getBase64FromMediaMessage: a URL que chega no
payload é mídia criptografada e um GET direto nela não serve.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Download de mídia por canal

**Files:**
- Modify: `src/whatsapp_langchain/worker/media.py:50-68`
- Test: `tests/unit/test_media_download_canal.py`

**Interfaces:**
- Consumes: `EvolutionClient.baixar_midia`.
- Produces: `download_media(url, canal=MessagingChannel.TWILIO, message_key=None) -> bytes`

- [ ] **Step 1: Escrever o teste que falha**

Arquivo `tests/unit/test_media_download_canal.py`:

```python
"""download_media decide o caminho pelo canal de origem."""

import pytest

from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.worker import media


async def test_evolution_usa_base64_e_ignora_url(monkeypatch):
    chamado = {}

    async def fake_baixar(self, message_key):
        chamado["key"] = message_key
        return b"conteudo-decifrado"

    monkeypatch.setattr(
        "whatsapp_langchain.worker.evolution_client.EvolutionClient.baixar_midia",
        fake_baixar,
    )
    monkeypatch.setattr(media.settings, "evolution_base_url", "https://e.host")
    monkeypatch.setattr(media.settings, "evolution_api_key", "chave")
    monkeypatch.setattr(media.settings, "evolution_instance", "inst")

    conteudo = await media.download_media(
        "https://mmg.whatsapp.net/algo.enc",
        canal=MessagingChannel.EVOLUTION,
        message_key={"id": "MSG1"},
    )

    assert conteudo == b"conteudo-decifrado"
    assert chamado["key"] == {"id": "MSG1"}


async def test_evolution_sem_message_key_falha():
    with pytest.raises(ValueError, match="message_key"):
        await media.download_media(
            "https://mmg.whatsapp.net/algo.enc",
            canal=MessagingChannel.EVOLUTION,
            message_key=None,
        )
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/unit/test_media_download_canal.py -v`
Expected: FAIL — `TypeError: download_media() got an unexpected keyword argument 'canal'`

- [ ] **Step 3: Substituir `download_media`**

Em `src/whatsapp_langchain/worker/media.py`, trocar a função inteira (linhas
50-68) por:

```python
async def download_media(
    url: str,
    canal: MessagingChannel | str = MessagingChannel.TWILIO,
    message_key: dict | None = None,
) -> bytes:
    """Baixa mídia usando o mecanismo do canal de origem.

    Twilio/Meta/uazapi entregam URL baixável com autenticação. A Evolution
    não: a URL do payload aponta para mídia criptografada, e o conteúdo
    decifrado só sai por getBase64FromMediaMessage.
    """
    if canal == MessagingChannel.EVOLUTION:
        if not message_key:
            raise ValueError("Evolution exige message_key para baixar mídia")

        from whatsapp_langchain.worker.evolution_client import EvolutionClient

        cliente = EvolutionClient(
            base_url=settings.evolution_base_url,
            api_key=settings.evolution_api_key,
            instance=settings.evolution_instance,
        )
        return await cliente.baixar_midia(message_key)

    auth = (
        (settings.twilio_api_key_sid, settings.twilio_api_key_secret)
        if settings.twilio_api_key_sid
        else None
    )

    async with httpx.AsyncClient() as client:
        response = await client.get(url, auth=auth, follow_redirects=True)
        response.raise_for_status()
        return response.content
```

Adicionar o import no topo do arquivo, junto aos demais:

```python
from whatsapp_langchain.shared.models import MessagingChannel
```

- [ ] **Step 4: Rodar os testes**

Run: `uv run pytest tests/unit/test_media_download_canal.py tests/unit/test_media_preprocess.py -v`
Expected: PASS — o teste antigo não pode quebrar, porque `canal` tem default.

- [ ] **Step 5: Commit**

```bash
git add src/whatsapp_langchain/worker/media.py tests/unit/test_media_download_canal.py
git commit -m "feat: download de mídia por canal

download_media autenticava só no Twilio. A Evolution entrega mídia
criptografada do Baileys, cuja URL não serve para GET direto — o
conteúdo decifrado vem de getBase64FromMediaMessage.

Sem isso, áudio e imagem falhariam em produção.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Webhook da Evolution

**Files:**
- Create: `src/whatsapp_langchain/server/routes/webhook_evolution.py`
- Modify: `src/whatsapp_langchain/server/main.py`
- Test: `tests/integration/test_webhook_evolution.py`

**Interfaces:**
- Consumes: `aplicar_gate`, `to_e164`, `enqueue_or_buffer`, `MessagingChannel.EVOLUTION`.
- Produces: rota `POST /webhook/evolution?agent=<id>`.

- [ ] **Step 1: Escrever os testes que falham**

Arquivo `tests/integration/test_webhook_evolution.py`:

```python
"""Webhook da Evolution: do payload até a fila."""

import pytest
from httpx import ASGITransport, AsyncClient

from whatsapp_langchain.server.main import app
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "551166665555"
AGENTE = "illumi_assistant"


def payload(texto="Olá", from_me=False, remote_jid="5511966665555@s.whatsapp.net"):
    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": "MSG1"},
            "pushName": "Fulano",
            "messageType": "conversation",
            "message": {"conversation": texto},
        },
    }


@pytest.fixture
async def limpar():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute(
            "delete from message_queue where phone_number = %s", (f"+{TELEFONE}",)
        )
    yield


async def cliente():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://teste")


async def test_mensagem_valida_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select channel, incoming_message from message_queue "
            "where phone_number = %s",
            (f"+{TELEFONE}",),
        )
        linha = await cur.fetchone()

    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


async def test_from_me_nao_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload(from_me=True)
        )

    assert r.json()["status"] == "ignorado"
    assert r.json()["motivo"] == "from_me"

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from message_queue where phone_number = %s",
            (f"+{TELEFONE}",),
        )
        assert (await cur.fetchone())[0] == 0


async def test_grupo_e_ignorado(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(remote_jid="12345-67890@g.us"),
        )

    assert r.json()["status"] == "ignorado"
    assert r.json()["motivo"] == "telefone_invalido"


async def test_evento_nao_mensagem_e_ignorado(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json={"event": "connection.update", "instance": "x", "data": {}},
        )

    assert r.status_code == 200
    assert r.json()["status"] == "ignorado"


async def test_agente_inexistente_da_erro():
    async with await cliente() as c:
        r = await c.post("/webhook/evolution?agent=nao_existe", json=payload())

    assert r.status_code in (404, 422)
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/integration/test_webhook_evolution.py -v`
Expected: FAIL — 404, a rota não existe.

- [ ] **Step 3: Implementar a rota**

Arquivo `src/whatsapp_langchain/server/routes/webhook_evolution.py`:

```python
"""Webhook da Evolution API (integração WHATSAPP-BUSINESS).

O payload chega no formato Baileys mesmo quando a integração é a Cloud API
oficial — a Evolution normaliza os dois casos:

    {
      "event": "messages.upsert",
      "instance": "instancia-apioficial",
      "data": {
        "key": {"remoteJid": "...@s.whatsapp.net", "remoteJidAlt": "...",
                "fromMe": false, "id": "..."},
        "pushName": "Fulano",
        "messageType": "conversation",
        "message": {"conversation": "texto"}
      }
    }

Diferente dos outros canais do harness, aqui o gate de ingestão roda ANTES
de enfileirar: `fromMe` é eco de mensagem enviada por humano e não pode
virar item de fila, e filtrar antes evita ocupar a fila com descarte.
"""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from whatsapp_langchain.agents.loader import AgentNotFoundError, list_agents
from whatsapp_langchain.server.dependencies import check_rate_limit
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.phone import to_e164
from whatsapp_langchain.shared.queue import enqueue_or_buffer

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

EVENTOS_DE_MENSAGEM = {"messages.upsert", "messages"}

MEDIA_TYPE_MAP: dict[str, str] = {
    "imagemessage": "image/jpeg",
    "stickermessage": "image/webp",
    "audiomessage": "audio/ogg",
    "pttmessage": "audio/ogg",
    "videomessage": "video/mp4",
    "documentmessage": "application/octet-stream",
}


def _extrair_conteudo(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Devolve (texto, media_url, media_type) do payload da Evolution."""
    msg = data.get("message") or {}
    tipo = (data.get("messageType") or "").strip().lower()

    texto = msg.get("conversation") or (
        (msg.get("extendedTextMessage") or {}).get("text") or ""
    )

    if tipo in ("conversation", "extendedtextmessage", "text") or not tipo:
        return texto, None, None

    media_type = MEDIA_TYPE_MAP.get(tipo)
    url = None
    for campo in ("audioMessage", "imageMessage", "videoMessage", "documentMessage"):
        if isinstance(msg.get(campo), dict):
            url = msg[campo].get("url")
            texto = texto or msg[campo].get("caption") or ""
            break

    return texto, url, media_type


@router.post("/webhook/evolution")
async def webhook_evolution_receive(
    request: Request,
    agent: str = Query(description="ID do agente para processar a mensagem"),
) -> dict[str, Any]:
    if agent not in list_agents():
        raise AgentNotFoundError(agent)

    try:
        payload = await request.json()
    except Exception:
        logger.warning("evolution_json_invalido")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    evento = (payload.get("event") or "").strip().lower()
    if evento not in EVENTOS_DE_MENSAGEM:
        logger.debug("evolution_evento_ignorado", evento=evento or None)
        return {"status": "ignorado", "motivo": "evento_nao_tratado"}

    data = payload.get("data") or {}
    key = data.get("key") or {}

    pool = await get_pool()
    resultado = await aplicar_gate(pool, key, push_name=data.get("pushName"))

    if not resultado.aceito:
        logger.info(
            "evolution_descartado",
            motivo=resultado.motivo,
            phone=resultado.canonico,
        )
        return {"status": "ignorado", "motivo": resultado.motivo}

    phone_e164 = to_e164(resultado.canonico)
    texto, media_url, media_type = _extrair_conteudo(data)

    try:
        await check_rate_limit(phone_e164)
    except HTTPException:
        logger.warning("evolution_rate_limit", phone=phone_e164)
        return {"status": "ignorado", "motivo": "rate_limit"}

    enfileirado = await enqueue_or_buffer(
        pool=pool,
        phone_number=phone_e164,
        agent_id=agent,
        body=texto,
        channel=MessagingChannel.EVOLUTION,
        media_url=media_url,
        media_type=media_type,
        message_id=key.get("id"),
        buffer_seconds=settings.message_buffer_seconds,
    )

    logger.info(
        "webhook_evolution_recebido",
        phone=phone_e164,
        agent_id=agent,
        instance=payload.get("instance"),
        queue_id=enfileirado.message_id,
        buffered=enfileirado.is_buffered,
        phase=resultado.lead.get("phase") if resultado.lead else None,
    )

    return {"status": "ok", "queue_id": enfileirado.message_id}
```

- [ ] **Step 4: Registrar a rota**

Em `src/whatsapp_langchain/server/main.py`, junto aos outros `include_router`,
seguindo o padrão já usado para `webhook_uazapi`:

```python
from whatsapp_langchain.server.routes import webhook_evolution

app.include_router(webhook_evolution.router)
```

- [ ] **Step 5: Rodar os testes**

Run: `uv run pytest tests/integration/test_webhook_evolution.py -v`
Expected: PASS — 5 testes.

- [ ] **Step 6: Rodar a suíte inteira**

Run: `make ci`
Expected: tudo verde. Nenhum teste pré-existente pode quebrar.

- [ ] **Step 7: Commit**

```bash
git add src/whatsapp_langchain/server/routes/webhook_evolution.py \
        src/whatsapp_langchain/server/main.py \
        tests/integration/test_webhook_evolution.py
git commit -m "feat: webhook inbound da Evolution API

Recebe o payload em formato Baileys, aplica o gate de ingestão antes de
enfileirar e grava channel='evolution'.

O gate roda na API e não no worker de propósito: fromMe é eco de mensagem
mandada por humano e não pode virar item de fila.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Roteamento outbound no processor

**Files:**
- Modify: `src/whatsapp_langchain/worker/processor.py:90-125`
- Test: `tests/unit/test_processor_evolution.py`

**Interfaces:**
- Consumes: `EvolutionClient`, `MessagingChannel.EVOLUTION`.
- Produces: `OutboundClient` incluindo `EvolutionClient`; `_select_client`
  devolvendo o cliente certo para `message.channel == EVOLUTION`.

**Atenção às assinaturas reais** (`processor.py:61` e `:90`), que não são óbvias:
- `_normalize_outbounds(outbounds, outbound, twilio)` — **três** parâmetros posicionais.
- `_select_client(outbounds, message)` — recebe um **`MessageQueue`**, não um canal.
- `_send_typing` é tipado `-> bool`.

- [ ] **Step 1: Escrever o teste que falha**

Arquivo `tests/unit/test_processor_evolution.py`:

```python
"""O processor escolhe o cliente Evolution para mensagens do canal."""

from datetime import UTC, datetime

import pytest

from whatsapp_langchain.shared.models import MessageQueue, MessagingChannel
from whatsapp_langchain.worker.evolution_client import EvolutionClient
from whatsapp_langchain.worker.processor import _normalize_outbounds, _select_client


@pytest.fixture
def evolution():
    return EvolutionClient(
        base_url="https://e.host",
        api_key="chave",
        instance="inst",
        delivery_mode="mock",
    )


def mensagem(channel: MessagingChannel) -> MessageQueue:
    return MessageQueue(
        id=1,
        phone_number="+551187654321",
        agent_id="illumi_assistant",
        thread_id="+551187654321:illumi_assistant",
        incoming_message="oi",
        channel=channel,
        status="queued",
        attempts=0,
        created_at=datetime.now(UTC),
    )


def test_seleciona_cliente_evolution(evolution):
    clientes = _normalize_outbounds(
        {MessagingChannel.EVOLUTION: evolution}, None, None
    )
    assert _select_client(clientes, mensagem(MessagingChannel.EVOLUTION)) is evolution


def test_canal_nao_habilitado_da_erro_claro(evolution):
    clientes = _normalize_outbounds(
        {MessagingChannel.EVOLUTION: evolution}, None, None
    )
    with pytest.raises(ValueError, match="não está habilitado"):
        _select_client(clientes, mensagem(MessagingChannel.TWILIO))


def test_cliente_legado_evolution_e_reconhecido(evolution):
    """Path legado: cliente único sem dict não pode cair no default Twilio."""
    clientes = _normalize_outbounds(None, evolution, None)
    assert clientes == {MessagingChannel.EVOLUTION: evolution}
```

> Se `MessageQueue` exigir campos além dos usados acima, rode
> `uv run python -c "from whatsapp_langchain.shared.models import MessageQueue; print(MessageQueue.model_fields.keys())"`
> e complete apenas os obrigatórios.

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/unit/test_processor_evolution.py -v`
Expected: FAIL — `ImportError` ou o canal caindo no default Twilio.

- [ ] **Step 3: Incluir EvolutionClient no tipo e no path legado**

Em `src/whatsapp_langchain/worker/processor.py`, adicionar o import junto aos
outros clientes e estender a união de tipos (linha 57):

```python
from whatsapp_langchain.worker.evolution_client import EvolutionClient

OutboundClient = TwilioClient | MetaClient | UazapiClient | EvolutionClient
```

Dentro de `_normalize_outbounds`, no bloco `if outbound is not None:`, antes do
`isinstance(outbound, MetaClient)`:

```python
        if isinstance(outbound, EvolutionClient):
            return {MessagingChannel.EVOLUTION: outbound}
```

`_select_client` **não muda** — ele já resolve por `outbounds.get(message.channel)`,
que funciona para qualquer canal presente no dict.

- [ ] **Step 4: Instanciar o cliente no worker**

Em `src/whatsapp_langchain/worker/main.py`, onde os clientes por canal são
montados, adicionar o bloco da Evolution seguindo o mesmo critério de "canal
habilitado" (todas as credenciais preenchidas) usado para os demais:

```python
    if (
        settings.evolution_base_url
        and settings.evolution_api_key
        and settings.evolution_instance
    ):
        outbounds[MessagingChannel.EVOLUTION] = EvolutionClient(
            base_url=settings.evolution_base_url,
            api_key=settings.evolution_api_key,
            instance=settings.evolution_instance,
            delivery_mode=settings.outbound_mode or "mock",
        )
```

- [ ] **Step 5: Rodar os testes**

Run: `uv run pytest tests/unit/test_processor_evolution.py tests/unit/test_processor_twilio.py -v`
Expected: PASS.

- [ ] **Step 6: Rodar a suíte e commitar**

```bash
make ci
git add src/whatsapp_langchain/worker/processor.py \
        src/whatsapp_langchain/worker/main.py \
        tests/unit/test_processor_evolution.py
git commit -m "feat: roteia respostas pelo cliente Evolution

O worker mantém um cliente por canal habilitado e seleciona pelo campo
channel da mensagem. Evolution entra no mesmo mecanismo dos demais.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Teste ponta a ponta e documentação

**Files:**
- Create: `docs/EVOLUTION.md`
- Modify: `.env.example`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Adicionar as variáveis ao `.env.example`**

```bash
# --- Evolution API (WHATSAPP-BUSINESS: Meta Cloud API por baixo) ---
EVOLUTION_BASE_URL=https://evolution.ju39tu.easypanel.host
EVOLUTION_API_KEY=
EVOLUTION_INSTANCE=instancia-apioficial
```

- [ ] **Step 2: Subir o ambiente e fazer o teste manual**

```bash
make up
curl -s -X POST 'http://localhost:8000/webhook/evolution?agent=illumi_assistant' \
  -H 'Content-Type: application/json' \
  -d '{"event":"messages.upsert","instance":"instancia-apioficial",
       "data":{"key":{"remoteJid":"5511966665555@s.whatsapp.net",
                      "fromMe":false,"id":"MSG_TESTE"},
               "pushName":"Teste","messageType":"conversation",
               "message":{"conversation":"oi"}}}'
```

Expected: `{"status":"ok","queue_id":<n>}`

- [ ] **Step 3: Conferir o efeito no banco**

```bash
docker compose exec db psql -U postgres -d whatsapp_langchain -c \
  "select phone, phase, followup_count from leads_crm where phone='551166665555';
   select channel, incoming_message, status from message_queue
   where phone_number='+551166665555';"
```

Expected: o lead existe em `iniciou_conversa`, e a mensagem está na fila com
`channel = 'evolution'`.

- [ ] **Step 4: Escrever `docs/EVOLUTION.md`**

Documento curto cobrindo: a instância e a integração `WHATSAPP-BUSINESS`, o
formato do payload inbound, a ordem das regras do gate, as três variáveis de
ambiente, o motivo de `send_typing` ser no-op, e o caminho de download de mídia.
Seguir o tom de `docs/UAZAPI.md`.

- [ ] **Step 5: Registrar o canal no `CLAUDE.md`**

Na tabela da seção **Stack**, na linha "Inbound/Outbound WhatsApp", acrescentar
a Evolution aos canais suportados. Na convenção 11, incluir `/webhook/evolution`
na lista de webhooks que gravam `message_queue.channel`.

- [ ] **Step 6: Commit final da fase**

```bash
make ci
git add docs/EVOLUTION.md .env.example CLAUDE.md
git commit -m "docs: documenta o canal Evolution

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push origin main
```

---

## Definição de pronto da Fase 1

- [ ] `remoteJidAlt` confirmado ou o resolver ajustado com base em payload real
- [ ] `make ci` verde, sem regressão nos testes pré-existentes
- [ ] Uma mensagem entra por `/webhook/evolution` e chega na fila com `channel='evolution'`
- [ ] O lead é criado/atualizado em `leads_crm` com telefone canônico
- [ ] `fromMe`, blocklist, telefone inválido e `agent_active=false` descartam corretamente
- [ ] Lead pausado **não** tem `followup_count` zerado
- [ ] O worker responde pelo `EvolutionClient`
- [ ] Áudio e imagem baixam por `getBase64FromMediaMessage`

## O que vem depois

| Fase | Conteúdo | Plano |
|---|---|---|
| 2 | Agente `elevec_sdr`: prompt portado, saída em balões, tools de calendário/CRM/handover | a escrever |
| 3 | Follow-up com reivindicação atômica, webhook do ChatWoot | a escrever |
| 4 | `migrar_supabase.py`: normalização, fusão de duplicatas, histórico | a escrever |
| 5 | Cutover e deploy no Railway | a escrever |

Cada fase vira um plano próprio, escrito quando a anterior estiver em pé — as
decisões de detalhe ficam melhores com o código real na frente.
