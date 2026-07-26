# Primeiros Passos

Este guia tem duas trilhas:
- **Trilha A (agentes):** LangGraph Studio para desenvolver comportamento
- **Trilha B (harness):** API + Worker + DB para aprender arquitetura operacional

## Pré-requisitos

- Python 3.11+
- `uv` (gerenciador de pacotes)
- Docker + Docker Compose
- conta OpenRouter (API key)
- conta em **pelo menos um** canal de mensageria — só obrigatória para envio real; em dev local você pode rodar tudo em `OUTBOUND_MODE=mock`:
  - **Twilio** (sandbox WhatsApp) — ver [TWILIO.md](TWILIO.md)
  - **Meta** WhatsApp Cloud API — ver [META.md](META.md)
  - **uazapi/uazapiGO** — ver [UAZAPI.md](UAZAPI.md)

## 1. Setup local

```bash
git clone <repo-url>
cd whatsapp-langchain
make setup
cp .env.example .env
```

Edite `.env` e configure no mínimo:

```bash
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MIDIA_MODEL=google/gemini-2.5-flash-lite
INTERNAL_SERVICE_TOKEN=seu-token-local
BETTER_AUTH_SECRET=seu-secret-local
BETTER_AUTH_URL=http://localhost:3000
INTERNAL_API_URL=http://localhost:8000
OUTBOUND_MODE=mock
```

Para desenvolvimento local, basta preencher `INTERNAL_SERVICE_TOKEN` e
`BETTER_AUTH_SECRET` com valores não-vazios. Em production, ambos devem ter
32+ caracteres.

`OUTBOUND_MODE` é compartilhado entre os três canais (Twilio, Meta, uazapi).
`mock` simula o envio em todos eles (não precisa credenciais). `real` exige as
credenciais completas dos canais que você "tocar". `TWILIO_OUTBOUND_MODE` ainda
funciona como alias retrocompatível.

Se quiser validar envio real **pelo Twilio** no ambiente local:

```bash
OUTBOUND_MODE=real
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_API_KEY_SID=SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_API_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER=whatsapp:+14155238886

# Inbound (obrigatório apenas para validação real de assinatura)
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
VALIDATE_TWILIO_SIGNATURE=false
TWILIO_WEBHOOK_URL=
```

Para Meta ou uazapi, ver respectivamente [META.md](META.md) e [UAZAPI.md](UAZAPI.md).
Em `OUTBOUND_MODE=real`, API e Worker fazem fail-fast no boot se um canal estiver
"tocado parcialmente" (alguma credencial preenchida e outras vazias). Para
desabilitar um canal, **zere todas** as credenciais dele.

## 2. Trilha A: desenvolvimento de agente no Studio

```bash
make dev
# abre o LangGraph Studio
```

O catálogo atual em `langgraph.json` traz dois agentes:

- `illumi_assistant` — agente interno da Illumi (doutrina estratégica)
- `rhawk_assistant` — agente do cliente Top Hawks

Arquivos centrais de cada agente seguem o mesmo contrato (`agent.py`,
`prompts.py`, `graph.py`) em `src/whatsapp_langchain/agents/catalog/<id>/`.
Para criar um novo agente, ver [ADDING_AGENTS.md](ADDING_AGENTS.md) ou usar a
skill `create-agent`.

## 3. Trilha B: stack completo do harness

### Desenvolvimento do frontend sem subir a stack inteira

Se o objetivo for trabalhar no painel administrativo, nao e obrigatorio subir
`worker` ou a stack Docker completa.

#### Caso 1: revisar apenas `/login`

Suba apenas o banco e rode o frontend localmente:

```bash
make db
cd frontend
npm run dev
```

O login usa Better Auth + PostgreSQL diretamente, entao o banco precisa estar
de pe. A API nao e necessaria para essa tela.

Crie `frontend/.env.local` com:

```bash
INTERNAL_API_URL=http://localhost:8000
INTERNAL_SERVICE_TOKEN=dev-token-local
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/whatsapp_langchain
BETTER_AUTH_SECRET=dev-secret-local-1234567890
BETTER_AUTH_URL=http://localhost:3000
ENVIRONMENT=development
ADMIN_EMAIL=admin@localhost
ADMIN_PASSWORD=troque-esta-senha
```

Se `auth."user"` estiver vazio, o frontend cria automaticamente o primeiro
admin no acesso ao `/login` usando `ADMIN_EMAIL` e `ADMIN_PASSWORD`.
Depois, troque a senha em `/settings`.

#### Caso 2: revisar o painel completo (`/`, `/agents`, `/queue`, `/chats`)

Suba banco + API e rode o frontend localmente:

```bash
make db
make migrate   # necessario apenas em banco novo ou apos reset
INTERNAL_SERVICE_TOKEN=dev-token-local make api
```

Em outro terminal:

```bash
cd frontend
npm run dev
```

Nesse fluxo:
- `db` e obrigatorio para Better Auth
- `api` e obrigatoria para metricas, agentes, fila e conversas
- `worker` continua opcional para desenvolvimento visual

> `frontend/.env.local` e apenas para desenvolvimento local com `npm run dev`.
> Docker Compose e ambientes publicados usam as variaveis de ambiente do
> proprio servico, nao esse arquivo.

### Subir serviços

```bash
make up
```

Isso sobe:
- `db` (PostgreSQL + pgvector)
- `api` (FastAPI)
- `worker` (consumidor da fila; em dev usa Twilio mock por default)
- `frontend` (painel administrativo)

> O worker faz fail-fast no boot quando `OUTBOUND_MODE=real` e algum canal está
> "tocado parcialmente" (ex: `META_VERIFY_TOKEN` preenchido mas `META_PHONE_NUMBER_ID` vazio).
> Para webhook público em dev local (cloudflared, ngrok), siga
> [TWILIO.md](TWILIO.md) (Twilio), [META.md](META.md) (Meta) ou
> [UAZAPI.md](UAZAPI.md) (uazapi).

### Reset completo do ambiente Docker

Para reiniciar do zero (incluindo volume do PostgreSQL e dados):

```bash
make reset
```

### Validar saúde

```bash
curl http://localhost:8000/health
```

### Ver logs

```bash
make logs
```

## 4. Testes de fluxo

### 4.1 Endpoint síncrono (didático)

```bash
curl -X POST "http://localhost:8000/webhook/sync?agent=rhawk_assistant" \
  -H "Content-Type: application/json" \
  -d '{"phone":"+5511999999999","message":"Me explique debounce"}'
```

Use para debugging rápido sem fila.

### 4.2 Webhook assíncrono (arquitetura real)

```bash
curl -X POST "http://localhost:8000/webhook/twilio?agent=rhawk_assistant" \
  -d "MessageSid=SM123" \
  -d "From=whatsapp:+5511999999999" \
  -d "To=whatsapp:+14155238886" \
  -d "Body=Mensagem de teste" \
  -d "NumMedia=0"
```

Depois consulte:

```bash
curl -H "Authorization: Bearer <seu_INTERNAL_SERVICE_TOKEN>" http://localhost:8000/api/metrics
curl -H "Authorization: Bearer <seu_INTERNAL_SERVICE_TOKEN>" http://localhost:8000/api/chats
curl -H "Authorization: Bearer <seu_INTERNAL_SERVICE_TOKEN>" http://localhost:8000/api/chats/+5511999999999
```

### 4.2.1 Teste manual no Swagger (`/docs`)

1. Abra `http://localhost:8000/docs`.
2. Execute `GET /api/agents` e confirme `rhawk_assistant`.
3. Abra `POST /webhook/twilio` e clique em `Try it out`.
4. Preencha:
   - `agent` (query): `rhawk_assistant`
   - `MessageSid`: `SMDOCS001`
   - `From`: `whatsapp:+5511999999999`
   - `To`: `whatsapp:+14155238886`
   - `Body`: `Mensagem de teste via Swagger`
   - `NumMedia`: `0`
5. Execute e verifique:
   - resposta `200` com TwiML vazio
   - dados em `GET /api/chats/+5511999999999`

