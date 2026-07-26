# whatsapp-langchain — contexto do projeto

Este arquivo é carregado automaticamente em toda sessão Claude Code dentro deste repositório. Use o **agente especialista** (`.claude/agents/whatsapp-langchain-specialist.md`) e as **skills** em `.claude/skills/` para tarefas específicas — eles têm a profundidade.

## O que é

Harness production-ready que conecta agentes LangGraph ao WhatsApp via Twilio, Meta WhatsApp Cloud API ou uazapi (uazapiGO).

```
WhatsApp (Twilio | Meta | uazapi) → API FastAPI → PostgreSQL (message_queue) → Worker → LangGraph Agent → resposta no canal de origem
```

Projeto em **português brasileiro**. É um **template** — cada novo cliente herda este repositório, customiza um agente do catálogo, configura `DOMAIN` e deploya. Marca da casa: **illumi** (logos em `Logotipo e assinaturas illumi/`, componente `frontend/src/components/illumi-logo.tsx`).

## Stack

| Camada | Tech |
|---|---|
| API | FastAPI 0.129 + uvicorn (Python 3.11+, gerenciado por `uv`) |
| Worker | asyncio + psycopg async pool |
| Agentes | LangGraph 1.0.7 + LangChain 1.2.8 |
| LLM | OpenRouter — chat (`OPENROUTER_MODEL`), multimodal para imagem/áudio (`OPENROUTER_MIDIA_MODEL`) e embeddings (`EMBEDDING_MODEL`) — uma única API key |
| Banco | PostgreSQL 16 + pgvector (checkpointer LangGraph + store semântico + schema `auth` do Better Auth). Em produção, **não exposto externamente** — visualização web via `pgweb` em `https://${DOMAIN}/banco/` (BasicAuth, modo `--lock-session` com `PGWEB_DATABASE_URL` pré-configurada — abre direto na lista de tabelas) ou SSH tunnel para acesso direto |
| Frontend | Next.js 16 + Better Auth (admin panel: `/login`, `/queue`, `/chats`, `/chats/[phone]`, `/agents`, `/settings`) |
| Inbound/Outbound WhatsApp | Twilio (HMAC-SHA1), Meta WhatsApp Cloud API (HMAC-SHA256) **ou** uazapi (uazapiGO, baseada em Baileys — token da instância via payload). Cada mensagem carrega `channel` na fila; o worker mantém um cliente por canal habilitado e roteia automaticamente. |
| Deploy produção | Docker + Traefik **v3.6.15** + Let's Encrypt (`deploy/`) — v3.5 não funciona com Docker ≥25 |
| Deploy alternativo | Railway (`docs/RAILWAY.md`) |

## Estrutura

```
src/whatsapp_langchain/
├── server/        # FastAPI: webhooks (/webhook/twilio, /webhook/meta, /webhook/uazapi, /webhook/sync) + admin (/api/*) + /health
├── worker/        # consumer (FOR UPDATE SKIP LOCKED + lease + retry) + processor +
│                  # clientes outbound (twilio_client, meta_client, uazapi_client) + media preprocessor
├── agents/        # catálogo, middleware de contexto, tools de memória
│   ├── catalog/
│   │   ├── illumi_assistant/    # agente da própria Illumi (doutrina estratégica interna)
│   │   └── rhawk_assistant/     # agente da comunidade Top Hawks (cliente)
│   ├── middleware/              # trim | summarize | none (CONTEXT_STRATEGY)
│   └── tools/                   # save_memory, read_memory
└── shared/        # config (Settings + channel_status/validate_runtime_settings),
                   # db pool, queue ops, models (MessagingChannel), llm factory, structlog

db/migrations/     # 001_initial → 006_message_channel (rodam no startup da API automaticamente)
frontend/          # Next.js 16 admin panel (Better Auth, fetch server-side da API; proxy.ts faz auth gate)
deploy/            # docker-compose.prod.yml + Traefik v3.6.15 + scripts (template para VPS)
docs/              # ARCHITECTURE, GETTING_STARTED, ADDING_AGENTS, DATABASE, TWILIO, META, UAZAPI, DEPLOY, RAILWAY, STRESS_TESTING
.claude/           # agente especialista + 9 skills (este arquivo aponta pra lá)
patch/             # snapshots por Fase_2/3/4 — não tocar a menos que pedido
stress/            # Locust setup
tests/             # unit/ + integration/ (pytest, asyncio_mode=auto)
```

## Convenções inegociáveis

