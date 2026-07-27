# Fase 3 — Periféricos do `elevec_sdr`: follow-up, template e ChatWoot

> **Para agentes:** SUB-SKILL OBRIGATÓRIA: use `superpowers:subagent-driven-development`
> para implementar task a task. Os passos usam checkbox (`- [ ]`).

**Goal:** Dar à Renata as três peças que faltam para operar sozinha — a régua de
follow-up com reivindicação atômica, o `sendTemplate` que abre a janela de 24h, e
o webhook do ChatWoot que deixa um humano pausá-la.

**Architecture:** O follow-up é uma task asyncio no Worker, ao lado do loop de
consumo. Ela reivindica leads com um único `UPDATE ... RETURNING` e só então
envia, fora de qualquer transação. O ChatWoot entra como mais uma rota de webhook
na API, com o mesmo padrão de secret do Evolution. O `sendTemplate` é um método
novo no `EvolutionClient`.

**Tech Stack:** psycopg async pool, asyncio, FastAPI, httpx.

---

## Global Constraints

- Python 3.11+, deps por `uv` — nunca `pip install`.
- Português brasileiro no código, nos logs e nos commits (Conventional Commits).
- Banco de desenvolvimento na **porta 5440**. Não rodar `make up`.
- **Não usar `make check`** (37 `E501` pré-existentes em `prompts.py`). Rodar
  `ruff check`, `ruff format --check` e `pyright` só nos arquivos tocados.
- **Não fazer `git push`.** O orquestrador empurra ao fim da fase.
- Nunca `git add -A` nem `git add .` — sempre caminhos explícitos.
- Nenhum segredo versionado.
- Migrações **nunca são editadas depois de aplicadas** — o runner pula por nome
  de arquivo. Correção vira migração nova. (Este projeto já quebrou por isso.)
- Toda tool/função que devolve texto ao modelo usa o marcador de
  `agents/catalog/elevec_sdr/interno.py`. Nada desta fase fala com o modelo, mas
  se falar, marque.
- Camada de banco **testada contra o Postgres real**, nunca monkeypatchada. Este
  projeto reprovou três tasks por isso: substituir o SQL por no-op tem que
  derrubar testes.
- Suíte de partida: **662 passed, 13 skipped, 11 deselected**.

---

## O achado que redesenha a escada

A especificação (`docs/superpowers/specs/2026-07-25-elevec-sdr-migracao-n8n-design.md`,
seção "Follow-up") descreve três níveis: 15 minutos, 1 hora, 23 horas — cada um
contado a partir de `last_interaction_at`, que o próprio envio atualiza.

A integração é a **Cloud API oficial**. Texto livre só alcança quem escreveu nas
últimas 24 horas. E a escada acumula:

| Nível | Dispara em (desde a última mensagem **do lead**) | Janela |
|---|---|---|
| 1 | T+0h15 | aberta |
| 2 | T+1h15 | aberta |
| 3 | **T+24h15** | **fechada há 15 min** |

O nível 3 **nunca é entregue**. Ele é rejeitado pela Meta, não atrasado. Isso vale
para a produção em n8n hoje — a mensagem sai do workflow, o contador sobe, e a
Meta recusa.

Esta fase resolve com três mudanças:

1. **`last_inbound_at`**, coluna nova, escrita **só** pelo gate de ingestão.
   `last_interaction_at` mistura "o lead falou" com "nós falamos" e por isso não
   serve para calcular janela.
2. **O predicado de janela entra na cláusula de reivindicação.** Lead fora da
   janela não é reivindicado, então **não queima nível** — ele continua elegível
   se voltar a falar. Um `UPDATE` que reivindicasse e depois descartasse gastaria
   o degrau à toa.
3. **Os três degraus viram configuração**, com o nível 3 em **22 horas** por
   padrão: dispara em T+23h15, dentro da janela.

> **Divergência consciente do n8n, registrada.** Lá o terceiro degrau é 23h.
> Manter 23h aqui seria paridade com um comportamento que não entrega. Com o
> predicado de janela no lugar, configurar 23h faz o lead simplesmente não ser
> reivindicado — o sistema se recusa a mandar o que a Meta rejeitaria, e diz isso
> no log em vez de falhar em silêncio.

