# Arquitetura

Este projeto ensina agentes por uma perspectiva de **harness operacional**.
O agente é só uma parte da solução. O valor real está no harness completo:
entrada multi-canal confiável, processamento assíncrono, persistência, recuperação
de falhas, inspeção operacional e deploy reproduzível.

## Estado atual

Implementado:

- API FastAPI assíncrona com **três webhooks paralelos**:
  - `POST /webhook/twilio` — Twilio (HMAC-SHA1)
  - `GET/POST /webhook/meta` — Meta WhatsApp Cloud API (handshake + HMAC-SHA256)
  - `POST /webhook/uazapi` — uazapi/uazapiGO (Baileys; token da instância no payload)
- Validação criptográfica de assinatura por canal (SDK oficial Twilio; HMAC-SHA256 manual no Meta).
- Fila em PostgreSQL (`message_queue`) com debounce texto-only, flush antes de mídia, advisory lock por `phone+agent+channel` e lease.
- Worker assíncrono consumindo fila com `FOR UPDATE SKIP LOCKED`.
- **Roteamento outbound automático por canal**: cada mensagem na fila carrega `channel`; o worker mantém um cliente outbound por canal habilitado e seleciona o cliente certo na hora de responder.
- Clientes outbound: `TwilioClient` (API Key), `MetaClient` (System User Bearer + Graph API), `UazapiClient` (token da instância via header `token`).
- Typing indicator best-effort (Twilio API Beta; Meta usa `status=read`; uazapi usa `presence=composing`).
- Execução de agentes via loader dinâmico — catálogo atual: `illumi_assistant` (interno) e `rhawk_assistant` (cliente Top Hawks).
- Checkpointer PostgreSQL (`AsyncPostgresSaver`) — contexto por `thread_id = "{phone}:{agent_id}"`.
- Store semântico PostgreSQL (`AsyncPostgresStore` + pgvector) — memória cross-thread por `user_id = phone_number`.
- Middleware de contexto configurável (`trim` | `summarize` | `none`).
- Memória semântica orientada a tools (`save_memory` / `read_memory`).
- Processamento de mídia (imagem e áudio) via OpenRouter multimodal — Twilio e uazapi têm download direto; Meta entrega `media_id` (download via Graph API ainda não implementado).
- Retry com backoff progressivo e status final em `failed`.
- Frontend Next.js 16 (admin panel: `/login`, `/queue`, `/chats`, `/agents`, `/settings`) com Better Auth no schema `auth`.
- Rotas administrativas `/api/agents`, `/api/chats`, `/api/metrics` protegidas por `INTERNAL_SERVICE_TOKEN` (compartilhado entre frontend e API).
- Validação fail-fast no boot (`Settings.validate_runtime_settings`): canal "tocado parcialmente" em `OUTBOUND_MODE=real` derruba API e Worker antes de aceitar tráfego.
- **Deploy oficial**: Docker + Traefik v3.6.15 + Let's Encrypt (`deploy/`), com pgweb em `/banco` (BasicAuth) para visualização do banco. Postgres não é exposto publicamente.
- **Deploy alternativo**: Railway (`docs/RAILWAY.md`).
- Stress testing com Locust documentado.

Limitações conhecidas:

- Mídia inbound via Meta ainda chega como `media_id` — o download via Graph API não está implementado; mensagem entra com placeholder.
- `NumMedia > 1` (Twilio) ou múltiplas mídias por payload uazapi ficam fora do escopo: apenas a primeira é processada.
- Templates outbound (HSM) não são enviados — só respondemos dentro da janela de 24h.

## Visão do harness

![Arquitetura](diagrams/harness_whatsapp.jpg)

