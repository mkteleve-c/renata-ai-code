# Migração do SDR da EleveC: n8n → harness whatsapp-langchain

**Data:** 2026-07-25
**Projeto:** `renata-ai-code` (repo `mkteleve-c/renata-ai-code`)
**Status:** design aprovado, aguardando plano de implementação

## Objetivo

Reimplementar o agente SDR da EleveC — hoje distribuído em 6 workflows n8n com
estado no Supabase — dentro do harness `whatsapp-langchain`, com **paridade
funcional total**, e rodando no **Railway**.

Ao final, **n8n e Supabase são descartados**: nenhum dos dois participa da
operação. O Supabase é lido **uma única vez** para exportar dados históricos.

## Sistema atual (o que está sendo substituído)

### Topologia n8n

| Workflow | Nós | Papel |
|---|---|---|
| `#0 Form & Primeira Abordagem` | 6 | entrada por formulário |
| `#1 Agente SDR \| 10/02/26 \| V2.2` | 47 | núcleo conversacional |
| `#1.1 Handover via ChatWoot` | 5 | liga/desliga por etiqueta |
| `#2 Follow Up \| 16/01/2026 \| v3` | 19 | cron de 5 min |
| `#3 Agenda MCP \| 12 Fev 26 \| v2` | 10 | Google Calendar via MCP |
| `#4 CRM Control \| 16/01/2026 \| v2` | 11 | phase + Pipedrive |
| `#5 Handover \| 10/02/26 \| v2` | 16 | desliga agente, avisa humano |

### Canal

Evolution API, instância `instancia-apioficial`, integração
**`WHATSAPP-BUSINESS`** (Meta Cloud API oficial por baixo), status `open`,
Phone Number ID `1025192374009897`. Servidor: `evolution.ju39tu.easypanel.host`.

Envio: `POST /message/sendText/{instanceName}`, header `apikey`, body
`{number, text, delay, linkPreview, quoted}`. O campo `delay` (ms) é nativo.

O payload inbound chega em formato Baileys: `body.instance`,
`body.data.key.{remoteJid, remoteJidAlt, fromMe, id}`, `body.data.pushName`,
`body.data.messageType`, `body.data.message.{conversation, audioMessage.url,
imageMessage.url}`.

### Volume em produção (medido em 2026-07-26)

| Métrica | Valor |
|---|---|
| Leads | 3.319 |
| Sessões de conversa / mensagens | 736 / 8.912 |
| `formulario_preenchido` | 2.559 |
| `iniciou_conversa` | 354 |
| `qualificado` | 175 |
| `agendou_sessao` | 152 |
| `desqualificado` | 79 |
| Leads com `agent_active = false` | 29 |
| Leads com `email` preenchido | **0** |
| Leads com `agent_reactivate_at` | **0** |

Os dois zeros documentam buracos do fluxo atual: o e-mail é coletado na conversa
mas nunca gravado, e o handover é permanente na prática (a coluna de reativação
existe e é lida pelo cron, mas nada nunca a preenche).

## Decisões de arquitetura

| # | Decisão | Motivo |
|---|---|---|
| 1 | Nativo desde o dia 1 (sem n8n como backend) | requisito do cliente: descartar n8n |
| 2 | Postgres próprio no Railway (`Dockerfile.db`, pgvector), **privado** | sem n8n externo, não há motivo para expor |
| 3 | Novo canal `evolution` no harness | mantém instância e número atuais intactos |
| 4 | Gate de ingestão na API, antes de enfileirar | `fromMe` é eco; não pode virar mensagem processada |
| 5 | Follow-up como task asyncio no Worker + `pg_try_advisory_lock` | evita serviço novo; lock garante execução única sob réplicas |
| 6 | `MEMORY_ENABLED=false` na v1 | o n8n não tem memória semântica; ligar mudaria comportamento |
| 7 | Telefone canônico **sem** o 9º dígito em todo lugar | replica `phone_v2`; divergência quebra histórico |
| 8 | Portões de e-mail/faturamento validados em código | hoje são só instrução de prompt, e o próprio prompt os chama de "INVIOLÁVEIS" |

## Arquitetura alvo