1. **`uv` é o gerenciador de deps** — `uv add`, `uv lock`, nunca `pip install` direto. `uv.lock` é fonte de verdade.
2. **Migrações rodam automaticamente no startup da API** (`run_migrations` + `bootstrap_langgraph_schema`). Não rodar `db/migrate.py` manualmente em produção a menos que necessário.
3. **`.env`/`.env.prod` são SEGREDOS** — gitignored, nunca printar valores.
4. **API e Worker compartilham `src/shared/`** — mudança ali afeta os dois containers.
5. **`thread_id = "{phone}:{agent_id}"`** — é a chave do checkpointer LangGraph.
6. **`user_id = phone_number`** — é a chave do store semântico (memória cross-thread).
7. **`/api/*` da API é protegido por `INTERNAL_SERVICE_TOKEN`** (compartilhado com o frontend). Apenas `/api/agents`, `/api/chats`, `/api/metrics` (e `/webhook/*`, `/health`) são roteados pelo Traefik para a API (priority 100); `/banco` vai para `pgweb` (priority 90); demais paths caem no catchall do frontend (priority 10).
8. **`/api/auth/*` pertence ao Better Auth no frontend**, NÃO à API. Não confundir. O frontend também expõe `/api/queue` como proxy autenticado por sessão Better Auth, que valida o cookie e chama o `/api/queue` da API internamente via `INTERNAL_API_URL` — chamadas externas a `https://DOMAIN/api/queue` caem nesse proxy do Next.js, não na API direto.
9. **`OUTBOUND_MODE=mock` em dev**, `real` em produção. Em modo `real`, `settings.validate_runtime_settings()` no boot da API e do Worker faz fail-fast se algum canal está parcialmente configurado (toque parcial) ou se `INTERNAL_SERVICE_TOKEN` é fraco/ausente.
10. **Em produção, validação de assinatura ATIVA** — Twilio (`VALIDATE_TWILIO_SIGNATURE=true` + `TWILIO_AUTH_TOKEN` + `TWILIO_WEBHOOK_URL`) ou Meta (`META_VALIDATE_SIGNATURE=true` + `META_APP_SECRET`). uazapi não assina o body — o próprio token da instância vem no payload e é o que autentica o outbound; restrinja por IP no Traefik se quiser hardening adicional.
11. **Roteamento por canal é automático** — cada webhook (`/webhook/twilio`, `/webhook/meta`, `/webhook/uazapi`) grava `message_queue.channel` ao enfileirar; o worker mantém clientes outbound dos canais habilitados (todos os com credenciais preenchidas) e seleciona o cliente pelo `channel` da mensagem. Não há env `MESSAGING_CHANNEL`. Canal "tocado parcialmente" no `.env.prod` em modo real → fail-fast no boot. Para uazapi, o token da instância chega via webhook por mensagem e é persistido em `message_queue.outbound_token` (migração `005_uazapi_outbound_token.sql`).
12. **Agentes vivem em `src/whatsapp_langchain/agents/catalog/<id>/`** — registrados em `langgraph.json`, selecionados via `?agent=<id>` na query string do webhook. Catálogo atual: **`illumi_assistant`** (interno Illumi, prompt = doutrina estratégica) e **`rhawk_assistant`** (cliente Top Hawks). Para criar mais → skill `create-agent`.
13. **Postgres NÃO é exposto publicamente** em produção. Acesso ao banco é via:
    - **`pgweb` em `https://${DOMAIN}/banco`** (visualizador web, BasicAuth via `PGWEB_AUTH_USER`/`PGWEB_AUTH_PASS` no `.env.prod`) — caminho oficial.
    - **SSH tunnel** para acesso direto (DBeaver, psql): `ssh -L 5432:localhost:5432 user@vps` (eventualmente exigindo bind manual `127.0.0.1:5432:5432` no compose).
14. **Não criar comentários óbvios** nem docstrings longas — o código é didático e bem nomeado por design.

## Comandos comuns (Makefile)

```bash
make help          # lista todos
make setup         # uv venv + install deps
make up / down     # docker compose dev (db + api + worker + frontend)
make api / worker / frontend  # rodar serviço local fora do Docker
make migrate       # aplica migrações SQL (script standalone)
make dev           # LangGraph Studio (porta 8123) para iterar em agentes
make test          # pytest sem docker_demo
make test-demo-up  # sobe Docker e roda testes integrados
make check         # lint + format-check + typecheck (não altera arquivos)
make ci            # check + testes
```

## Onde está cada coisa