```text
[WhatsApp]
   │
   ├──► Twilio  ──► POST /webhook/twilio?agent=…  (X-Twilio-Signature)
   ├──► Meta    ──► GET/POST /webhook/meta?agent=…  (X-Hub-Signature-256)
   └──► uazapi  ──► POST /webhook/uazapi?agent=…    (token no payload)
                          │
                          ▼
                 ┌────────────────────┐
                 │  API (FastAPI)     │
                 │  - valida agente   │
                 │  - valida assinat. │
                 │  - rate limit      │
                 │  - enqueue/debounce│
                 │  - grava channel + │
                 │    outbound_token  │
                 └─────────┬──────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │  PostgreSQL          │
                │  - message_queue     │
                │  - conversations     │
                │  - checkpoints       │
                │  - store_vectors     │
                │  - schema "auth"     │
                └─────────┬────────────┘
                          │
                          ▼
                ┌──────────────────────┐
                │  Worker              │
                │  - claim com lease   │
                │  - processa mídia    │
                │  - envia typing      │
                │  - invoca agente     │
                │  - seleciona cliente │
                │    outbound pelo     │
                │    channel           │
                │  - mark_done         │
                └──┬────────┬────────┬─┘
                   │        │        │
                   ▼        ▼        ▼
                Twilio    Meta    uazapi
                outbound  Graph   /send/text
                (API Key) Bearer  (token)


[Frontend Next.js + Better Auth]   ──server-side── ► API (/api/*)
        ▲                          INTERNAL_SERVICE_TOKEN
        │
   admin (browser)
```

Em produção o tráfego entra pelo Traefik (`:80` → redirect → `:443`), com TLS automático via Let's Encrypt, e é roteado por `Host(${DOMAIN}) + PathPrefix`:

| URL | Serviço | Prioridade |
|---|---|---|
| `/webhook/*`, `/health`, `/api/agents`, `/api/chats`, `/api/metrics` | API | 100 |
| `/banco/*` | pgweb (BasicAuth) | 90 |
| catchall (UI + `/api/auth/*` do Better Auth) | Frontend Next.js | 10 |

## Fronteiras e contratos

### API (`src/whatsapp_langchain/server/`)

Responsabilidades:

- aceitar webhooks dos três canais
- validar assinatura quando habilitada (`VALIDATE_TWILIO_SIGNATURE`, `META_VALIDATE_SIGNATURE`)
- responder rápido — TwiML vazio (Twilio), JSON `{status: ok}` (Meta/uazapi)
- não executar agente inline
- enfileirar payload normalizado, gravando o `channel` da mensagem (e `outbound_token` no caso uazapi)
- proteger `/api/*` via `INTERNAL_SERVICE_TOKEN` compartilhado com o frontend

Contratos relevantes:

- `agent` via query string (`?agent=<agent_id>`) — vale para os 3 webhooks; o agente é selecionado no momento da chamada
- `thread_id = "{phone_number}:{agent_id}"` (chave do checkpointer)
- `user_id = phone_number` (chave do store semântico)
- identidade inbound:
  - Twilio: `From=whatsapp:+E.164` (fallback `WaId`)
  - Meta: `entry[].changes[].value.messages[].from = "<E.164 sem +>"`
  - uazapi: `chatid = "<E.164>@s.whatsapp.net"` (ou `chat.wa_chatid` / `chat.phone`)

### Worker (`src/whatsapp_langchain/worker/`)

Responsabilidades:

- fazer polling da fila (`claim_next` com `FOR UPDATE SKIP LOCKED`)
- processar mídia se existir (download autenticado conforme canal)
- enviar typing best-effort no canal de origem
- carregar agente com checkpointer/store compartilhados (abertos no boot)
- invocar grafo com `thread_id` e `user_id`
- **selecionar o cliente outbound pelo `channel` da mensagem** e enviar a resposta
- persistir sucesso/falha — `mark_done` apenas após envio confirmado

O worker mantém um dict `outbounds: dict[MessagingChannel, OutboundClient]` com os canais habilitados e levanta `mark_failed` claro se chega uma mensagem cujo canal não está habilitado neste worker (mensagem de outro deploy / canal desligado).

### Frontend (`frontend/`)

- autenticar administradores com Better Auth (tabelas no schema `auth`)
- consumir rotas administrativas server-side via `INTERNAL_API_URL` + `INTERNAL_SERVICE_TOKEN`
- expor `/api/queue` como proxy autenticado por sessão Better Auth (valida cookie e chama `/api/queue` da API internamente)