```
Evolution API (instancia-apioficial, WHATSAPP-BUSINESS)
   │  POST /webhook/evolution?agent=elevec_sdr
   ▼
┌─ API (FastAPI) ───────────────────────────────────────┐
│ webhook_evolution.py                                  │
│   1. resolve telefone (score remoteJid/remoteJidAlt)  │
│   2. blocklist            → descarta                  │
│   3. fromMe=true          → desliga agente, descarta  │
│   4. upsert leads_crm (canoniza sem 9)                │
│   5. agent_active=false   → descarta                  │
│   6. enfileira (channel=evolution)                    │
│ webhook_chatwoot.py  → etiqueta pausar_agente         │
└───────────────────────────────────────────────────────┘
   ▼  buffer nativo agrupa mensagens consecutivas
┌─ Worker ──────────────────────────────────────────────┐
│ processor.py                                          │
│   • media.py: áudio→transcrição, imagem→descrição     │
│   • agente elevec_sdr (LangGraph)                     │
│   • saída {messages:[...]} → N envios com delay       │
│ followup.py  ← asyncio + advisory lock, a cada 5 min  │
└───────────────────────────────────────────────────────┘
   ▼
evolution_client.py → POST /message/sendText/{instance}
```

### Componentes

Cada módulo tem uma responsabilidade e é testável isoladamente.

| Módulo | Faz | Depende de |
|---|---|---|
| `shared/phone.py` | canonicalização do 9º dígito, score de JID, blocklist | nada (puro) |
| `shared/leads.py` | CRUD de `leads_crm`, gate de ingestão | pool do Postgres |
| `server/routes/webhook_evolution.py` | valida payload, aplica gate, enfileira | `phone`, `leads`, `queue` |
| `server/routes/webhook_chatwoot.py` | liga/desliga agente por etiqueta | `phone`, `leads` |
| `worker/evolution_client.py` | envio de texto/mídia, presença | HTTP |
| `worker/followup.py` | cron de 5 min com advisory lock | `leads`, cliente outbound |
| `agents/catalog/elevec_sdr/` | prompt, grafo, saída estruturada | LangGraph |
| `.../tools/calendar.py` | 5 operações de Google Calendar | Google API |
| `.../tools/crm.py` | `update_crm`: phase + Pipedrive | `leads`, Pipedrive |
| `.../tools/handover.py` | desliga agente, notifica humano | `leads`, cliente outbound |

### Arquivos

```
NOVOS
  src/whatsapp_langchain/shared/phone.py
  src/whatsapp_langchain/shared/leads.py
  src/whatsapp_langchain/server/routes/webhook_evolution.py
  src/whatsapp_langchain/server/routes/webhook_chatwoot.py
  src/whatsapp_langchain/worker/evolution_client.py
  src/whatsapp_langchain/worker/followup.py
  src/whatsapp_langchain/agents/catalog/elevec_sdr/{__init__,agent,graph,prompts}.py
  src/whatsapp_langchain/agents/catalog/elevec_sdr/tools/{__init__,calendar,crm,handover}.py
  db/migrations/007_elevec.sql
  scripts/migrar_supabase.py          ← export único
  tests/unit/test_phone.py
  tests/unit/test_gate_ingestao.py
  tests/unit/test_saida_baloes.py
  tests/integration/test_webhook_evolution.py
  tests/integration/test_followup.py

TOCADOS
  shared/models.py      → MessagingChannel.EVOLUTION
  shared/config.py      → settings de Evolution, ChatWoot, Google, Pipedrive
  worker/processor.py   → envio de múltiplos balões
  worker/main.py        → sobe a task de follow-up
  server/main.py        → registra as 2 rotas novas
  langgraph.json        → registra elevec_sdr
```

## Modelo de dados

`message_queue.channel` é `TEXT` sem CHECK (`006_message_channel.sql`), então o
canal novo **não exige migração de coluna** — só o valor `evolution` no enum
Python.

### `007_elevec.sql`

Cria o schema do SDR no Postgres do Railway. Enums e `leads_crm` são recriados
(não existem no banco novo), com as colunas que faltavam:

```sql
CREATE TYPE lead_phase AS ENUM (
  'formulario_preenchido','iniciou_conversa','qualificado',
  'agendou_sessao','desqualificado','perdido');

CREATE TYPE lead_source AS ENUM (
  'linkedin_form','respondiapp_form','whatsapp_direct','manual_import');

CREATE TABLE leads_crm (
  phone                TEXT PRIMARY KEY,
  pipedriveid          TEXT,
  name                 TEXT,
  username             TEXT,
  email                TEXT,
  faturamento_mensal   TEXT,           -- NOVO
  qualificacao_notas   TEXT,           -- NOVO
  google_event_id      TEXT,           -- NOVO
  phase                lead_phase DEFAULT 'formulario_preenchido',
  source               lead_source,
  followup_count       INT DEFAULT 0,
  followup_active      BOOLEAN DEFAULT true,
  agent_active         BOOLEAN DEFAULT true,
  agent_reactivate_at  TIMESTAMPTZ,
  created_at           TIMESTAMPTZ DEFAULT now(),
  last_interaction_at  TIMESTAMPTZ DEFAULT now(),
  metadata             JSONB DEFAULT '{}'
);

CREATE TABLE blocklist (
  phone      TEXT PRIMARY KEY,
  motivo     TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE legacy_chat_history (
  phone   TEXT,
  idx     INT,
  role    TEXT,
  content TEXT,
  PRIMARY KEY (phone, idx)
);

CREATE INDEX idx_leads_followup
  ON leads_crm (followup_active, agent_active, last_interaction_at)
  WHERE followup_active AND agent_active;
```

**`faturamento_mensal` é `TEXT`, não numérico.** O lead responde "uns 30 mil",
"entre 20 e 25" — forçar número perde informação.

**`google_event_id`** elimina a busca por evento no reagendamento/cancelamento.
Hoje o agente precisa varrer o calendário para achar o evento; guardar o id
torna `calendar_update` e `calendar_delete` determinísticos.

Não existem `message_buffer` nem `n8n_chat_histories`: o harness resolve os dois
nativamente (`MESSAGE_BUFFER_SECONDS` e checkpointer do LangGraph).

### Export único do Supabase (`scripts/migrar_supabase.py`)

Leitura única, sem dependência permanente:

1. `leads_crm` → 3.319 linhas, cópia direta (colunas novas ficam nulas)
2. `n8n_chat_histories` → últimas **12 mensagens** por `session_id` →
   `legacy_chat_history` (736 sessões)
3. Os 28 números hardcoded no nó `Filtro e Permissao v002` → `blocklist`

Validação obrigatória pós-migração: contagem por `phase` idêntica à origem.

### Continuidade das conversas

Sem histórico, ~529 leads ativos (`iniciou_conversa` + `qualificado`) receberiam
a saudação da Fase 1 outra vez — inclusive quem já passou pelo portão de
faturamento.

Mitigação: no primeiro turno de um `thread_id` sem checkpoint, o middleware
carrega `legacy_chat_history` daquele telefone, injeta como histórico e marca
como consumido (`metadata->>'historico_injetado'`).

## O agente `elevec_sdr`

### Configuração

| Item | Valor | Origem |
|---|---|---|
| Modelo | `x-ai/grok-4.3` | idem n8n |
| Temperatura | `0.3` | idem n8n |
| `CONTEXT_STRATEGY` | `trim`, ~12 mensagens | equivale a `contextWindowLength: 12` |
| `MEMORY_ENABLED` | `false` | paridade |
| Saída | `{messages: [string]}`, `minItems: 1` | idem n8n |
| `thread_id` | `{phone_canonico}:elevec_sdr` | convenção do harness |

### Prompt

Portado **literalmente** do nó `AI Agent`: persona Renata, contexto EleveC /
Silvio Hirata, critérios de desqualificação C1 (recolocação) e C2 (fora de
escopo), as 8 fases do SOP, regras de tools, regras de escassez, sequência
inviolável de agendamento e as regras anti-jailbreak.

Única mudança: interpolação das variáveis (`{nome}`, `{origem}`, `{telefone}`,
`{data_hoje}`) pelo mecanismo do harness em vez das expressões n8n.

### Tools

Todas nativas.

**`calendar_get_many(after, periodo)`** — política extraída do código do `#3`:

| Regra | Valor |
|---|---|
| Agenda | `silvio.hirata@eleve-c.co` |
| Timezone | `America/Sao_Paulo` (-03:00) |
| Dias permitidos | segunda a sexta |
| Manhã / Tarde / Noite | `8,9,10,11` / `13,14,15,16,17` / `18,19,20,21` |
| Duração do slot | 60 min, horas cheias |
| Janela | 4 dias a partir de **D+1** (nunca hoje) |
| Saída | `quinta 12/02: 13, 14, 16, 17` |