**O que esta fase não resolve:** reabrir a janela de quem passou de 24h exige um
template aprovado pela Meta para follow-up, e os dois templates que existem são de
boas-vindas (`boas_vindas_renata_linkedin_02`, `boas_vindas_renata_respondiapp_03`).
Mandar "boas-vindas" para alguém no meio de uma conversa é pior que não mandar.
`send_template` fica implementado e testado — o cutover da Fase 5 precisa dele —
mas o follow-up **não** o usa. É decisão do cliente aprovar um template de
retomada.

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `db/migrations/013_last_inbound_at.sql` | coluna `last_inbound_at` + backfill + índice |
| `src/whatsapp_langchain/shared/leads.py` (modificar) | gate passa a gravar `last_inbound_at` |
| `src/whatsapp_langchain/worker/evolution_client.py` (modificar) | `send_template` |
| `src/whatsapp_langchain/worker/followup.py` (criar) | reivindicação, escada, envio |
| `src/whatsapp_langchain/worker/main.py` (modificar) | sobe a task de follow-up |
| `src/whatsapp_langchain/server/routes/webhook_chatwoot.py` (criar) | pausa/retoma pelo rótulo |
| `src/whatsapp_langchain/shared/config.py` (modificar) | variáveis novas + fail-fast |
| `tests/integration/test_followup.py` (criar) | escada, concorrência, janela |
| `tests/integration/test_webhook_chatwoot.py` (criar) | rótulo, secret, filtro de evento |
| `tests/unit/test_evolution_template.py` (criar) | payload do `sendTemplate` |

---

## Task 1: `last_inbound_at` — o relógio da janela

Hoje só existe `last_interaction_at`, escrito pelo gate (`leads.py:376`). Assim
que o follow-up também escrever nele, ele deixa de responder "quando o lead falou
pela última vez", que é a única pergunta que decide se texto livre é entregue.

**Files:**
- Create: `db/migrations/013_last_inbound_at.sql`
- Modify: `src/whatsapp_langchain/shared/leads.py:376`
- Test: `tests/integration/test_gate_ingestao.py` (existente — acrescentar)

**Interfaces:**
- Produces: coluna `leads_crm.last_inbound_at TIMESTAMPTZ`, escrita **apenas** por
  `aplicar_gate`. Consumida pela Task 3.

- [ ] **Step 1: Escrever o teste que falha**

Em `tests/integration/test_gate_ingestao.py`:

```python
async def test_gate_grava_last_inbound_at(pool, lead_factory):
    """Só o inbound do lead move last_inbound_at — é o relógio da janela."""
    await lead_factory(phone="5511987654321", phase="iniciou_conversa")

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "update leads_crm set last_inbound_at = now() - interval '3 hours' "
            "where phone = %s",
            ("5511987654321",),
        )
        await conn.commit()

    resultado = await aplicar_gate(
        pool,
        key={"remoteJid": "5511987654321@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )
    assert resultado.aceito

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "select now() - last_inbound_at < interval '10 seconds' "
            "from leads_crm where phone = %s",
            ("5511987654321",),
        )
        (recente,) = await cur.fetchone()
    assert recente, "o gate precisa renovar last_inbound_at no inbound aceito"
```

- [ ] **Step 2: Rodar e ver falhar**

`uv run pytest tests/integration/test_gate_ingestao.py::test_gate_grava_last_inbound_at -v`
Esperado: FAIL — `column "last_inbound_at" does not exist`.

- [ ] **Step 3: Criar a migração**

`db/migrations/013_last_inbound_at.sql`:

```sql
-- 013_last_inbound_at.sql
-- Relógio da janela de 24h da Cloud API.
--
-- last_interaction_at mistura "o lead falou" com "nós falamos" — o follow-up
-- também o atualiza. Para decidir se texto livre ainda é entregue, só serve o
-- inbound do lead. Esta coluna é escrita EXCLUSIVAMENTE pelo gate de ingestão.

ALTER TABLE leads_crm ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;

-- Backfill: para quem já existe, last_interaction_at é a melhor aproximação
-- disponível. Superestima a janela de quem recebeu follow-up (o valor é do
-- nosso envio, não do inbound dele) — aceitável, porque o pior caso é uma
-- mensagem rejeitada pela Meta, que é exatamente o que acontece hoje.
UPDATE leads_crm SET last_inbound_at = last_interaction_at
WHERE last_inbound_at IS NULL;

-- Índice do follow-up: reivindicação filtra por followup_active + agent_active
-- (parcial), ordena por last_interaction_at e corta pela janela.
DROP INDEX IF EXISTS idx_leads_followup;
CREATE INDEX IF NOT EXISTS idx_leads_followup
    ON leads_crm (last_interaction_at, phase, followup_count, last_inbound_at)
    WHERE followup_active AND agent_active;
```

- [ ] **Step 4: Fazer o gate gravar a coluna**

Em `leads.py`, no `UPDATE` do upsert (linha ~376), acrescentar ao `SET`:

```python
"  last_inbound_at = now(),"
```

Na mesma altura de `last_interaction_at = now(),`. E no `INSERT` do lead novo,
incluir `last_inbound_at` com `now()`.

- [ ] **Step 5: Rodar e ver passar**

`uv run pytest tests/integration/test_gate_ingestao.py -v` → todos passam.

- [ ] **Step 6: Provar por mutação**

Remova `last_inbound_at = now(),` do `UPDATE`. O teste do Step 1 tem que
falhar. Restaure. Registre no relatório.

- [ ] **Step 7: Commit**

```bash
git add db/migrations/013_last_inbound_at.sql \
        src/whatsapp_langchain/shared/leads.py \
        tests/integration/test_gate_ingestao.py
git commit -m "feat: last_inbound_at, o relogio da janela de 24h"
```

