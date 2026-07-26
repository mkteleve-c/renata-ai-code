# Deploy

Este projeto suporta dois caminhos de deploy:

| Caminho | Quando usar | Doc canônico |
|---|---|---|
| **Docker + Traefik + Let's Encrypt** (oficial) | VPS Debian/Ubuntu própria, controle total, TLS automático, multi-canal Twilio/Meta/uazapi no mesmo domínio | [`deploy/README.md`](../deploy/README.md) |
| **Railway** (alternativo) | Provisionamento gerenciado, sem dor de servidor, foco em iteração rápida | [`docs/RAILWAY.md`](RAILWAY.md) |

Os dois rodam o mesmo código. A diferença é a infra abaixo.

## O que está pronto

- Webhooks paralelos para os três canais — Twilio, Meta WhatsApp Cloud API e uazapi/uazapiGO. O canal de cada mensagem é gravado em `message_queue.channel` e o worker seleciona o cliente outbound automaticamente.
- API FastAPI pública (`/health` + os 3 webhooks + admin protegido por `INTERNAL_SERVICE_TOKEN`).
- Worker assíncrono com clientes outbound dos canais habilitados.
- Frontend Next.js com Better Auth (`/login`, `/queue`, `/chats`, `/agents`, `/settings`).
- PostgreSQL 16 com pgvector (checkpointer LangGraph + store semântico + schema `auth`).
- pgweb em `/banco` (BasicAuth) para visualização do banco em produção — Postgres não é exposto publicamente.
- Stress testing com Locust documentado.

## Topologia (Docker + Traefik)

```text
                      Internet
                         │
                ┌────────┴────────┐
                │ Traefik :80/:443│  TLS automático Let's Encrypt
                │ Host=${DOMAIN}  │  HTTP-01 challenge na :80
                └─┬──────┬──────┬─┘
                  │      │      │
   /webhook/*     │      │      │   resto (UI + /api/auth/*)
   /health        │      │      │   = catchall
   /api/agents    │      │      │
   /api/chats     │      │      │
   /api/metrics   ▼      ▼      ▼
                ┌────┐ ┌─────┐ ┌──────────┐
                │api │ │pgweb│ │ frontend │
                │8000│ │8081 │ │  3000    │
                └─┬──┘ └─┬───┘ └─┬────────┘
                  │      │       │
                  ▼      ▼       ▼
              ┌──────────────────────┐
              │ db (Postgres+pgvector)│  não exposto
              │ acesso só via pgweb  │  publicamente
              │ ou SSH tunnel        │
              └──────────────────────┘
                       ▲
                       │
                  ┌────┴────┐
                  │ worker  │ consome message_queue,
                  └─────────┘ envia outbound pelo canal certo
```

Twilio, Meta e uazapi entram pelo **mesmo `${DOMAIN}`**, paths distintos:

- `https://${DOMAIN}/webhook/twilio?agent=<id>`
- `https://${DOMAIN}/webhook/meta?agent=<id>` (GET = handshake; POST = mensagens)
- `https://${DOMAIN}/webhook/uazapi?agent=<id>`

## Variáveis essenciais por serviço

### API

- `DATABASE_URL`
- `ENVIRONMENT=production`
- `LOG_JSON=true`
- `OPENROUTER_API_KEY` (necessário no boot — `bootstrap_langgraph_schema` cria o store de embeddings)
- `OPENROUTER_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMS`, `MEMORY_ENABLED`
- `INTERNAL_SERVICE_TOKEN` (forte, 32+ chars em produção)
- Por canal habilitado:
  - Twilio inbound: `VALIDATE_TWILIO_SIGNATURE=true`, `TWILIO_AUTH_TOKEN`, `TWILIO_WEBHOOK_URL` (=`https://${DOMAIN}` no deploy Docker)
  - Meta inbound: `META_VERIFY_TOKEN`, `META_VALIDATE_SIGNATURE=true`, `META_APP_SECRET`
  - uazapi inbound: nenhuma — assinatura não existe; opcional `UAZAPI_INSTANCE_TOKEN` como fallback

### Worker

- `DATABASE_URL`, `ENVIRONMENT`, `LOG_JSON`
- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_MIDIA_MODEL`, `OPENROUTER_BASE_URL`
- `OUTBOUND_MODE=real` (em produção)
- Por canal habilitado:
  - Twilio: `TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_FROM_NUMBER`
  - Meta: `META_PHONE_NUMBER_ID`, `META_ACCESS_TOKEN`, `META_GRAPH_API_VERSION` (default `v23.0`)
  - uazapi: `UAZAPI_BASE_URL` (token vem por mensagem; opcional `UAZAPI_INSTANCE_TOKEN` como fallback)

> Em `OUTBOUND_MODE=real`, API e Worker fazem fail-fast no boot se um canal está
> "tocado parcialmente" (alguma credencial preenchida e outras vazias). Para
> desabilitar um canal, **zere todas** as credenciais dele.

### Frontend

- `DATABASE_URL` (Better Auth lê schema `auth`)
- `INTERNAL_API_URL` (`http://api:8000` no compose Docker)
- `INTERNAL_SERVICE_TOKEN` (mesmo da API)
- `BETTER_AUTH_SECRET`, `BETTER_AUTH_URL` (=`https://${DOMAIN}`)
- `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` (bootstrap do primeiro admin)

## Fluxo recomendado de publicação (Docker + Traefik)