> O código original calcula disponibilidade para 4 dias (`windowDays: 4`) mas
> busca eventos de 7 dias no nó do Google. Padronizamos em **4** — a busca maior
> era desperdício, não regra.

**`calendar_agendar(start, end, attendees, summary)`** — título
`Consultoria de Alavancagem de Carreira - {Nome}`, duração padrão 60 min,
attendee obrigatório com o e-mail do lead. Grava `google_event_id` no lead.

*Validação em código, não só no prompt:* recusa se `email` ou
`faturamento_mensal` estiverem vazios no lead, e reconsulta disponibilidade
antes de criar. Esta é a "Sequência Inviolável" do prompt, garantida por `if`.

**`calendar_update` / `calendar_delete` / `calendar_get_event`** — usam
`google_event_id` do lead; só varrem o calendário se ele estiver ausente.

**`update_crm(phone, phase)`** — `UPDATE leads_crm`; `agendou_sessao` e
`desqualificado` desligam `followup_active`. Pipedrive: `PUT /deals/{id}` com
`stage_id` **12** (`qualificado`) ou **13** (`agendou_sessao`);
`desqualificado` não move card.

**`human_handover(phone, reason)`** — zera `agent_active` e `followup_active`,
e envia WhatsApp para o número do responsável com o motivo e `wa.me/{phone}`.

### Autenticação Google

OAuth2 com o par `client_id`/`client_secret` e o `refresh_token` da credencial
`Google Calendar account | Silvio H.`, extraída do n8n e **validada**: renova
access token e tem `owner` em `silvio.hirata@eleve-c.co`.

Risco registrado: rotacionar o OAuth Client no Google Cloud Console invalida o
refresh token e derruba o agendamento.

## Gate de ingestão

Ordem exata, replicando o SQL `Add New Lead`:

1. **Resolver telefone** — pontua `remoteJidAlt` e `remoteJid`: base 2 se tiver
   12–14 dígitos, +2 se contiver `@s.whatsapp.net`, +1 se começar com `55`.
   Maior score vence. Sem candidato válido → descarta.
2. **Blocklist** — sufixo casando com a tabela → descarta.
3. **Variações do 9º dígito** — `^55(\d{2})(\d{8,9})$` gera `phone_v1` (com 9) e
   `phone_v2` (sem 9).
4. **`fromMe = true`** — humano respondeu pelo celular: `agent_active = false`,
   `followup_active = false`, `agent_reactivate_at = NULL`, descarta.
5. **Upsert** — busca por qualquer variação, **grava sempre `phone_v2`**,
   atualiza `last_interaction_at`, zera `followup_count`, promove
   `formulario_preenchido → iniciou_conversa`.
6. **`agent_active = false`** — descarta.
7. **Enfileira** com `channel = 'evolution'`.

## Follow-up

Task asyncio no Worker, a cada 5 min, protegida por `pg_try_advisory_lock` — se
outra réplica já detém o lock, a rodada é pulada.

Reativação (mantida por paridade, mesmo hoje inerte):

```sql
UPDATE leads_crm SET agent_active = TRUE, agent_reactivate_at = NULL
WHERE agent_active = FALSE AND agent_reactivate_at < NOW();
```

Seleção — `followup_active AND agent_active`, `phase NOT IN ('agendou_sessao',
'desqualificado', 'perdido', 'qualificado')`, `LIMIT 10`:

| Nível | Gatilho | Mensagem |
|---|---|---|
| 1 | `followup_count = 0` e 15 min sem interação | `{PrimeiroNome}?` |
| 2 | `followup_count = 1` e 1 hora | `Opa, imagino que esteja corrido ai! Só para não perdermos o timing da sua aplicação, consegue falar agora?` |
| 3 | `followup_count = 2` e 23 horas | `{PrimeiroNome}, tudo bem? Ainda faz sentido falarmos sobre o seu momento de carreira?` |

Sem nome: nível 1 vira `Oi?` e nível 3 perde o vocativo. Após enviar,
`followup_count + 1` e `last_interaction_at = NOW()`.

## Handover pelo ChatWoot

`POST /webhook/chatwoot` lê `body.meta.sender.identifier` (telefone) e
`body.labels`. Se contiver `pausar_agente` → `agent_active = false` +
`followup_active = false`. Caso contrário → `agent_active = true`.