---

## Task 2: `send_template` no `EvolutionClient`

**Files:**
- Modify: `src/whatsapp_langchain/worker/evolution_client.py`
- Test: `tests/unit/test_evolution_template.py`

**Interfaces:**
- Produces:
  ```python
  async def send_template(
      self, to: str, template: str, parametro_header: str | None = None,
      language: str = "pt_BR",
  ) -> str | None
  ```
  Devolve o id da mensagem, ou `None` em 2xx sem id reconhecível — mesma
  precedência de `send_message`. Levanta `EvolutionSendError` em não-2xx.

**Contexto para quem implementa:** o endpoint é
`POST {base_url}/message/sendTemplate/{instance}`, com o header `apikey` (o mesmo
`_headers()` que `send_message` já usa). O corpo:

```json
{ "number": "<telefone>", "language": "pt_BR",
  "name": "boas_vindas_renata_respondiapp_03",
  "components": [{"type": "header",
                  "parameters": [{"type": "text", "text": "<primeiro nome>"}]}] }
```

Quando `parametro_header` é `None`, `components` sai como lista vazia — template
sem variável. **Não invente um valor padrão**: mandar "Olá, {{1}}" com o
placeholder literal é pior que mandar sem.

- [ ] **Step 1: Escrever os testes que falham**

`tests/unit/test_evolution_template.py`:

```python
import httpx
import pytest

from whatsapp_langchain.worker.evolution_client import (
    EvolutionClient,
    EvolutionSendError,
)


def _cliente(handler) -> EvolutionClient:
    cliente = EvolutionClient(
        base_url="https://evo.exemplo",
        api_key="chave",
        instance="inst",
        delivery_mode="real",
    )
    cliente._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return cliente


async def test_payload_do_template_com_parametro():
    capturado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["url"] = str(request.url)
        capturado["json"] = __import__("json").loads(request.content)
        capturado["apikey"] = request.headers.get("apikey")
        return httpx.Response(200, json={"key": {"id": "wamid.ABC"}})

    cliente = _cliente(handler)
    msg_id = await cliente.send_template(
        to="5511987654321",
        template="boas_vindas_renata_respondiapp_03",
        parametro_header="Fulano",
    )

    assert msg_id == "wamid.ABC"
    assert capturado["url"] == "https://evo.exemplo/message/sendTemplate/inst"
    assert capturado["apikey"] == "chave"
    assert capturado["json"] == {
        "number": "5511987654321",
        "language": "pt_BR",
        "name": "boas_vindas_renata_respondiapp_03",
        "components": [
            {"type": "header", "parameters": [{"type": "text", "text": "Fulano"}]}
        ],
    }


async def test_template_sem_parametro_nao_inventa_placeholder():
    capturado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["json"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"key": {"id": "wamid.X"}})

    cliente = _cliente(handler)
    await cliente.send_template(to="5511987654321", template="t")
    assert capturado["json"]["components"] == []


async def test_template_em_modo_mock_nao_chama_a_rede():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("modo mock não pode tocar a rede")

    cliente = _cliente(handler)
    cliente.delivery_mode = "mock"
    assert await cliente.send_template(to="551199", template="t") is None


async def test_erro_da_meta_vira_excecao():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "template not found"})

    cliente = _cliente(handler)
    with pytest.raises(EvolutionSendError) as exc:
        await cliente.send_template(to="551199", template="inexistente")
    assert exc.value.status_code == 400
```

- [ ] **Step 2: Rodar e ver falhar**

`uv run pytest tests/unit/test_evolution_template.py -v` → `AttributeError: send_template`.

- [ ] **Step 3: Implementar**

Em `evolution_client.py`, ao lado de `send_message`. Reaproveite `_headers()`,
`_safe_json()` e `_extract_message_id()` — não duplique. O docstring deve dizer
por que este método existe: **texto livre não alcança quem não escreveu nas
últimas 24h**, e é o template que abre a conversa.

- [ ] **Step 4: Rodar e ver passar**

- [ ] **Step 5: Provar por mutação**

Pelo menos quatro, cada uma tem que matar teste: (a) trocar `sendTemplate` por
`sendText` na URL; (b) omitir `language`; (c) mandar `components` preenchido
quando `parametro_header` é `None`; (d) engolir o não-2xx devolvendo `None` em vez
de levantar.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/worker/evolution_client.py \
        tests/unit/test_evolution_template.py
