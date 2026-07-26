# Deploy — Docker + Traefik

Stack de produção self-hosted para qualquer VPS Debian/Ubuntu, com TLS automático via Let's Encrypt.

Esta pasta é o **template de deploy** do projeto. Cada novo cliente herda este template e só muda a variável `DOMAIN` (e segredos) no `.env.prod`.

## O que sobe

```
                  ┌─────────────────────────┐
   Internet  ───▶ │  Traefik :80/:443       │  TLS Let's Encrypt
                  │  (reverse proxy)        │
                  └────┬─────────┬─────────┬┘
                       │         │         │
              path-routing por Host(${DOMAIN}):
                       │         │         │
       /webhook/*      │         │  /banco │  resto (UI + /api/auth/*)
       /health         │         │         │
       /api/agents/*   ▼         ▼         ▼
       /api/chats/*  ┌──────┐ ┌────────┐ ┌──────────┐
       /api/metrics  │ api  │ │ pgweb  │ │ frontend │
                     │ :8000│ │ :8081  │ │ :3000    │
                     └───┬──┘ └────┬───┘ └────┬─────┘
                         │         │          │
                         ▼         ▼          ▼
                       ┌──────────────┐
                       │  postgres    │  (não exposto publicamente)
                       │  + pgvector  │
                       └──────────────┘
                              ▲
                              │
                       ┌──────┴───┐
                       │  worker  │  (consome message_queue)
                       └──────────┘
```

## Arquivos desta pasta

```
deploy/
├── README.md                    # este guia
├── docker-compose.prod.yml      # stack de produção
├── .env.prod.example            # variáveis (copiar para .env.prod)
├── traefik/
│   └── acme.json                # storage Let's Encrypt (criado pelo init-vps.sh, gitignored)
└── scripts/
    ├── init-vps.sh              # prepara VPS limpa (instala Docker, configura firewall, cria acme.json)
    └── deploy.sh                # build + up + status (idempotente)
```

## Quick start (VPS limpa)

### 1. Clone o repositório na VPS

```bash
git clone <url-do-repo> ~/whatsapp-langchain
cd ~/whatsapp-langchain
```

### 2. Prepare a infraestrutura

```bash
bash deploy/scripts/init-vps.sh
```

Instala Docker + Compose plugin, libera portas no UFW (se instalado), cria `acme.json` com permissão correta.

### 3. Configure DNS

No painel do seu provedor de DNS, crie um registro A:

| Tipo | Nome | Valor | TTL | Proxy |
|---|---|---|---|---|
| A | `cliente1.vps.illumiai.com` | `<IP da VPS>` | 300 | **Desligado** |

> Se usa Cloudflare, deixe o ícone **cinza** (não laranja). Proxy quebra o desafio HTTP-01 do Let's Encrypt.

Confirme propagação:

```bash
dig +short cliente1.vps.illumiai.com @1.1.1.1
# Deve retornar o IP da VPS
```

### 4. Preencha o `.env.prod`

```bash
cd deploy
cp .env.prod.example .env.prod
nano .env.prod
```

Variáveis obrigatórias:

| Variável | Como obter |
|---|---|
| `DOMAIN` | O subdomínio configurado no DNS |
| `LETSENCRYPT_EMAIL` | Seu e-mail (alertas de expiração) |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` (HEX evita `/=+` que quebram a URL) |
| `INTERNAL_SERVICE_TOKEN` | `openssl rand -base64 32` |
| `BETTER_AUTH_SECRET` | `openssl rand -base64 32` |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys |
| `PGWEB_AUTH_USER` | Login do visualizador `/banco` (ex: `admin`) |
| `PGWEB_AUTH_PASS` | `openssl rand -base64 24` |
| `TWILIO_ACCOUNT_SID` | Twilio Console → Account Info |
| `TWILIO_API_KEY_SID` | Twilio Console → API Keys |
| `TWILIO_API_KEY_SECRET` | Idem (mostrado uma vez) |
| `TWILIO_AUTH_TOKEN` | Twilio Console → Account Info |
| `TWILIO_FROM_NUMBER` | `whatsapp:+14155238886` (sandbox) ou seu número real |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Primeiro admin do painel |

### 5. Suba o stack

```bash
bash deploy/scripts/deploy.sh
```

O primeiro deploy demora ~3–5 min (build de 4 imagens). Aguarde Let's Encrypt emitir o cert (~30s após o `up`).

### 6. Smoke test

Use **GET** (não HEAD) — pgweb não suporta HEAD em rotas da UI e retorna 404 mesmo com tudo OK.

```bash
DOMAIN=$(grep ^DOMAIN= deploy/.env.prod | cut -d= -f2-)
PGUSER=$(grep ^PGWEB_AUTH_USER= deploy/.env.prod | cut -d= -f2-)
PGPASS=$(grep ^PGWEB_AUTH_PASS= deploy/.env.prod | cut -d= -f2-)

curl -s https://$DOMAIN/health                                # {"status":"ok",...}
curl -s -o /dev/null -w "%{http_code}\n" https://$DOMAIN/login            # 200
curl -s -o /dev/null -w "%{http_code}\n" https://$DOMAIN/banco/           # 401 (sem auth)
curl -s -o /dev/null -w "%{http_code}\n" -u "$PGUSER:$PGPASS" https://$DOMAIN/banco/  # 200
```

Esperado: `200` em `/health`, `/login` e `/banco/` com auth; `401` em `/banco/` sem auth.

### 7. Configure o canal de WhatsApp

Escolha um canal e cadastre o webhook correspondente:

| Canal | URL do webhook | Skill com detalhes |
|---|---|---|
| Twilio | `https://${DOMAIN}/webhook/twilio?agent=rhawk_assistant` | `.claude/skills/twilio-setup/SKILL.md` ou `docs/TWILIO.md` |
| Meta WhatsApp Cloud API | `https://${DOMAIN}/webhook/meta?agent=rhawk_assistant` | `.claude/skills/meta-setup/SKILL.md` |
| uazapi (uazapiGO) | `https://${DOMAIN}/webhook/uazapi?agent=rhawk_assistant` | `docs/UAZAPI.md` |

(Substitua `rhawk_assistant` pelo agente que você cadastrou em `langgraph.json`.)

Os canais coexistem — o roteamento é automático (cada webhook grava `channel` na fila). Habilitar = preencher credenciais; desabilitar = zerar credenciais.

### 8. Crie o admin e teste

```
https://cliente1.vps.illumiai.com/login
```

O primeiro acesso cria o admin com `ADMIN_EMAIL`/`ADMIN_PASSWORD`. Troque a senha em `/settings`.

Envie uma mensagem WhatsApp para o número configurado e veja a resposta.

## Roteamento Traefik (path-based, único subdomínio)

Por que tudo num só subdomínio? Porque o usuário precisa de **uma URL única** para colocar no Twilio/Meta como webhook. Path-based routing resolve sem precisar de 2 DNS records.

| URL | Vai para | Prioridade |
|---|---|---|
| `https://${DOMAIN}/webhook/*` | API (FastAPI) | 100 |
| `https://${DOMAIN}/health` | API | 100 |
| `https://${DOMAIN}/api/agents` | API | 100 |
| `https://${DOMAIN}/api/chats/*` | API | 100 |
| `https://${DOMAIN}/api/metrics` | API | 100 |
| `https://${DOMAIN}/banco/*` | pgweb (BasicAuth) | 90 |
| `https://${DOMAIN}/*` (resto) | Frontend (Next.js) | 10 |

**Importante**: `/api/auth/*` (Better Auth no frontend) cai no catchall do frontend — não colide com os endpoints administrativos da API porque eles têm paths específicos (`/api/agents`, `/api/chats`, `/api/metrics`).