| Quero... | Vá para |
|---|---|
| Entender arquitetura | `docs/ARCHITECTURE.md` |
| Setup local primeira vez | `docs/GETTING_STARTED.md` |
| Criar novo agente | skill `create-agent` (e `docs/ADDING_AGENTS.md`) |
| Schema do banco e queries | `docs/DATABASE.md` |
| Configurar Twilio | skill `twilio-setup` (e `docs/TWILIO.md`) |
| Configurar Meta WhatsApp Cloud API | skill `meta-setup` |
| Configurar/entender uazapi | `docs/UAZAPI.md` + `webhook_uazapi.py` |
| Deploy em VPS (Docker+Traefik) | `deploy/README.md` + skills `infra-setup`/`domain-setup`/`deploy` |
| Deploy em Railway | `docs/RAILWAY.md` |
| Debugar fila travada | skill `debug-queue` |
| Stress test | skill `stress-test` (e `docs/STRESS_TESTING.md`) |
| Mexer no UI/design do admin panel | skill `ui-ux-pro-max` |
| Variáveis de ambiente | `.env.example` (dev) / `deploy/.env.prod.example` (prod) |

## Skills disponíveis (`.claude/skills/`)

Invoque pedindo coisas como "preciso fazer deploy", "criar agente para o cliente X", "fila travou":

- **`infra-setup`** — VPS nova: instala Docker, configura firewall, prepara acme.json
- **`domain-setup`** — preencher `DOMAIN`, validar DNS, preparar para Let's Encrypt
- **`deploy`** — `bash deploy/scripts/deploy.sh` + healthchecks + smoke test
- **`twilio-setup`** — webhook Twilio (sandbox ou produção real), API Keys, validação de assinatura
- **`meta-setup`** — webhook Meta WhatsApp Cloud API (Verify Token, App Secret, Phone Number ID, System User Token, validação X-Hub-Signature-256)
- **`create-agent`** — novo agente LangGraph no catálogo
- **`debug-queue`** — queries SQL prontas para `message_queue`, recovery seguro
- **`stress-test`** — Locust + análise + reverter ajustes pós-teste
- **`ui-ux-pro-max`** — design system, paletas, componentes shadcn/ui para o admin panel Next.js

## Quando o usuário pede deploy

Antes de tocar em produção, sempre confirme:
1. Está numa VPS? (`uname`, `hostname`)
2. Docker disponível? Se não → skill `infra-setup`
3. `deploy/.env.prod` preenchido? (`DOMAIN`, `LETSENCRYPT_EMAIL`, `POSTGRES_PASSWORD`, `PGWEB_AUTH_USER`, `PGWEB_AUTH_PASS`, `OPENROUTER_API_KEY`, `INTERNAL_SERVICE_TOKEN`, `BETTER_AUTH_SECRET`, `ADMIN_EMAIL`/`ADMIN_PASSWORD`)
4. DNS de `DOMAIN` aponta para o IP desta VPS? (`dig +short $DOMAIN @1.1.1.1`)

Falhou algum check → invoque a skill apropriada antes de `deploy`.

## Estilo de resposta

- Português brasileiro, direto e técnico.
- Curto. Ações > narração.
- Cite `arquivo:linha` quando indicar mudanças.
- Confirme antes de operações destrutivas (down de produção, drop de tabela, force-renew SSL).
- Para tarefas com 3+ passos, use TaskCreate.

## Git / commits e push (autorizado em CLAUDE.md)

Autorização durável: **ao final de cada alteração coerente, commitar e fazer
`git push origin main` automaticamente — sem pedir confirmação por padrão**. É
o fluxo deste repositório.

Regras:

- Mensagens em **português brasileiro**, padrão Conventional Commits
  (`docs:`, `feat:`, `fix:`, `chore:`, `deploy:`, `chore(skills):`, etc.).
- Sempre incluir `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- **Agrupar mudanças em commits temáticos** quando há múltiplos temas no working
  tree (ex: `docs:` separado de `deploy:` separado de `chore(skills):`). Não
  juntar tudo num commit gigante.
- Stagger por arquivos específicos (`git add <paths>`), não usar `git add -A`/`.`
  para evitar incluir sem querer arquivos sensíveis ou em progresso.
- Push direto para `origin main` é o padrão.

Continua exigindo **confirmação explícita** antes de:

- `git push --force` / `git push --force-with-lease`
- `git reset --hard` / `git checkout --` em arquivos modificados
- `git rebase` de commits já publicados em `origin main`
- Deleção de branches publicados (`git push origin --delete <branch>`)
- Skip de hooks (`--no-verify`, `--no-gpg-sign`)
- Qualquer outra operação que reescreva histórico publicado

Se o working tree estiver com modificações não relacionadas à tarefa em
andamento, **não** commitar tudo cego — perguntar antes de englobar arquivos
que parecem trabalho em progresso de outra mudança.