git commit -m "feat: sendTemplate no EvolutionClient para abrir a janela de 24h"
```

---

## Task 3: Reivindicação atômica e a escada

O coração da fase. **Um envio indevido aqui é WhatsApp para gente de verdade.**

**Files:**
- Create: `src/whatsapp_langchain/worker/followup.py`
- Modify: `src/whatsapp_langchain/shared/config.py`
- Test: `tests/integration/test_followup.py`

**Interfaces:**
- Consumes: `leads_crm.last_inbound_at` (Task 1).
- Produces:
  ```python
  @dataclass(frozen=True)
  class LeadReivindicado:
      phone: str
      name: str | None
      nivel: int          # 1, 2 ou 3 — já é o novo followup_count

  async def reivindicar(pool, *, limite: int) -> list[LeadReivindicado]
  def montar_mensagem(nivel: int, name: str | None) -> str
  async def reativar_agentes(pool) -> int
  async def rodada(pool, cliente, *, limite: int) -> dict[str, int]
  ```

**Config nova** (`config.py`), com fail-fast no mesmo grupo do SDR:

| Variável | Default | Papel |
|---|---|---|
| `FOLLOWUP_ENABLED` | `false` | **default desligado** — subir o worker não pode começar a disparar |
| `FOLLOWUP_INTERVAL_SECONDS` | `300` | intervalo da task |
| `FOLLOWUP_BATCH_SIZE` | `10` | `LIMIT` da reivindicação |
| `FOLLOWUP_NIVEL1_MINUTOS` | `15` | degrau 1 |
| `FOLLOWUP_NIVEL2_HORAS` | `1` | degrau 2 |
| `FOLLOWUP_NIVEL3_HORAS` | `22` | degrau 3 — **22, não 23**; ver seção do achado |
| `FOLLOWUP_JANELA_MARGEM_MINUTOS` | `30` | folga antes das 24h |

- [ ] **Step 1: Escrever os testes que falham**

`tests/integration/test_followup.py` — contra o Postgres real, sem monkeypatch
da camada de banco:

```python
async def test_os_tres_niveis_disparam_no_tempo_certo(pool, lead_factory):
    """Cada degrau é reivindicado só depois do seu intervalo."""
    await lead_factory(phone="5511900000001", phase="iniciou_conversa",
                       followup_count=0, minutos_desde_interacao=10)
    await lead_factory(phone="5511900000002", phase="iniciou_conversa",
                       followup_count=0, minutos_desde_interacao=20)
    await lead_factory(phone="5511900000003", phase="iniciou_conversa",
                       followup_count=1, minutos_desde_interacao=90)
    await lead_factory(phone="5511900000004", phase="iniciou_conversa",
                       followup_count=2, minutos_desde_interacao=23 * 60)

    reivindicados = await reivindicar(pool, limite=10)
    por_telefone = {r.phone: r.nivel for r in reivindicados}

    assert "5511900000001" not in por_telefone, "10 min ainda não vence o degrau 1"
    assert por_telefone["5511900000002"] == 1
    assert por_telefone["5511900000003"] == 2
    assert por_telefone["5511900000004"] == 3


async def test_fase_terminal_nunca_e_perseguida(pool, lead_factory):
    """agendou_sessao, qualificado, desqualificado e perdido saem da régua."""
    for i, fase in enumerate(
        ["agendou_sessao", "qualificado", "desqualificado", "perdido"]
    ):
        await lead_factory(phone=f"551190000001{i}", phase=fase,
                           followup_count=0, minutos_desde_interacao=60)

    assert await reivindicar(pool, limite=10) == []


async def test_janela_fechada_nao_reivindica_e_nao_queima_nivel(pool, lead_factory):
    """Fora das 24h a Meta rejeita texto livre — não gastar o degrau."""
    await lead_factory(
        phone="5511900000020", phase="iniciou_conversa", followup_count=2,
        minutos_desde_interacao=23 * 60,      # o degrau venceu
        minutos_desde_inbound=25 * 60,        # mas a janela fechou
    )

    assert await reivindicar(pool, limite=10) == []

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "select followup_count from leads_crm where phone = %s",
            ("5511900000020",),
        )
        (contador,) = await cur.fetchone()
    assert contador == 2, "lead fora da janela não pode perder um degrau"


async def test_duas_rodadas_concorrentes_nunca_pegam_o_mesmo_lead(pool, lead_factory):
    for i in range(6):
        await lead_factory(phone=f"55119000001{i:02d}", phase="iniciou_conversa",
                           followup_count=0, minutos_desde_interacao=30)

    a, b = await asyncio.gather(
        reivindicar(pool, limite=10), reivindicar(pool, limite=10)
    )
    telefones = [r.phone for r in a] + [r.phone for r in b]
    assert len(telefones) == len(set(telefones)), "o mesmo lead foi reivindicado 2x"
    assert len(telefones) == 6


async def test_contador_sobe_antes_do_envio(pool, lead_factory):
    """Divergência consciente do n8n: falha de envio pula um nível, e é o certo.

    Mandar a mesma mensagem duas vezes para um lead é pior que perder um
    follow-up.
    """
    await lead_factory(phone="5511900000030", phase="iniciou_conversa",
                       followup_count=0, minutos_desde_interacao=30)

    class ClienteQueFalha:
        async def send_message(self, to, body, **kwargs):
            raise EvolutionSendError(500, "boom")

    resumo = await rodada(pool, ClienteQueFalha(), limite=10)
    assert resumo["falhas"] == 1

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "select followup_count from leads_crm where phone = %s",
            ("5511900000030",),
        )
        (contador,) = await cur.fetchone()
    assert contador == 1