Se precisar trocar para subdomínios separados (ex: `api.${DOMAIN}` + `${DOMAIN}`), edite as labels do `docker-compose.prod.yml`:

```yaml
# Em api:
- "traefik.http.routers.api.rule=Host(`api.${DOMAIN}`)"

# Em frontend:
- "traefik.http.routers.frontend.rule=Host(`${DOMAIN}`)"
```

Crie 2 registros A (`${DOMAIN}` e `api.${DOMAIN}`) e ajuste `BETTER_AUTH_URL` e o `TWILIO_WEBHOOK_URL` no `.env.prod` se necessário.

## Operações comuns

### Ver status

```bash
cd deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
```

### Ver logs

```bash
# Todos
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f

# Só um serviço
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f api
```

### Redeploy (após git pull)

```bash
git pull
bash deploy/scripts/deploy.sh
```

Apenas um serviço:

```bash
cd deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build api
```

### Atualizar variável de ambiente

Edite `deploy/.env.prod`, depois:

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod up -d
```

(O Compose detecta o diff e recria os containers afetados.)

### Pausar e retomar

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod stop
docker compose -f docker-compose.prod.yml --env-file .env.prod start
```

### Conectar no banco

**Caminho oficial: pgweb em `/banco`** — interface web (browse, query, edição), protegida por BasicAuth:

```
https://${DOMAIN}/banco/
```

Usuário/senha: `PGWEB_AUTH_USER` / `PGWEB_AUTH_PASS` do `.env.prod`. O container roda em modo `--lock-session` com `PGWEB_DATABASE_URL` pré-configurada — após o BasicAuth, a sidebar carrega direto com as tabelas do `whatsapp_langchain` (sem tela "Connect" intermediária; os botões "Connect/Disconnect" do header ficam inertes).

**Acesso via psql (raro — debug profundo):** o Postgres não está exposto publicamente, então o caminho é `docker exec`:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec db \
  psql -U "$(grep ^POSTGRES_USER= .env.prod | cut -d= -f2-)" \
       -d "$(grep ^POSTGRES_DB= .env.prod | cut -d= -f2-)"
```

Para conectar a partir de DBeaver/TablePlus na sua máquina, use SSH tunnel — não exponha 5432 publicamente.

### Backup do PostgreSQL

```bash
DATE=$(date +%Y%m%d-%H%M)
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod exec -T db \
  pg_dump -U "$(grep ^POSTGRES_USER= deploy/.env.prod | cut -d= -f2-)" \
          -d "$(grep ^POSTGRES_DB= deploy/.env.prod | cut -d= -f2-)" \
  | gzip > "backup-$DATE.sql.gz"
```

Restore:

```bash
gunzip -c backup-XXXX.sql.gz | \
  docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod exec -T db \
    psql -U "$(grep ^POSTGRES_USER= deploy/.env.prod | cut -d= -f2-)" \
         -d "$(grep ^POSTGRES_DB= deploy/.env.prod | cut -d= -f2-)"
```

> Recomendação: agendar backup diário via `cron` e copiar para storage externo (S3, Backblaze).

### Renovação SSL

Automática. Traefik renova certificados ~30 dias antes da expiração. Para forçar renovação (raro):

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod down
rm deploy/traefik/acme.json
touch deploy/traefik/acme.json
chmod 600 deploy/traefik/acme.json
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod up -d
```

> Atenção: Let's Encrypt tem rate limit de 50 certificados/semana por domínio. Não force sem necessidade.

## Customização para múltiplos clientes

Este projeto é um **template** — cada cliente herda esta estrutura.

### Forma 1: VPS dedicada por cliente (mais simples)

1. Clone o repo numa VPS nova.
2. Customize o agente em `src/whatsapp_langchain/agents/catalog/<nome_cliente>/`.
3. Atualize `langgraph.json` com o novo agente.
4. Configure `DOMAIN` no `.env.prod` para o subdomínio do cliente.
5. Deploy normal.