### 4.3 Teste de memória semântica (save + recall via tools)

1. Envie uma mensagem pedindo para salvar um fato:

```bash
curl -X POST "http://localhost:8000/webhook/twilio?agent=rhawk_assistant" \
  -d "MessageSid=SMMEM001" \
  -d "From=whatsapp:+5511999999999" \
  -d "To=whatsapp:+14155238886" \
  -d "Body=Use a ferramenta save_memory e salve este fato: meu código é codex-12345" \
  -d "NumMedia=0"
```

2. Envie outra mensagem pedindo recall explícito:

```bash
curl -X POST "http://localhost:8000/webhook/twilio?agent=rhawk_assistant" \
  -d "MessageSid=SMMEM002" \
  -d "From=whatsapp:+5511999999999" \
  -d "To=whatsapp:+14155238886" \
  -d "Body=Sem salvar nada novo agora, use read_memory e me diga meu código" \
  -d "NumMedia=0"
```

3. Verifique evidências no banco:

```sql
SELECT prefix, value->>'memory' AS memory, updated_at
FROM store
WHERE prefix = '+5511999999999.memories'
ORDER BY updated_at DESC;

SELECT id, message_id, status, response
FROM message_queue
WHERE phone_number = '+5511999999999'
ORDER BY id DESC
LIMIT 5;
```

## 5. Configurações importantes (.env)

### Contexto

```bash
CONTEXT_STRATEGY=trim            # trim | summarize | none
TRIM_KEEP_TURNS=5
SUMMARIZE_TRIGGER_TOKENS=4000
SUMMARIZE_KEEP_MESSAGES=10
SUMMARIZE_MODEL=x-ai/grok-4.1-fast
```

### Memória semântica

```bash
MEMORY_ENABLED=true
EMBEDDING_MODEL=openai/text-embedding-3-small
EMBEDDING_DIMS=1536
MEMORY_SEARCH_LIMIT=5
```

`EMBEDDING_MODEL` e `EMBEDDING_DIMS` precisam bater entre si. Os defaults acima
são os recomendados (mesmo modelo OpenRouter dos exemplos no `.env.example`).
Se mudar um, mude o outro junto e re-bootstrap o store.

### Operação da fila

```bash
MESSAGE_BUFFER_SECONDS=2.0
POLL_INTERVAL_SECONDS=1.0
LEASE_SECONDS=60
MAX_ATTEMPTS=3
RATE_LIMIT_PER_HOUR=30
```

## 6. Qualidade e testes

```bash
make test
make check
```

Comandos úteis:

```bash
make test-x
make test-v
make lint
make format
make typecheck
```

### Testes demonstrativos (com Docker)

Esses testes validam features de demonstração (imagem, áudio e memória semântica)
no fluxo real da stack Docker.

```bash
make test-demo
# ou:
make test-demo-up
```

## 7. Troubleshooting

### `OPENROUTER_API_KEY` ausente

```bash
grep OPENROUTER_API_KEY .env
```

### API sem conectar no banco

- confira `DATABASE_URL` no `.env`
- se estiver em Docker, lembre que API/Worker usam host `db` via `docker-compose.yml`

### Worker não processa mensagens

- verifique se o serviço `worker` está rodando (`make logs`)
- confira se há mensagens `queued` e `process_after <= now()`
- valide se o agente passado em `agent=` existe no catálogo

### Mídia não transcreve/processa

- confirme `MEDIA_IMAGE_ENABLED` / `MEDIA_AUDIO_ENABLED`
- confira se há chave OpenRouter válida
- verifique logs de `worker.media`

## Próximos passos

- [Arquitetura](ARCHITECTURE.md) — visão multi-canal
- [Criando Agentes](ADDING_AGENTS.md)
- [Banco de Dados](DATABASE.md)
- [Deploy](DEPLOY.md) — Docker + Traefik (oficial) ou Railway
- Canais: [Twilio](TWILIO.md) · [Meta](META.md) · [uazapi](UAZAPI.md)