async def test_lead_pausado_por_humano_nunca_e_perseguido(pool, lead_factory):
    await lead_factory(phone="5511900000040", phase="iniciou_conversa",
                       followup_count=0, minutos_desde_interacao=60,
                       agent_active=False)
    assert await reivindicar(pool, limite=10) == []
```

E os testes da mensagem, que não precisam de banco:

```python
def test_nivel_1_usa_o_primeiro_nome():
    assert montar_mensagem(1, "Fulano de Tal") == "Fulano?"


def test_nivel_1_sem_nome_vira_oi():
    assert montar_mensagem(1, None) == "Oi?"


def test_nivel_3_sem_nome_perde_o_vocativo_sem_deixar_virgula_solta():
    texto = montar_mensagem(3, None)
    assert not texto.startswith(","), texto
    assert "None" not in texto
```

- [ ] **Step 2: Rodar e ver falhar**

`uv run pytest tests/integration/test_followup.py -v`

- [ ] **Step 3: Implementar `reivindicar`**

O `UPDATE` único, com a janela **dentro** do predicado:

```python
_SQL_REIVINDICAR = """
update leads_crm
set followup_count = followup_count + 1,
    last_interaction_at = now()
where phone in (
    select phone from leads_crm
    where followup_active
      and agent_active
      and phase not in ('agendou_sessao','desqualificado','perdido','qualificado')
      and (
            (followup_count = 0
             and last_interaction_at < now() - make_interval(mins => %(n1)s))
         or (followup_count = 1
             and last_interaction_at < now() - make_interval(hours => %(n2)s))
         or (followup_count = 2
             and last_interaction_at < now() - make_interval(hours => %(n3)s))
      )
      and last_inbound_at > now() - make_interval(mins => %(janela)s)
    order by last_interaction_at
    limit %(limite)s
    for update skip locked
)
returning phone, name, followup_count
"""
```

`janela` = `24 * 60 - FOLLOWUP_JANELA_MARGEM_MINUTOS`, calculado em Python.

Três coisas que o implementador precisa entender, e que o docstring deve dizer:

- **Nenhuma transação fica aberta durante HTTP.** A reivindicação commita, e só
  então o envio acontece. Segurar `FOR UPDATE` enquanto se espera a Evolution
  responder é o defeito que este desenho evita.
- **Sem advisory lock.** O lock do Postgres é por sessão; com pool, a conexão
  volta segurando o lock e some sem aviso se for reciclada.
- **`followup_count` do `RETURNING` já é o novo valor** (1, 2 ou 3), que é
  exatamente o nível da mensagem.

- [ ] **Step 4: Implementar `montar_mensagem`**

```python
_NIVEL_2 = (
    "Opa, imagino que esteja corrido ai! Só para não perdermos o timing da "
    "sua aplicação, consegue falar agora?"
)
```

Nível 1: `f"{primeiro_nome}?"`, ou `"Oi?"` sem nome.
Nível 3: `f"{primeiro_nome}, tudo bem? Ainda faz sentido falarmos sobre o seu momento de carreira?"`;
sem nome, começa em `"Tudo bem?"` — **sem vírgula órfã**.

Use `sanitizar_nome` de `agents/catalog/elevec_sdr/contexto.py` se ele já der o
primeiro nome; não duplique a lógica.

- [ ] **Step 5: Implementar `reativar_agentes` e `rodada`**

```sql
update leads_crm set agent_active = true, agent_reactivate_at = null
where agent_active = false and agent_reactivate_at < now()
```

Mantido por paridade, hoje inerte (nada escreve `agent_reactivate_at`). O
docstring deve dizer isso, para ninguém achar que está quebrado.

`rodada` reivindica, envia um a um, e devolve `{"enviados": n, "falhas": n}`.
Falha de envio **não** desfaz o contador — é a divergência documentada. Cada falha
sai no log com `followup_envio_falhou` e o telefone.

- [ ] **Step 6: Rodar e ver passar**

- [ ] **Step 7: Provar por mutação — obrigatório, mínimo 8**

Cada uma tem que matar teste:
(a) tirar `for update skip locked`; (b) tirar o predicado de janela; (c) tirar
`agent_active` do filtro; (d) tirar `qualificado` da lista de fases terminais;
(e) trocar `followup_count + 1` por `followup_count`; (f) mover o incremento para
depois do envio; (g) `montar_mensagem` nível 1 usando o nome inteiro;
(h) `limit` ignorado.

- [ ] **Step 8: Commit**

```bash
git add src/whatsapp_langchain/worker/followup.py \
        src/whatsapp_langchain/shared/config.py \
        tests/integration/test_followup.py
