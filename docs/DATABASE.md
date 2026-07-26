# Banco de Dados

Este documento explica as tabelas do projeto e traz queries de inspeção
para validação operacional (fila, conversa e memória semântica via tools).

## Como acessar o banco

Em **produção**, o Postgres não é exposto publicamente. Os caminhos são:

1. **pgweb em `https://${DOMAIN}/banco/`** (oficial, recomendado)

   Visualizador web open source rodando atrás do Traefik com TLS Let's Encrypt e
   protegido por BasicAuth (`PGWEB_AUTH_USER` / `PGWEB_AUTH_PASS` no `.env.prod`).
   Tem aba SQL para rodar as queries deste documento direto pelo browser.

   O container roda em modo `--lock-session` com `PGWEB_DATABASE_URL` pré-configurada:
   após o login BasicAuth, a sidebar já abre com as tabelas do banco — sem passar
   por tela de "Connect" intermediária. Os botões "Connect/Disconnect" no header da
   UI ficam inertes (backend bloqueia troca de DB).

2. **`docker compose exec` para `psql`** (debug profundo)

   ```bash
   cd deploy
   docker compose -f docker-compose.prod.yml --env-file .env.prod exec db \
     psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
   ```

3. **SSH tunnel para DBeaver/TablePlus** (cliente desktop)

   ```bash
   ssh -L 5432:localhost:5432 user@vps
   # No compose, é preciso bindar a porta em 127.0.0.1:5432:5432 — não está por padrão.
   ```

Em **dev**, o Postgres expõe `localhost:5432` via `docker-compose.yml` (não o `.prod.yml`).

## Visão Geral

O PostgreSQL guarda três blocos de dados:

1. Tabelas de aplicação (`message_queue`, `conversations`, `_migrations`)
2. Tabelas de checkpointer do LangGraph (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`, `checkpoint_migrations`)
3. Tabelas de memória semântica (`store`, `store_vectors`, `store_migrations`, `vector_migrations`)

## Tabelas de Aplicação

### `_migrations`

Controle das migrações SQL locais (`db/migrations/*.sql`).

### `message_queue`

Fila operacional de mensagens.

Campos principais:
- `message_id`: id externo (Twilio MessageSid, Meta wamid, ou messageid uazapi)
- `phone_number`, `agent_id`, `thread_id`
- `channel`: canal de origem (`twilio` | `meta` | `uazapi`) — gravado pelo webhook que recebeu a mensagem; usado pelo worker para escolher o cliente outbound (mig 006, default `'twilio'` para linhas pré-existentes)
- `outbound_token`: token outbound dinâmico — usado pelo cliente uazapi (vem no payload do webhook); `NULL` para Twilio/Meta (mig 005)
- `incoming_message`: entrada original
- `media_url`, `media_type`
- `normalized_input`: texto final enviado ao agente (quando houver)
- `media_processing_status`: `none | processed | disabled | failed | unsupported`
- `media_processing_error`: erro de pré-processamento de mídia
- `status`: `queued | processing | done | failed`
- `response`, `error`, `attempts`, `max_attempts`, `process_after`, `lease_until`

Índices relevantes: `idx_queue_polling` (`status, process_after, created_at`), `idx_queue_phone_agent`, `idx_message_queue_channel`.

### `conversations`

Resumo por conversa (`phone_number + agent_id`) para o painel/admin.

## Tabelas do LangGraph

### `checkpoints`

Snapshots do estado por `thread_id`.

### `checkpoint_writes`

Eventos incrementais por canal (inclui canal `messages`).
O payload fica em `blob` (msgpack).

### `checkpoint_blobs`

Blobs auxiliares do checkpointer.

### `checkpoint_migrations`

Controle interno de schema do checkpointer.

## Tabelas de Memória Semântica

### `store`

Memórias em JSON por namespace/prefix.
Para este projeto, padrão:
- `prefix = "<user_id>.memories"`
- sem namespaces de tenant (`tenant_user`/`tenant_shared`)

### `store_vectors`

Embeddings vetoriais da `store` (HNSW + `vector`).

### `store_migrations` e `vector_migrations`

Controle interno de schema da store vetorial.

## Queries Prontas

### 1) Quais formatos de mídia chegaram

```sql
SELECT media_type, COUNT(*) AS total
FROM message_queue
WHERE media_type IS NOT NULL
GROUP BY media_type
ORDER BY total DESC;
```

### 1b) Distribuição de mensagens por canal

```sql
SELECT channel, status, COUNT(*) AS total
FROM message_queue
GROUP BY channel, status
ORDER BY channel, status;
```

### 2) Histórico completo de uma conversa (fila + resposta)

```sql
SELECT
  id,
  message_id,
  phone_number,
  channel,
  media_type,
  media_processing_status,
  status,
  incoming_message,
  normalized_input,
  response,
  media_processing_error,
  error,
  created_at,
  processed_at
FROM message_queue
WHERE phone_number = '+5511999999999'
ORDER BY id DESC;
```

### 3) Memórias salvas de um usuário

```sql
SELECT
  prefix,
  key,
  value->>'memory' AS memory,
  created_at
FROM store
WHERE prefix = '+5511999999999.memories'
ORDER BY created_at DESC;
```

### 4) Evidência de save no store (memória durável por usuário)

```sql
SELECT
  prefix,
  key,
  value->>'memory' AS memory,
  updated_at
FROM store
WHERE prefix = '+5511999999999.memories'
ORDER BY updated_at DESC
LIMIT 20;
```

### 5) Evidência de recall no output (resposta final ao usuário)

```sql
SELECT
  id,
  message_id,
  phone_number,
  status,
  left(response, 220) AS response_preview,
  created_at
FROM message_queue
WHERE phone_number = '+5511999999999'
  AND status = 'done'
ORDER BY id DESC
LIMIT 20;
```

### 6) Inspeção técnica das mensagens persistidas no checkpoint

```sql
SELECT
  checkpoint_id,
  channel,
  type,
  octet_length(blob) AS bytes,
  left(encode(blob, 'escape'), 600) AS blob_preview
FROM checkpoint_writes
WHERE thread_id = '+5511999999999:rhawk_assistant'
  AND channel = 'messages'
ORDER BY checkpoint_id DESC
LIMIT 20;
```

### 7) Conversas mais recentes (visão painel)

```sql
SELECT
  phone_number,
  agent_id,
  thread_id,
  last_message,
  last_message_at,
  message_count
FROM conversations
ORDER BY last_message_at DESC
LIMIT 50;
```

## Observações

- `conversations` mostra apenas resumo; não mostra detalhes de save/recall.
- Save de memória é observado em `store` (`prefix = "<user_id>.memories"`).
- Recall é observado no output final (`message_queue.response`) e nos logs do worker (`memory_read`).
- `message_queue.status='done'` significa ciclo encerrado com resposta ao usuário,
  inclusive respostas automáticas quando mídia está desabilitada ou falha.