## Tratamento de erro

Herdado do harness: lease com retry e `MAX_ATTEMPTS` cobrem falha de LLM e de
envio.

Específico do SDR:

| Situação | Comportamento |
|---|---|
| Agenda falha 3× | `human_handover` — contador no estado do grafo, não no modelo |
| Jailbreak / prompt injection | `human_handover` (regra do prompt) |
| Saída fora do schema | 1 retry com instrução de correção; persistindo, envia como balão único |
| `calendar_agendar` sem e-mail ou faturamento | tool recusa e devolve erro ao agente |
| Envio de balão falha no meio | os já enviados não são reenviados; retry cobre só o restante |

## Testes

**Unitários** (sem banco):
- `test_phone.py` — com/sem 9, DDDs, números não-BR, score `remoteJid` vs
  `remoteJidAlt`, blocklist por sufixo
- `test_gate_ingestao.py` — `fromMe`, `agent_active=false`, blocklist,
  promoção de phase
- `test_saida_baloes.py` — parse do `{messages:[...]}`, fallback de balão único

**Integração:**
- `test_webhook_evolution.py` — payload real, enfileiramento correto
- `test_followup.py` — os 3 níveis com relógio controlado, e o advisory lock
  impedindo execução dupla

## Deploy no Railway

Quatro serviços (`docs/RAILWAY.md` cobre a mecânica):

| Serviço | Dockerfile | Notas |
|---|---|---|
| `db` | `Dockerfile.db` | pgvector, volume em `/var/lib/postgresql/data`, **privado** |
| `api` | `Dockerfile.api` | público; recebe webhooks Evolution e ChatWoot |
| `worker` | `Dockerfile.worker` | privado; **1 réplica** na v1 |
| `frontend` | `Dockerfile.frontend` | painel admin |

Variáveis novas além das de `docs/RAILWAY.md`:

```
EVOLUTION_BASE_URL, EVOLUTION_API_KEY, EVOLUTION_INSTANCE
CHATWOOT_TOKEN
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, GOOGLE_CALENDAR_ID
PIPEDRIVE_API_TOKEN, PIPEDRIVE_STAGE_QUALIFICADO=12, PIPEDRIVE_STAGE_AGENDADO=13
HANDOVER_NOTIFY_PHONE
FOLLOWUP_ENABLED, FOLLOWUP_INTERVAL_SECONDS=300
```

## Fases de entrega

1. **Fundação** — `007`, `phone.py`, `leads.py`, canal Evolution, `MessagingChannel.EVOLUTION`
2. **Agente** — `elevec_sdr`, prompt portado, saída em balões, tools nativas
3. **Periféricos** — follow-up com advisory lock, webhook ChatWoot
4. **Migração de dados** — `migrar_supabase.py` + validação de contagens
5. **Cutover** — desligar workflows n8n, repontar webhook da Evolution, monitorar

## Pré-condição bloqueante da Fase 1

**É necessário capturar um payload real** do webhook da Evolution nesta
instância para confirmar que `remoteJidAlt` vem preenchido na integração
`WHATSAPP-BUSINESS`. `remoteJidAlt` é conceito do Baileys, e **toda** a
canonicalização do 9º dígito depende dele. Se não vier, o resolver de telefone
precisa de outra fonte e o passo 1 do gate muda.

Não presumir. Validar antes de escrever `phone.py`.

## Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| `remoteJidAlt` ausente na integração oficial | alto | pré-condição acima |
| Rotação do OAuth Client do Google | alto | documentado; ninguém rotaciona sem aviso |
| Cutover perde mensagens | médio | fora do horário comercial; conferir `max(last_interaction_at)` antes/depois |
| Divergência de canonicalização | médio | `phone.py` único, com testes |
| Grok-4.3 responder fora do schema | baixo | retry + fallback de balão único |
| Follow-up duplicado sob réplicas | baixo | advisory lock |

## Fora de escopo

- `#00 ZAPIER | Formulário Linkedin` (71 nós) e `#0 Form` — entrada de leads por
  formulário continua fora do harness nesta etapa
- `Green Webhook` (D4Sign, contratos) — não faz parte do SDR
- Memória semântica — proposta para depois da v1, com medição antes/depois
- Migração do histórico completo (8.912 mensagens) — só a janela de 12 por sessão