git commit -m "feat: regua de follow-up com reivindicacao atomica e janela de 24h"
```

---

## Task 4: A task no Worker

**Files:**
- Modify: `src/whatsapp_langchain/worker/main.py`
- Test: `tests/integration/test_followup.py` (acrescentar)

**Interfaces:**
- Consumes: `rodada`, `reativar_agentes` (Task 3).

- [ ] **Step 1: Escrever o teste que falha**

```python
async def test_task_desligada_por_padrao_nao_reivindica(pool, lead_factory, monkeypatch):
    """FOLLOWUP_ENABLED default false — subir o worker não pode disparar nada."""
    monkeypatch.setattr(settings, "followup_enabled", False)
    await lead_factory(phone="5511900000050", phase="iniciou_conversa",
                       followup_count=0, minutos_desde_interacao=60)

    tarefa = iniciar_followup(pool, cliente=ClienteQueContaEnvios())
    assert tarefa is None, "com a flag desligada não deve nem existir task"


async def test_excecao_numa_rodada_nao_derruba_o_loop(pool, monkeypatch, caplog):
    """Uma rodada que explode não pode matar o follow-up até o próximo deploy."""
    chamadas = {"n": 0}

    async def rodada_que_explode(*args, **kwargs):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            raise RuntimeError("banco caiu")
        return {"enviados": 0, "falhas": 0}

    monkeypatch.setattr("whatsapp_langchain.worker.main.rodada", rodada_que_explode)
    monkeypatch.setattr(settings, "followup_interval_seconds", 0.01)
    ...
    assert chamadas["n"] >= 2, "o loop precisa sobreviver à primeira exceção"
```

- [ ] **Step 2: Rodar e ver falhar**

- [ ] **Step 3: Implementar**

Em `main.py`, uma task asyncio ao lado do loop de consumo. Requisitos:

- **Só sobe se `settings.followup_enabled`.** Desligada, `iniciar_followup`
  devolve `None` e loga `followup_desabilitado`.
- **Só sobe se houver cliente Evolution** em `outbounds`. Sem canal, loga
  `followup_sem_canal` e não sobe — a régua manda pela Evolution, não pelos
  outros canais.
- **`try/except` por rodada.** Exceção loga `followup_rodada_falhou` e continua.
  Sem isso, um erro de banco mata a régua silenciosamente até o próximo deploy.
- **Cancelamento limpo** no shutdown, com o `finally` que já fecha o pool.
- Loga o resumo de cada rodada com contagem, não com telefone a telefone.

- [ ] **Step 4: Rodar e ver passar**

- [ ] **Step 5: Provar por mutação**

(a) subir a task com a flag desligada; (b) tirar o `try/except` do loop;
(c) subir sem cliente Evolution.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/worker/main.py tests/integration/test_followup.py
git commit -m "feat: task de follow-up no worker, desligada por padrao"
```

---

## Task 5: Webhook do ChatWoot

**A especificação desta rota tem dois defeitos que este plano corrige.**

**Defeito 1 — a rota não tem autenticação.** Como especificada, qualquer um que
descubra a URL desliga a Renata para qualquer telefone, ou religa ela por cima de
um atendimento humano em andamento. Ganha secret, no mesmo padrão do Evolution.

**Defeito 2 — "caso contrário → `agent_active = true`" religa o agente por
engano.** O ChatWoot dispara webhook para vários eventos. Um `message_created`
não carrega `labels`; lido como "lista vazia de rótulos", ele viraria "sem
`pausar_agente`, logo religa" — **e o agente volta a responder por cima do humano
que acabou de assumir**. A regra correta é: só agir quando o payload realmente
traz a chave `labels`. Ausente → ignorar o evento inteiro.

**Files:**
- Create: `src/whatsapp_langchain/server/routes/webhook_chatwoot.py`
- Modify: `src/whatsapp_langchain/server/app.py` (registrar o router)
- Modify: `src/whatsapp_langchain/shared/config.py` (`CHATWOOT_WEBHOOK_SECRET`)
- Test: `tests/integration/test_webhook_chatwoot.py`

**Interfaces:**
- Consumes: `canonicalizar` de `shared/phone.py`.
- Produces: `POST /webhook/chatwoot`.

- [ ] **Step 1: Escrever os testes que falham**