### Forma 2: Múltiplos clientes na mesma VPS (avançado)

Para isolar clientes na mesma VPS:

1. Use **um Traefik compartilhado** (stack separado), não o Traefik incluso aqui.
2. Cada cliente roda num diretório próprio com seu compose, mas SEM o serviço `traefik`.
3. Use a mesma rede externa nos compose dos clientes.
4. Cada cliente tem seu `DOMAIN` próprio (subdomínios diferentes).

Exige refactor — não está pronto neste template. Discuta antes.

## Troubleshooting

| Sintoma | Provável causa | O que fazer |
|---|---|---|
| `bad gateway` no domínio | Container caiu | `docker compose ps` + `logs <serviço>` |
| `certificate not trusted` após 1min | DNS não propagou ou proxy ligado | `dig $DOMAIN`; conferir Cloudflare |
| `404 not found` em `/login` | Frontend caiu | `docker compose logs frontend` |
| `403 forbidden` no webhook | Signature do Twilio inválida | `TWILIO_WEBHOOK_URL` deve ser apenas a base, sem path |
| `connection refused` ao conectar no banco | DB não saudável | `docker compose ps db`; ver `healthcheck` |
| Worker reiniciando | Credencial faltando em modo `real` ou nenhum canal configurado | Conferir `TWILIO_*` / `META_*` / `UAZAPI_BASE_URL` em `.env.prod`. Logs: `event: no_outbound_channel_enabled`. |
| Agente não responde mas stack 100% saudável | `OPENROUTER_API_KEY` inválida (falha em runtime, não no boot) | Testar com `curl -H "Authorization: Bearer $KEY" https://openrouter.ai/api/v1/models` — esperado `200` |
| Build do Next.js morre | OOM em VPS pequena | Adicionar swap (ver `init-vps.sh` passo 5) |
| Let's Encrypt rate limit | Recriou certs muitas vezes | Usar staging temporariamente (ver skill `deploy`) |

Para diagnóstico profundo da fila, use a skill `debug-queue` (`.claude/skills/debug-queue/SKILL.md`).

## Skills automatizadas

Este projeto tem um agente especializado (Claude Code) em `.claude/agents/whatsapp-langchain-specialist.md` com 9 skills para automatizar operações:

| Skill | Quando |
|---|---|
| `infra-setup` | preparar VPS limpa (Docker, UFW, swap, acme.json) |
| `domain-setup` | configurar DNS e validar propagação |
| `deploy` | subir/atualizar o stack |
| `twilio-setup` | configurar webhook Twilio (sandbox ou produção) |
| `meta-setup` | configurar webhook Meta WhatsApp Cloud API |
| `create-agent` | criar novo agente LangGraph no catálogo |
| `debug-queue` | diagnosticar problemas na `message_queue` |
| `stress-test` | rodar Locust contra a API |
| `ui-ux-pro-max` | mexer no UI/design do admin Next.js |

Em qualquer pasta deste projeto, peça ao Claude Code: "preciso fazer deploy" ou "criar agente para o cliente X" — ele invoca a skill correta.

> uazapi não tem skill própria; o setup é só preencher `UAZAPI_BASE_URL` no `.env.prod` e cadastrar `https://${DOMAIN}/webhook/uazapi?agent=<id>` no painel da instância.

## Documentação relacionada

| Arquivo | Conteúdo |
|---|---|
| `../docs/ARCHITECTURE.md` | Arquitetura geral, contratos entre serviços |
| `../docs/DEPLOY.md` | Visão geral de deploy (focada em Railway) |
| `../docs/TWILIO.md` | Integração Twilio detalhada |
| `../docs/DATABASE.md` | Schema do banco e queries |
| `../docs/RAILWAY.md` | Deploy alternativo no Railway |
| `../docs/STRESS_TESTING.md` | Stress testing detalhado |