1. Provisionar VPS Debian/Ubuntu — skill `infra-setup` (instala Docker, libera firewall, cria `acme.json`).
2. Configurar DNS `A` apontando `${DOMAIN}` para o IP da VPS — skill `domain-setup`.
3. Preencher `deploy/.env.prod` (cópia de `.env.prod.example`) com `DOMAIN`, `LETSENCRYPT_EMAIL`, segredos fortes (`POSTGRES_PASSWORD`, `INTERNAL_SERVICE_TOKEN`, `BETTER_AUTH_SECRET`, `PGWEB_AUTH_PASS`), credenciais de **pelo menos um canal**.
4. Subir o stack: `bash deploy/scripts/deploy.sh` — skill `deploy`.
5. Cadastrar webhook no(s) provedor(es):
   - Twilio: skill `twilio-setup`
   - Meta: skill `meta-setup`
   - uazapi: configurar URL no painel da instância (ver [UAZAPI.md](UAZAPI.md))
6. Definir `ADMIN_EMAIL`/`ADMIN_PASSWORD`, acessar `/login`, validar bootstrap automático e trocar a senha em `/settings`.
7. Smoke tests — `/health`, `/login`, `/banco` (BasicAuth) e mensagem real em pelo menos um canal.

Para Railway, ver [`RAILWAY.md`](RAILWAY.md).

## Checklist de verificação

- `GET https://${DOMAIN}/health` responde `200`
- `/login` renderiza
- `/banco` retorna `401` sem auth e `200` com `PGWEB_AUTH_USER:PGWEB_AUTH_PASS`
- Para cada canal habilitado: webhook configurado e mensagem teste chega ao agente
- Request com assinatura inválida retorna `403` (Twilio e Meta com validação ativa)
- `message_queue` registra `queued -> processing -> done|failed` e o `channel` correto
- A resposta chega ao usuário final **antes** de `mark_done`
- Frontend acessa `/api/*` apenas via `INTERNAL_SERVICE_TOKEN`
- Não existe endpoint público de signup habilitado em produção
- `Settings.validate_runtime_settings` não derruba o boot — todos os canais "tocados" estão completos

## Cutover entre provedores

Como o roteamento é por mensagem (`message_queue.channel`), não há mais "canal ativo único". Habilitar/desabilitar é apenas preencher/zerar credenciais e re-`up -d`.

Exemplo Twilio sandbox → Meta produção (mantendo Twilio temporariamente):

1. Preencher os 4 envs `META_*` no `.env.prod`; deixar Twilio preenchido.
2. `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d` — worker recria com clientes Twilio + Meta.
3. Cadastrar webhook no painel do Meta (skill `meta-setup`).
4. Smoke test pelo número Meta.
5. Quando confortável, **desativar webhook no Twilio Console** (mensagens param de chegar em `/webhook/twilio`).
6. Para parar 100%: zerar credenciais Twilio e `up -d`.

Mensagens em voo na fila são processadas pelo cliente do `channel` que carregam — não há perda durante a transição enquanto os dois clientes estiverem habilitados.

## Rollback

Três níveis (Docker + Traefik):

**Nível 1 — rollback de versão.** `git checkout <tag-anterior>` + `bash deploy/scripts/deploy.sh`. Compose detecta diff e recria os containers afetados.

**Nível 2 — rollback de provedor.** Reverter as credenciais do canal no `.env.prod` e desativar o webhook no painel do provedor (Meta ou Twilio). uazapi: trocar token da instância no painel uazapi.

**Nível 3 — rollback de domínio/cert.** Em emergências, apagar `deploy/traefik/acme.json`, recriar com `chmod 600` e `up -d` força nova emissão. Atenção: Let's Encrypt tem rate limit de 50 certs/semana por domínio — não force sem necessidade.

Para Railway, ver [`RAILWAY.md`](RAILWAY.md#rollback).

## Notas operacionais

- Em `ENVIRONMENT=production`, o endpoint `/webhook/sync` fica desabilitado.
- `OUTBOUND_MODE=mock` é útil para dev local e stress testing sem custo real (e sem precisar credenciais).
- `TWILIO_OUTBOUND_MODE` continua sendo aceito como alias legacy — `OUTBOUND_MODE` é o nome novo, compartilhado entre canais.
- Em qualquer ambiente, API/Worker falham cedo se `INTERNAL_SERVICE_TOKEN` ou `BETTER_AUTH_SECRET` estiverem ausentes; em produção, exigem 32+ caracteres.
- Se `auth."user"` estiver vazio, o primeiro acesso ao `/login` cria o admin a partir de `ADMIN_EMAIL`/`ADMIN_PASSWORD`.

## Documentos relacionados

| Documento | Conteúdo |
|---|---|
| [`deploy/README.md`](../deploy/README.md) | **Deploy oficial** — quick-start completo Docker + Traefik para VPS limpa |
| [`RAILWAY.md`](RAILWAY.md) | Deploy alternativo no Railway (topologia, watch paths, reference variables) |
| [`TWILIO.md`](TWILIO.md) | Configuração Twilio detalhada (sandbox e produção) |
| [`META.md`](META.md) | Configuração Meta WhatsApp Cloud API |
| [`UAZAPI.md`](UAZAPI.md) | Configuração do canal uazapi/uazapiGO |
| [`DATABASE.md`](DATABASE.md) | Schema do banco e queries |
| [`STRESS_TESTING.md`](STRESS_TESTING.md) | Stress testing detalhado |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Visão geral da arquitetura multi-canal |