```python
async def test_rotulo_pausar_agente_desliga_o_agente_e_a_regua(client, pool, lead_factory):
    await lead_factory(phone="5511987654321", agent_active=True, followup_active=True)

    r = await client.post("/webhook/chatwoot", json={
        "event": "conversation_updated",
        "labels": ["pausar_agente", "vip"],
        "meta": {"sender": {"identifier": "+55 11 98765-4321"}},
    }, headers={"x-chatwoot-secret": SECRET})

    assert r.status_code == 200
    lead = await buscar(pool, "5511987654321")
    assert lead["agent_active"] is False
    assert lead["followup_active"] is False


async def test_remover_o_rotulo_devolve_o_lead_ao_agente(client, pool, lead_factory):
    await lead_factory(phone="5511987654321", agent_active=False, followup_active=False)

    r = await client.post("/webhook/chatwoot", json={
        "event": "conversation_updated",
        "labels": ["vip"],
        "meta": {"sender": {"identifier": "5511987654321"}},
    }, headers={"x-chatwoot-secret": SECRET})

    assert r.status_code == 200
    lead = await buscar(pool, "5511987654321")
    assert lead["agent_active"] is True
    assert lead["followup_active"] is True


async def test_evento_sem_a_chave_labels_nao_religa_o_agente(client, pool, lead_factory):
    """O defeito que este teste tranca: message_created religaria o agente por
    cima do humano que acabou de assumir a conversa."""
    await lead_factory(phone="5511987654321", agent_active=False, followup_active=False)

    r = await client.post("/webhook/chatwoot", json={
        "event": "message_created",
        "content": "oi, aqui é o Silvio",
        "meta": {"sender": {"identifier": "5511987654321"}},
    }, headers={"x-chatwoot-secret": SECRET})

    assert r.status_code == 200
    lead = await buscar(pool, "5511987654321")
    assert lead["agent_active"] is False, "evento sem labels não pode religar"


async def test_sem_secret_devolve_401(client):
    r = await client.post("/webhook/chatwoot", json={"event": "conversation_updated",
                                                     "labels": ["pausar_agente"]})
    assert r.status_code == 401


async def test_secret_errado_devolve_401(client):
    r = await client.post("/webhook/chatwoot",
                          json={"event": "conversation_updated", "labels": []},
                          headers={"x-chatwoot-secret": "errado"})
    assert r.status_code == 401


async def test_identifier_ausente_nao_derruba_a_rota(client):
    r = await client.post("/webhook/chatwoot", json={
        "event": "conversation_updated", "labels": ["pausar_agente"], "meta": {},
    }, headers={"x-chatwoot-secret": SECRET})
    assert r.status_code == 200


async def test_lead_inexistente_nao_e_criado_pelo_chatwoot(client, pool):
    """A rota pausa quem existe; ela não é porta de entrada de lead."""
    r = await client.post("/webhook/chatwoot", json={
        "event": "conversation_updated", "labels": ["pausar_agente"],
        "meta": {"sender": {"identifier": "5511999999999"}},
    }, headers={"x-chatwoot-secret": SECRET})

    assert r.status_code == 200
    assert await buscar(pool, "5511999999999") is None


async def test_telefone_com_9_e_sem_9_atingem_o_mesmo_lead(client, pool, lead_factory):
    """Mesma canonicalização do gate — senão a pausa erra o alvo em silêncio."""
    await lead_factory(phone="551187654321", agent_active=True)

    r = await client.post("/webhook/chatwoot", json={
        "event": "conversation_updated", "labels": ["pausar_agente"],
        "meta": {"sender": {"identifier": "+5511987654321"}},
    }, headers={"x-chatwoot-secret": SECRET})

    assert r.status_code == 200
    lead = await buscar(pool, "551187654321")
    assert lead["agent_active"] is False
```

- [ ] **Step 2: Rodar e ver falhar** — 404, a rota não existe.

- [ ] **Step 3: Implementar**

Espelhe `webhook_evolution.py`: `verify_chatwoot_webhook_secret` como dependência,
`Depends(...)`, comparação com `secrets.compare_digest`. Regras:

- `labels` ausente do payload → devolve `{"ok": True, "acao": "ignorado"}` sem
  tocar no banco.
- `identifier` ausente, vazio ou que não canonicaliza → 200 com
  `{"acao": "telefone_invalido"}` e log. **Nunca 500** — o ChatWoot repetiria.
- O `UPDATE` casa pelas **duas variações** (com e sem o nono dígito), igual ao
  gate. Sem isso, a pausa erra o alvo em silêncio.
- Nunca cria lead. `UPDATE`, não `upsert`.
- Retomar liga `agent_active` **e** `followup_active` — espelha o que
  `reverter_fase_apos_cancelamento` já faz.

- [ ] **Step 4: Rodar e ver passar**

- [ ] **Step 5: Provar por mutação — mínimo 6**

(a) tratar `labels` ausente como lista vazia; (b) remover a dependência do secret;
(c) `==` em vez de `compare_digest`; (d) casar só a variação exata do telefone;
(e) trocar `UPDATE` por upsert; (f) retomar sem religar `followup_active`.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/server/routes/webhook_chatwoot.py \
        src/whatsapp_langchain/server/app.py \
        src/whatsapp_langchain/shared/config.py \
        tests/integration/test_webhook_chatwoot.py