### Shared (`src/whatsapp_langchain/shared/`)

- `Settings` (pydantic-settings) com helpers `channel_status()` e `validate_runtime_settings()`
- pool/migrações (rodam no startup da API)
- operações de fila com `channel`/`outbound_token`
- modelos Pydantic (`MessageQueue`, `MessagingChannel`, `Conversation`, `EnqueueResult`)
- factory de LLM com rate limiter
- logging estruturado (structlog)

## Modelo de dados

### `message_queue`

Estado da mensagem e ciclo operacional.

Fluxo de status:

```text
queued ──► processing ──► done
                  │
                  └──► queued (retry com backoff)
                  │
                  └──► failed
```

Campos chave (acrescidos pelas migrações 005/006):

| Campo | Função |
|---|---|
| `phone_number`, `agent_id`, `thread_id` | identidade da conversa |
| `channel` | `twilio` \| `meta` \| `uazapi` (mig 006) — define qual cliente outbound o worker usa |
| `outbound_token` | token outbound dinâmico (uazapi) recebido no payload do webhook (mig 005) |
| `incoming_message`, `media_url`, `media_type` | entrada original |
| `normalized_input`, `media_processing_status`, `media_processing_error` | auditoria do pré-processamento de mídia |
| `process_after`, `lease_until` | debounce e lock temporal |
| `attempts` / `max_attempts` | governança de retry |
| `response`, `error` | auditoria de resultado |

`channel` tem `DEFAULT 'twilio'` para preservar comportamento histórico nas linhas pré-existentes; `outbound_token` é nullable e só é usado pelo cliente uazapi.

### `conversations`

Tabela agregada por `(phone_number, agent_id)` para o painel admin. Atualizada por upsert a cada mensagem concluída.

### Checkpointer / Store semântico

Schemas gerenciados pelo LangGraph (não aparecem em `db/migrations/`):

- `checkpoints`, `checkpoint_writes`, `checkpoint_blobs`, `checkpoint_migrations` — `AsyncPostgresSaver`
- `store`, `store_vectors`, `store_migrations`, `vector_migrations` — `AsyncPostgresStore` + pgvector

### Schema `auth`

Tabelas do Better Auth (mig 003 cria o schema; mig 004 cria as tabelas) — `user`, `session`, `account`, `verification`. Frontend usa `search_path=auth,public`; API/Worker não tocam esse schema.

## Fluxo end-to-end (qualquer canal)

1. Usuário envia mensagem no WhatsApp.
2. Provedor (Twilio / Meta / uazapi) faz `POST /webhook/<canal>?agent=<agent_id>`.
3. API valida agente e assinatura/handshake conforme o canal, aplica rate limit e chama `enqueue_or_buffer` passando `channel` (e `outbound_token` no caso uazapi).
4. Debounce concatena textos rápidos do mesmo `phone+agent+channel`; mídia entra imediata e faz flush de texto pendente do mesmo trio.
5. Worker faz `claim_next` com lease e advisory lock por canal — mensagens do mesmo phone vindas de canais distintos não se bloqueiam.
6. Worker pré-processa a entrada (mídia → texto) e monta `HumanMessage`.
7. Worker seleciona o cliente outbound pelo `message.channel` e dispara typing best-effort.
8. Agente executa com checkpointer/store compartilhados.
9. Worker envia a resposta via cliente do canal de origem.
10. `mark_done` + `upsert_conversation`. Falha vai para retry com backoff (`attempts * 5s`) ou `failed` final.

## Habilitação de canais (multi-canal automático)

Não há mais env `MESSAGING_CHANNEL`. Cada canal é habilitado pela presença das próprias credenciais:

| Canal | Credenciais que habilitam |
|---|---|
| Twilio | `TWILIO_ACCOUNT_SID` + `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET` + `TWILIO_FROM_NUMBER` |
| Meta | `META_PHONE_NUMBER_ID` + `META_ACCESS_TOKEN` + `META_VERIFY_TOKEN` (+ `META_APP_SECRET` se `META_VALIDATE_SIGNATURE=true`) |
| uazapi | `UAZAPI_BASE_URL` (token da instância chega via webhook) |

`OUTBOUND_MODE` é compartilhado:

- `real` — envia de verdade; canal "tocado parcialmente" derruba o boot (fail-fast)
- `mock` — simula envio em todos os canais (não exige credenciais)
- vazio — default por ambiente: `production=real`, dev=`mock`

`TWILIO_OUTBOUND_MODE` ainda é aceito como alias retrocompatível e sobrescrevido por `OUTBOUND_MODE` quando ambos estão preenchidos.

## Contexto e memória

### Contexto por thread (checkpointer)

Persistência de mensagens de uma conversa específica (`thread_id = "{phone}:{agent_id}"`).

### Memória semântica por usuário (store)

- namespace: `(user_id, "memories")`
- `save_memory` grava fatos relevantes (tool exposta ao agente)
- `read_memory` recupera memórias por similaridade quando o agente precisar
- `user_id = phone_number` derivado do payload de cada canal

Separa duas necessidades:

- continuidade da conversa atual (checkpointer)
- conhecimento durável sobre o usuário (store)

## Controles do harness

### Debounce

- texto faz debounce em `MESSAGE_BUFFER_SECONDS` (default 2s)
- mídia entra imediata e faz flush dos textos pendentes do mesmo `phone+agent+channel`
- concorrência serializada por `pg_advisory_xact_lock(hash(phone:agent:channel))` — canais distintos não se bloqueiam mutuamente

### Retry com backoff

`mark_failed` aplica `backoff_seconds = attempts * 5` enquanto houver tentativas (`MAX_ATTEMPTS`, default 3).

### Rate limits

- API: limite por telefone/hora in-memory (`RATE_LIMIT_PER_HOUR`)
- LLM: token bucket por processo (`InMemoryRateLimiter`)

### Observabilidade

Logs estruturados via `structlog`. Eventos relevantes incluem `channel=<twilio|meta|uazapi>` quando aplicável, facilitando filtragem no log shipper.

## Endpoints disponíveis

Públicos (Traefik priority 100 em produção):

- `GET /health`
- `POST /webhook/twilio?agent=<id>`
- `GET /webhook/meta` (handshake) / `POST /webhook/meta?agent=<id>`
- `POST /webhook/uazapi?agent=<id>`
- `POST /webhook/sync?agent=<id>` — apenas em `ENVIRONMENT=development` (educacional, fila bypass)

Administrativos (exigem `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>`):

- `GET /api/agents`
- `GET /api/chats`
- `GET /api/chats/{phone_number}`
- `GET /api/metrics`
- `GET /api/queue` (também exposto pelo frontend como proxy autenticado via Better Auth)

## Decisões do harness

- PostgreSQL como fila: reduz moving parts no início e dá `FOR UPDATE SKIP LOCKED` de graça.
- API e Worker separados: isola latência da IA da borda HTTP.
- Multi-canal automático com `channel` por mensagem: permite cutover gradual entre provedores e operação simultânea sem feature flags.
- Loader dinâmico de agentes + catálogo: facilita extensibilidade (cada cliente vira pasta nova em `agents/catalog/`).
- Config centralizada com `validate_runtime_settings`: erro de configuração é fail-fast no boot, não erro silencioso em runtime.
- Memória por tools explícitas: separa contexto transiente (middleware) de memória durável (store).
- Deploy via Docker + Traefik + Let's Encrypt: VPS pequena, sem fornecedor proprietário, certificado automático.

## Próximos passos

- Implementar download de mídia inbound via Meta Graph API.
- Suporte a `NumMedia > 1` (Twilio) e mídia múltipla (uazapi).
- Templates outbound (HSM) para mensagens fora da janela de 24h.
- Endurecer rate limit em multi-instância (hoje é in-memory por processo).
- Telemetria padronizada (Prometheus/OpenTelemetry).