git commit -m "feat: webhook do ChatWoot com secret e filtro de evento"
```

---

## Task 6: Configuração e documentação

**Files:**
- Modify: `.env.example`, `deploy/.env.prod.example`
- Modify: `docs/AGENTE_ELEVEC.md`
- Modify: `CLAUDE.md`
- Test: `tests/unit/test_config_sdr.py` (acrescentar)

- [ ] **Step 1: Teste do fail-fast**

`CHATWOOT_WEBHOOK_SECRET` segue a regra do `EVOLUTION_WEBHOOK_SECRET`:
obrigatório em `ENVIRONMENT=production`, mínimo 32 caracteres. E
`FOLLOWUP_ENABLED=true` sem canal Evolution configurado derruba o boot em modo
real — ligar a régua sem por onde mandar é erro de configuração, não runtime.

```python
def test_followup_ligado_sem_canal_evolution_derruba_o_boot(monkeypatch): ...
def test_chatwoot_secret_fraco_derruba_o_boot_em_producao(monkeypatch): ...
```

- [ ] **Step 2: Rodar, ver falhar, implementar, ver passar**

- [ ] **Step 3: Documentar as variáveis**

Nos dois `.env.example`, um bloco com todas as sete do follow-up mais
`CHATWOOT_WEBHOOK_SECRET`. **O comentário do nível 3 tem que explicar o 22.**
Texto sugerido:

```
# Degrau 3 em 22h, não 23h como no n8n. Cada degrau conta a partir do envio
# anterior, então 23h faz o disparo cair em ~T+24h15 desde a última mensagem
# DO LEAD — fora da janela de 24h da Cloud API, e a Meta rejeita. Com 22h o
# disparo cai em ~T+23h15 e é entregue. Aumentar este valor não atrasa a
# mensagem: faz o lead deixar de ser reivindicado, e a régua morre no degrau 2.
FOLLOWUP_NIVEL3_HORAS=22
```

Corrija também a frase que hoje descreve a régua em `docs/AGENTE_ELEVEC.md`, se
ela repetir os 23h da especificação.

- [ ] **Step 4: `docs/AGENTE_ELEVEC.md`**

Seção nova cobrindo: os três degraus e por que o terceiro é 22h; a divergência do
contador (sobe antes do envio); o que acontece com quem sai da janela; a rota do
ChatWoot, o rótulo `pausar_agente` e o secret; e a nota de que `send_template`
existe mas **não** é usado pelo follow-up, com o motivo.

- [ ] **Step 5: `CLAUDE.md`**

Na tabela de canais, acrescentar `/webhook/chatwoot` à lista de rotas. Na
convenção 11 (roteamento por canal), deixar claro que o ChatWoot **não** é canal
de mensageria — não grava `message_queue.channel`, só mexe em `leads_crm`.

- [ ] **Step 6: Rodar a suíte inteira**

`uv run pytest tests/unit tests/integration -q -m "not docker_demo"`
Esperado: 662 + os novos, tudo verde.

- [ ] **Step 7: Commit**

```bash
git add .env.example deploy/.env.prod.example docs/AGENTE_ELEVEC.md CLAUDE.md \
        src/whatsapp_langchain/shared/config.py tests/unit/test_config_sdr.py
git commit -m "docs: variaveis do follow-up e do ChatWoot, com o porque do degrau de 22h"
```

---

## Auto-revisão do plano

**Cobertura da especificação (seções "Follow-up", "Handover pelo ChatWoot",
"Impacto nas fases"):**

| Requisito | Task |
|---|---|
| `UPDATE ... RETURNING` atômico, `FOR UPDATE SKIP LOCKED` | 3 |
| Sem advisory lock, sem transação durante HTTP | 3 |
| Contador sobe antes do envio (divergência documentada) | 3 |
| Três níveis com os textos exatos | 3 |
| Sem nome: nível 1 vira `Oi?`, nível 3 perde o vocativo | 3 |
| Seleção por `followup_active AND agent_active`, fases terminais, `LIMIT 10` | 3 |
| Reativação por `agent_reactivate_at` (inerte, por paridade) | 3 |
| Task a cada 5 minutos | 4 |
| `POST /webhook/chatwoot`, `identifier` + `labels`, `pausar_agente` | 5 |
| `sendTemplate` no `EvolutionClient` | 2 |
| Follow-up ciente da janela de 24h | 1 + 3 |
| `FOLLOWUP_ENABLED`, `FOLLOWUP_INTERVAL_SECONDS` | 3 + 6 |

**Fora do escopo desta fase, por decisão registrada:** reabrir janela fechada
(exige template de retomada aprovado pela Meta — decisão do cliente); ingestão de
leads por formulário (a especificação já a coloca fora); `migrar_supabase.py`
(Fase 4).

**Herdado da Fase 2, a levar para a Fase 4/5:** a coluna `google_event_id` é nova,
e leads migrados do Supabase terão reunião real com a coluna nula. O código lê
nulo como "não tem reunião", o que deixa `update_crm('qualificado')` puxar o card
de volta. Mitigação: backfill na migração.

**Aviso duro para quem implementar a Task 3:** o filtro de fases exclui
`qualificado`. Não tire — a Fase 2 religa `followup_active` no cancelamento de
reunião, e o lead volta como `qualificado`. Se `qualificado` entrar na régua, esse
lead passa a ser perseguido, e isso é **WhatsApp indevido para gente de verdade**.

---

**Plano salvo.** Execução por subagentes, uma task por vez, com revisão entre elas
— o mesmo fluxo das Fases 1 e 2.
