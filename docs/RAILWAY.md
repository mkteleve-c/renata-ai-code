# Deploy no Railway

Guia completo para deploy do whatsapp-langchain no Railway, cobrindo topologia de serviços, rede interna, config-as-code (`railway.*.json`) e todas as variáveis de ambiente necessárias.

> Railway é o **caminho escolhido para o cutover da Renata** (agente `elevec_sdr`
> — ver `docs/AGENTE_ELEVEC.md` e `docs/CUTOVER.md`). O caminho self-hosted
> continua existindo em Docker + Traefik + Let's Encrypt em VPS própria — ver
> [`deploy/README.md`](../deploy/README.md) e [DEPLOY.md](DEPLOY.md) — mas para
> este deploy específico use este documento.

## Topologia de Serviços

O projeto usa 4 serviços no Railway:

| Serviço  | Dockerfile            | Porta | Visibilidade | Réplicas | Domínio                       |
|----------|-----------------------|-------|--------------|----------|-------------------------------|
| API      | `Dockerfile.api`      | 8000  | Público      | **1**    | `api-*.up.railway.app`        |
| Worker   | `Dockerfile.worker`   | ---   | Privado      | **1**    | ---                           |
| Frontend | `Dockerfile.frontend` | 3000  | Público      | 1        | `frontend-*.up.railway.app`   |
| DB       | `Dockerfile.db`       | 5432  | Privado      | 1        | ---                           |

### Por que API e Worker ficam em 1 réplica — não é preguiça, é uma dívida real

Este documento sugeria 2 réplicas para a API antes desta revisão. Estava
errado, por dois motivos distintos que não desaparecem com "só documentar":

**API — o rate limit é em memória.** `RATE_LIMIT_PER_HOUR` (30 por telefone,
por padrão) é contado num dicionário dentro do processo. Com 2 réplicas por
trás do load balancer do Railway, cada instância conta pra si — o limite
*efetivo* por telefone dobra em silêncio (30/h vira até 60/h, sem nenhum log
ou métrica avisando). Escalar a API horizontalmente exige mover essa
contagem para o Postgres primeiro (compartilhado entre réplicas); isso está
**fora do escopo desta entrega**. Enquanto isso não acontecer, 1 réplica é o
único jeito de `RATE_LIMIT_PER_HOUR` continuar significando o que o nome diz.

**Worker — a régua de follow-up não coordena entre processos.** O
`FOR UPDATE SKIP LOCKED` do `claim_next` protege a fila comum contra
duplicidade mesmo com várias réplicas — isso é seguro. O problema é
`iniciar_followup`/`rodada` (`worker/followup.py`): cada réplica roda seu
próprio loop assíncrono no mesmo intervalo, reivindicando lotes da régua de
follow-up. Duas réplicas dobram a carga de escrita em `leads_crm` sem
nenhum ganho de throughput — a régua já processa o lote inteiro em uma
rodada. Isso é ortogonal ao fato de `FOLLOWUP_ENABLED=false` no primeiro
deploy (ver seção própria abaixo); mesmo depois de ligada, o worker continua
em 1 réplica.

Se um dia a fila crescer a ponto de precisar de mais throughput de
mensageria, a resposta é mais réplicas do worker **com o rate limit já
migrado para o Postgres e a régua de follow-up com lock distribuído** — não
apenas subir `numReplicas`.

### API

Serviço público que recebe webhooks dos canais habilitados e expõe o health check.

- **Rotas públicas:**
  - `/health`
  - `POST /webhook/twilio?agent=<id>` (Twilio)
  - `GET /webhook/meta` (handshake) e `POST /webhook/meta?agent=<id>` (Meta WhatsApp Cloud API)
  - `POST /webhook/uazapi?agent=<id>` (uazapi/uazapiGO)
  - `POST /webhook/evolution?agent=<id>` (Evolution API — ver `docs/EVOLUTION.md`; é o canal do cutover da Renata)
- **Rotas protegidas:** `/api/agents`, `/api/chats`, `/api/metrics`, `/api/queue` requerem `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>`
- O Frontend se comunica com a API via rede interna do Railway (`http://api.railway.internal:8000`), nunca pelo domínio público
- **1 réplica** — ver justificativa acima

> `POST /webhook/chatwoot` (pausa/retomada do agente pela etiqueta
> `pausar_agente`) está **planejado, não implementado** — bloqueado na Task 5
> da Fase 3 até um payload real do ChatWoot ser capturado. Não cadastre esse
> webhook em lugar nenhum ainda; ver `docs/CUTOVER.md` passo 9.

### Worker

Serviço privado que consome a fila de mensagens do PostgreSQL.

- Sem porta exposta --- não recebe requisições HTTP
- Faz polling na tabela de fila do banco para processar mensagens pendentes
- Executa os agentes LangGraph e envia respostas pelo cliente outbound do canal de origem (lê `message_queue.channel`: twilio | meta | uazapi | evolution)
- Mantém um cliente outbound por canal habilitado (com credenciais preenchidas) e faz fail-fast no boot se um canal está "tocado parcialmente" em `OUTBOUND_MODE=real`
- **1 réplica** — ver justificativa acima
- Roda a régua de follow-up (`worker/followup.py`) quando `FOLLOWUP_ENABLED=true` — **fica `false` no primeiro deploy**, ver seção própria

### Frontend

Admin Panel em Next.js, público para acesso dos administradores.

- Consome a API internamente via `http://api.railway.internal:8000`
- O `INTERNAL_SERVICE_TOKEN` garante que apenas o Frontend consegue chamar as rotas `/api/*`
- Conecta diretamente ao banco para o Better Auth (sessões, usuários, tokens)

### DB (PostgreSQL + pgvector)

Container customizado usando a imagem `pgvector/pgvector:pg16`.

- Acessível apenas pela rede interna (privado)
- Volume persistente montado em `/var/lib/postgresql/data`
- pgvector habilitado para memória semântica (extensão criada via migração SQL)
- **Não é um plugin nativo do Railway** --- usa `Dockerfile.db` com a imagem do pgvector
- **É o banco de verdade depois do cutover** — ver "Postgres é a origem de verdade" abaixo

---

## `railway.*.json` — config-as-code

O Railway procura, por padrão, um `railway.json` (ou `railway.toml`) **na raiz
do serviço**. Como os quatro serviços deste projeto compartilham a mesma raiz
de repositório mas usam **Dockerfiles diferentes** (`build.dockerfilePath`
diverge por serviço), um único `railway.json` na raiz não dá conta dos
quatro — cada serviço precisaria de um `dockerfilePath` diferente do mesmo
campo do mesmo arquivo. A saída oficial do Railway para isso é "Custom Config
File Path" por serviço (Service Settings → Config as Code), então este
projeto tem **quatro arquivos**, um por serviço:

| Arquivo | Serviço | O que fixa |
|---|---|---|
| `railway.api.json` | api | `Dockerfile.api`, watch paths, healthcheck `/health`, 1 réplica |
| `railway.worker.json` | worker | `Dockerfile.worker`, watch paths, sem healthcheck HTTP, 1 réplica |
| `railway.frontend.json` | frontend | `Dockerfile.frontend`, watch paths, healthcheck `/login`, 1 réplica |
| `railway.db.json` | db | `Dockerfile.db`, watch paths, `requiredMountPath` do volume, 1 réplica |

**Nenhum dos quatro se chama `railway.json`** — de propósito. Se existisse um
`railway.json` na raiz, ele seria o *default* que o Railway aplicaria a
**qualquer serviço sem path customizado configurado**, o que é exatamente o
bug a evitar aqui: os quatro serviços moram na mesma raiz, então um default
compartilhado vazaria configuração de um serviço para os outros (ex: o worker
herdando o healthcheck HTTP da API). Por isso os quatro nomes são explícitos
e **os quatro serviços precisam ter o path customizado configurado no
dashboard** — nenhum pode depender do default.

Configuração no dashboard, por serviço (Settings → Config as Code → Custom
Config File Path):

```
api      → railway.api.json
worker   → railway.worker.json
frontend → railway.frontend.json
db       → railway.db.json
```

> Configuração no arquivo **sempre tem precedência sobre o dashboard** — se
> você mudar `numReplicas` pelo dashboard depois, o próximo deploy volta a
> ler o valor do JSON. Mude no arquivo, não no dashboard, ou as duas fontes
> divergem silenciosamente.

Os quatro arquivos foram validados contra o schema oficial
(`https://railway.com/railway.schema.json`) — o `$schema` de cada um aponta
pra lá, então editores com suporte a JSON Schema (VS Code) dão autocomplete e
validação inline.

### Watch Paths — agora dentro do arquivo, não só no dashboard

Cada `railway.*.json` já declara `build.watchPatterns` — controla quais
mudanças disparam redeploy daquele serviço. Isso substitui a configuração
manual "Service Settings > Source > Watch Paths" do dashboard que a versão
anterior deste documento descrevia como único caminho possível.

| Serviço      | Watch Paths (em `build.watchPatterns`)                                                                                      | Motivo                               |
|--------------|--------------------------------------------------------------------------------------------------------------------------------|--------------------------------------|
| **API**      | `src/whatsapp_langchain/server/**`, `src/whatsapp_langchain/shared/**`, `pyproject.toml`, `uv.lock`, `Dockerfile.api` | Código da API + dependências compartilhadas |
| **Worker**   | `src/whatsapp_langchain/worker/**`, `src/whatsapp_langchain/agents/**`, `src/whatsapp_langchain/shared/**`, `pyproject.toml`, `uv.lock`, `Dockerfile.worker` | Worker + agentes + dependências compartilhadas |
| **Frontend** | `frontend/**`, `Dockerfile.frontend`                                                                                    | Isolado do backend                   |
| **DB**       | `db/**`, `Dockerfile.db`                                                                                                | Migrações e imagem do Postgres       |

O motivo de granularidade continua o mesmo: `src/whatsapp_langchain/` mistura
código de API e Worker debaixo do mesmo diretório (`server/` só da API,
`worker/`+`agents/` só do Worker, `shared/` dos dois) — assistir `src/**`
inteiro faria qualquer mudança redesplegar os dois serviços à toa.

### Por que `db/**` não dispara redeploy da API/Worker

Migrações SQL são executadas automaticamente no **startup da API**
(`run_migrations`), não durante o build de nenhum serviço — uma migração
nova não deve triggerar redeploy do worker ou do frontend. `db/**` só está
no watch path do serviço `db` (a imagem do Postgres em si, via
`Dockerfile.db`), não porque os arquivos `.sql` mudem o container do banco
(eles não mudam — rodam via `psql`/`psycopg` no boot da API), mas porque é o
lugar mais correto para o Railway associar "mudança no schema" a "serviço de
dados", ainda que o redeploy do `db` sozinho não aplique migração nenhuma.

---

## Rede Interna (Reference Variables)

Os serviços se comunicam pela rede privada do Railway. Para que o dashboard visualize as conexões entre serviços, usamos **reference variables** (`${{service.VARIABLE}}`) em vez de strings hardcoded.

### Conexoes

```
                    +----------+
                    |    db    | (privado)
                    | pgvector |
                    +----+-----+
              +----------+----------+
              |          |          |
         +----v---+ +----v----+ +--v-------+
         |  api   | | worker  | | frontend |
         | :8000  | | (priv.) | | Next.js  |
         +----+---+ +---------+ +--+-------+
              |                    |
              |<-------------------+
              |   INTERNAL_API_URL
              |   (rede interna)
```

### DATABASE_URL (api, worker, frontend)

```
postgresql://${{db.POSTGRES_USER}}:${{db.POSTGRES_PASSWORD}}@${{db.RAILWAY_PRIVATE_DOMAIN}}:5432/${{db.POSTGRES_DB}}
```

Isso referência as variáveis do serviço `db` e resolve para algo como:

```
postgresql://postgres:SENHA@db.railway.internal:5432/whatsapp_langchain
```

### INTERNAL_API_URL (frontend)

```
http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000
```

Resolve para `http://api.railway.internal:8000`.

### Como setar via CLI

A CLI do Railway (`railway variables`) mostra os valores **resolvidos**, mas internamente o Railway armazena as referências. Para setar via CLI, use aspas simples para evitar que o shell interprete `${{}}` como substituição bash:

```bash
# DATABASE_URL com referências ao serviço db
railway variables --service api --set 'DATABASE_URL=postgresql://${{db.POSTGRES_USER}}:${{db.POSTGRES_PASSWORD}}@${{db.RAILWAY_PRIVATE_DOMAIN}}:5432/${{db.POSTGRES_DB}}'

# INTERNAL_API_URL com referência ao serviço api
railway variables --service frontend --set 'INTERNAL_API_URL=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000'
```

> **Por que não hardcodar?** Além da visualização no dashboard, se o Railway alterar hostnames internos ou credenciais do banco, as referências se atualizam automaticamente.

---

## Migrações

O projeto tem dois mecanismos de migração que coexistem:

### 1. Automatica (startup da API)

Quando a API sobe, o `lifespan` executa `run_migrations()` antes de aceitar requisições. Esse mecanismo:

1. Cria a tabela `_migrations` se não existir (controle de estado)
2. Le todos os arquivos `.sql` de `db/migrations/` em ordem alfabética
3. Compara com os nomes já registrados na tabela `_migrations`
4. Aplica os pendentes e registra cada um

```python
# src/whatsapp_langchain/server/main.py (lifespan)
pool = await get_pool()
await run_migrations(pool)          # migrações SQL
await bootstrap_langgraph_schema()  # tabelas do checkpointer + store
```

Isso significa que **não é necessário rodar migrações manualmente** após um deploy --- a API cuida disso automaticamente.

### 2. Manual (script standalone)

O script `db/migrate.py` faz a mesma coisa, mas de forma síncrona e independente. Útil para:

- Rodar migrações sem subir a API
- Debugging local
- Aplicar migrações em ambientes sem a API rodando

```bash
# Local
python db/migrate.py

# No Railway (usando variáveis do serviço api)
railway run --service api python db/migrate.py
```

### Arquivos de migração

```
db/migrations/
├── 001_initial.sql                     # Schema da fila de mensagens (message_queue, conversations)
├── 002_media_processing_audit.sql      # Auditoria de mídia (normalized_input, media_processing_*)
├── 003_auth_schema.sql                 # CREATE SCHEMA auth (Better Auth)
├── 004_better_auth_tables.sql          # Tabelas user/session/account/verification
├── 005_uazapi_outbound_token.sql       # message_queue.outbound_token (token uazapi via payload)
├── 006_message_channel.sql             # message_queue.channel (twilio | meta | uazapi | evolution)
├── 007_elevec.sql                      # leads_crm, blocklist, legacy_chat_history, leads_descartados (SDR elevec_sdr)
├── 008_provider_message_key.sql        # message_queue.provider_message_key (mídia Evolution)
├── 009_message_id_unico_por_canal.sql  # índice de dedupe (channel, message_id)
├── 010_message_id_unico_por_agente.sql # índice de dedupe (channel, agent_id, message_id)
├── 011_leads_pausa_not_null.sql        # leads_crm.followup_active/agent_active NOT NULL
├── 012_dedupe_por_telefone_e_absorvidos.sql # índice de dedupe (channel, agent_id, phone_number, message_id) + message_ids_absorvidos
├── 013_last_inbound_at.sql             # leads_crm.last_inbound_at — âncora dos degraus de follow-up
├── 014_uma_linha_por_pessoa.sql        # CHECK de telefone canônico em leads_crm
├── 015_legacy_chat_history.sql         # ajustes de histórico legado importado
└── 016_blocklist_opt_out_n8n.sql       # opt-outs importados do n8n (26 pessoas)
```

Para adicionar uma nova migração, crie um arquivo SQL com o próximo número sequencial (ex: `016_nova_feature.sql`). A ordem alfabética dos nomes determina a ordem de aplicação.

> **Migração que troca índice de `ON CONFLICT` exige parada, não rolling
> deploy** — ver `docs/DATABASE.md` (`_migrations` → "Migração que troca
> índice..."). Com 1 réplica na API isso já não é um problema de duas
> réplicas convivendo brevemente; ainda assim, confira que nenhuma réplica
> antiga sobreviveu depois de um deploy que inclua uma migração dessa
> categoria.

### Idempotência

Ambos os mecanismos usam a tabela `_migrations` com constraint `UNIQUE` no nome do arquivo. Se a migração já foi aplicada, ela é ignorada silenciosamente. Com 1 réplica na API (ver acima) não há corrida entre réplicas tentando aplicar a mesma migração ao mesmo tempo.

### Bootstrap do LangGraph

Além das migrações SQL, o startup da API também executa `bootstrap_langgraph_schema()`, que inicializa:

- **Checkpointer** (`AsyncPostgresSaver`) --- tabelas para persistência de conversas
- **Store vetorial** (`AsyncPostgresStore`) --- tabelas para memória semântica (quando `MEMORY_ENABLED=true`)

Essas tabelas são do LangGraph e não aparecem em `db/migrations/`. O LangGraph gerencia o schema delas internamente via `.setup()`.

---

## Postgres do Railway é a origem de verdade — Supabase é leitura única

Antes do cutover, os dados vivos da Renata moram no Supabase (projeto do n8n).
`scripts/migrar_supabase.py` lê o Supabase **uma única vez**, via REST,
paginado, e escreve em `leads_crm`/`legacy_chat_history` deste Postgres. A
partir do momento em que essa migração roda com `--executar`:

- **O Postgres do Railway passa a ser o banco de verdade.** Toda leitura e
  escrita operacional (fase do lead, agendamento, handover, follow-up) passa
  a acontecer aqui, nunca mais no Supabase.
- **O Supabase não é mais tocado.** Nenhum componente do harness escreve
  nele — a leitura do `migrar_supabase.py` é a única interação, e é de
  mão única.
- **O Supabase deixa de existir depois do cutover** — é descartado junto
  com o n8n, não fica como réplica de leitura nem como backup contínuo. O
  backup de longo prazo passa a ser o backup do volume do Postgres do
  Railway (seção abaixo), não mais o Supabase.

Isso também explica por que **o Supabase não é alterado pela migração** — se
o cutover precisar ser desfeito, o Supabase continua exatamente como estava,
e o caminho de volta é religar o n8n contra ele (ver `docs/CUTOVER.md`,
"Plano de volta").

### Como alcançar o Postgres de produção de fora do Railway

Todo comando deste documento e de `docs/CUTOVER.md` que precisa falar com o
Postgres de produção **a partir do seu laptop** — `preflight_cutover.py`,
`migrar_supabase.py`, `monitorar_cutover.py`, `pg_dump`/`psql` do backup —
esbarra no mesmo problema se você não ler esta seção primeiro.

**O problema:** `DATABASE_URL` (a reference variable da seção "Rede Interna"
acima) resolve para `postgresql://...@db.railway.internal:5432/...` —
`db.railway.internal` só existe dentro da rede privada do Railway, entre os
serviços do próprio projeto. `railway run --service <x> <comando>` **executa
o `<comando>` NA SUA MÁQUINA**, só injetando as variáveis de ambiente do
serviço `<x>` — inclusive a `DATABASE_URL` privada. O resultado, rodando um
script que lê `DATABASE_URL` desse jeito no seu laptop: a conexão trava até
dar timeout (o host não resolve fora da rede do Railway), não um erro óbvio
de "host errado".

**O mecanismo certo é o TCP Proxy**, não `railway run` sozinho nem `railway
connect` (este último abre um shell **interativo** — ótimo para cutucar o
banco na mão com `psql`, inútil para os scripts Python deste projeto ou para
`pg_dump`/`psql` num pipe não-interativo):

1. **Habilite o TCP Proxy no serviço `db`, uma vez** (fica permanente —
   dashboard: Service `db` → Settings → Networking → TCP Proxy → porta
   `5432`; ou via CLI):

   ```bash
   railway tcp-proxy create --port 5432 --service db
   ```

   Isso expõe três variáveis novas no **próprio serviço `db`**:
   `RAILWAY_TCP_PROXY_DOMAIN` (algo como `roundhouse.proxy.rlwy.net`),
   `RAILWAY_TCP_PROXY_PORT` (uma porta externa, não `5432`) e
   `RAILWAY_TCP_PROXY_APPLICATION_PORT`. **Não confunda com
   `DATABASE_PUBLIC_URL`** — essa variável é gerada automaticamente só para
   os plugins de Postgres *gerenciados* pelo Railway (ex.: "PostgreSQL HA");
   `db` aqui é um serviço customizado (`Dockerfile.db`), então a URL pública
   não existe pronta — você monta com as três variáveis acima mais as
   credenciais que já existem (`POSTGRES_USER`/`POSTGRES_PASSWORD`/
   `POSTGRES_DB`).

2. **Monte a `DATABASE_URL` pública** — `railway run --service db` injeta
   as credenciais do banco **e**, com o proxy do passo 1 já criado, as três
   variáveis do TCP Proxy, tudo como env vars locais no comando que você
   passar a ele:

   ```bash
   export DATABASE_URL_PUBLICA=$(railway run --service db bash -c '
     echo "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@$RAILWAY_TCP_PROXY_DOMAIN:$RAILWAY_TCP_PROXY_PORT/$POSTGRES_DB"
   ')
   ```

   `$DATABASE_URL_PUBLICA` fica exportada na sua sessão de terminal — é o
   valor que `preflight_cutover.py`/`migrar_supabase.py`/
   `monitorar_cutover.py` precisam em `DATABASE_URL` para rodar do laptop.
   Vale só para essa sessão de shell; se abrir um terminal novo, rode este
   passo de novo.

3. **Rode os scripts sobrescrevendo `DATABASE_URL`** — as demais variáveis
   (Evolution, Google, Pipedrive, OpenRouter, `HANDOVER_NOTIFY_PHONE`...)
   continuam vindo do serviço `worker`; só `DATABASE_URL` precisa ser a
   pública, não a privada que `railway run --service worker` injetaria por
   padrão:

   ```bash
   railway run --service worker env DATABASE_URL="$DATABASE_URL_PUBLICA" \
     uv run python scripts/preflight_cutover.py
   ```

   `env VAR=valor comando` sobrescreve `VAR` só para aquele `comando`
   específico — mesmo o `railway run` já tendo exportado a `DATABASE_URL`
   privada do `worker` antes disso, o `env` na frente do comando final vence
   para esse processo.

Isso vale para os **três** scripts de cutover (`preflight_cutover.py`
roda no passo 2 do roteiro, `migrar_supabase.py` nos passos 4 e 6,
`monitorar_cutover.py` no passo 11) e para `pg_dump`/`psql` do backup
abaixo — nenhum deles alcança produção sem essa URL pública.

### Backup do volume do Postgres antes do cutover

O volume do serviço `db` no Railway é o dado real depois do cutover — trate
como trataria qualquer banco de produção. Duas formas, use as duas.

**1. Backup lógico (`pg_dump`), antes de rodar a migração** — com o TCP
Proxy já habilitado (seção acima). `railway run --service db` roda o
`pg_dump`/`psql` **na sua máquina**, não dentro do Railway, e a Railway
**não injeta `PGHOST`/`PGPORT`** — sem `-h`/`-p` explícitos, o comando ou
falha em conectar, ou, se você tiver um Postgres local escutando na 5432,
**dumpa esse banco local em silêncio**, sem erro nenhum, produzindo um
`.sql.gz` que parece válido mas não é o de produção. Os dois `-h`/`-p`
abaixo fecham essa lacuna — e, como defesa adicional, o passo conta linhas
de `leads_crm` ANTES do dump e confere que o dump carrega a mesma contagem,
porque um arquivo `.sql.gz` existir não prova, sozinho, que veio do banco
certo:

```bash
ARQUIVO="backup-pre-cutover-$(date +%Y%m%d-%H%M).sql.gz"

railway run --service db bash -c '
  set -euo pipefail
  CONTAGEM_ANTES=$(psql -h "$RAILWAY_TCP_PROXY_DOMAIN" -p "$RAILWAY_TCP_PROXY_PORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from leads_crm")
  echo "leads_crm no banco: $CONTAGEM_ANTES linha(s)"

  pg_dump -h "$RAILWAY_TCP_PROXY_DOMAIN" -p "$RAILWAY_TCP_PROXY_PORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "'"$ARQUIVO"'"

  CONTAGEM_NO_DUMP=$(gunzip -c "'"$ARQUIVO"'" \
    | awk "/^COPY public\.leads_crm /{c=1;next} \$0==\"\\\\.\"{c=0} c{n++} END{print n+0}")
  echo "leads_crm no dump: $CONTAGEM_NO_DUMP linha(s)"

  [ "$CONTAGEM_ANTES" = "$CONTAGEM_NO_DUMP" ] || {
    echo "ERRO: contagem não bate ($CONTAGEM_ANTES no banco, $CONTAGEM_NO_DUMP no dump)"
    echo "-- o dump pode ter vindo do banco errado. NÃO trate como backup válido."
    exit 1
  }
'
```

Guarde esse arquivo fora do Railway (download local, ou upload pra storage
externo) — um backup que só existe dentro do mesmo projeto que você está
prestes a mexer não protege contra um erro de operação no próprio Railway.

**Como saber que deu certo:** você tem um `.sql.gz` fora do Railway **e** a
mensagem confirma que a contagem de `leads_crm` no dump bate com a do banco
no momento do backup — não só que o arquivo existe.

**2. Snapshot do volume**, pelo dashboard do Railway (Service `db` →
Volume → Backups/Snapshots, se o plano contratado oferecer). Cheque a
disponibilidade dessa opção no plano atual antes do cutover — nem todo plano
Railway oferece snapshot de volume; se o seu não oferecer, o `pg_dump` acima
é o único backup e precisa ser tratado como tal (testado, restaurável).

Restore do `pg_dump` — mesma correção de `-h`/`-p` explícitos:

```bash
gunzip -c backup-pre-cutover-XXXX.sql.gz | \
  railway run --service db bash -c '
    psql -h "$RAILWAY_TCP_PROXY_DOMAIN" -p "$RAILWAY_TCP_PROXY_PORT" \
      -U "$POSTGRES_USER" -d "$POSTGRES_DB"
  '
```

---

## Variáveis de Ambiente

Abaixo estão todas as variáveis necessárias, organizadas por serviço. As
variáveis específicas do agente `elevec_sdr` (Evolution, Google Calendar,
Pipedrive, Handover, Follow-up) entram no **Worker**, que é quem carrega e
executa o agente; `EVOLUTION_WEBHOOK_SECRET` entra na **API**, porque é ela
quem valida o webhook inbound.

### DB

| Variavel | Valor / Exemplo | Descricao |
|----------|----------------|-----------|
| `POSTGRES_USER` | `postgres` | Usuario do PostgreSQL |
| `POSTGRES_PASSWORD` | --- | Senha do PostgreSQL (gerar com `openssl rand -base64 32`) |
| `POSTGRES_DB` | `whatsapp_langchain` | Nome do banco de dados |
| `PGDATA` | `/var/lib/postgresql/data/pgdata` | Diretorio de dados (dentro do volume) |

> `RAILWAY_DOCKERFILE_PATH` não é mais necessário — `railway.db.json` já fixa
> `dockerfilePath` via config-as-code. Mantenha a variável só se este serviço
> ainda não tiver o Custom Config File Path configurado no dashboard.

### API

| Variavel | Valor / Exemplo | Descricao |
|----------|----------------|-----------|
| `DATABASE_URL` | `${{db.*}}` (reference) | Connection string do PostgreSQL via rede interna |
| `ENVIRONMENT` | `production` | Ambiente de execução --- desabilita `/webhook/sync` em production |
| `LOG_LEVEL` | `info` | Nível de log (debug, info, warning, error) |
| `LOG_JSON` | `true` | Logs em formato JSON estruturado (melhor para produção) |
| `PORT` | `8000` | Porta do FastAPI |
| `OUTBOUND_MODE` | `real` | Precisa estar setado aqui também — `validate_runtime_settings()` roda no boot da API |
| `VALIDATE_TWILIO_SIGNATURE` | `true` (se Twilio habilitado) | Validar assinatura dos webhooks do Twilio |
| `TWILIO_AUTH_TOKEN` | --- (se Twilio habilitado) | Auth Token do Twilio (necessário para validação de assinatura) |
| `TWILIO_WEBHOOK_URL` | `https://api-*.up.railway.app` (se Twilio habilitado) | URL base pública da API (sem path, sem barra final) |
| `META_VERIFY_TOKEN` | --- (se Meta habilitado) | Verify token do handshake `GET /webhook/meta` |
| `META_APP_SECRET` | --- (se Meta habilitado) | App Secret do Meta para HMAC-SHA256 |
| `META_VALIDATE_SIGNATURE` | `true` (se Meta habilitado) | Validar `X-Hub-Signature-256` |
| `EVOLUTION_WEBHOOK_SECRET` | --- (se Evolution habilitado) | **Obrigatório em produção com o canal Evolution tocado** — sem ele `/webhook/evolution` aceita qualquer POST. 32+ caracteres, `openssl rand -base64 32`. A Evolution não assina o body; este é o único gate do inbound |
| `RATE_LIMIT_PER_HOUR` | `30` | Maximo de mensagens por telefone por hora — só significa isso com **1 réplica**, ver seção de topologia |
| `MESSAGE_BUFFER_SECONDS` | `2.0` | Tempo de espera para agrupar mensagens consecutivas |
| `INTERNAL_SERVICE_TOKEN` | --- | Token para proteger rotas `/api/*` **(shared com Frontend)**. 32+ caracteres em produção |
| `OPENROUTER_API_KEY` | --- | Chave do OpenRouter (necessária para bootstrap do LangGraph store — embeddings) |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | URL base do OpenRouter (idem) |
| `MEMORY_ENABLED` | `true` | Habilitar memória semântica — controla se o store vetorial é criado no startup |
| `EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Modelo de embeddings usado no bootstrap do store |
| `EMBEDDING_DIMS` | `1536` | Dimensões do vetor de embeddings |

> **Por que a API precisa de variáveis de embeddings?** No startup, a API chama `bootstrap_langgraph_schema()` que inicializa as tabelas do checkpointer e do store vetorial. O store precisa da configuração de embeddings para criar os índices. Sem essas variáveis, o startup falha quando `MEMORY_ENABLED=true`.

### Worker

| Variavel | Valor / Exemplo | Descricao |
|----------|----------------|-----------|
| `DATABASE_URL` | `${{db.*}}` (reference) | Connection string do PostgreSQL via rede interna |
| `ENVIRONMENT` | `production` | Ambiente de execução |
| `LOG_LEVEL` | `info` | Nível de log |
| `LOG_JSON` | `true` | Logs em formato JSON estruturado |
| `OPENROUTER_API_KEY` | --- | Chave de API do OpenRouter |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | URL base do OpenRouter |
| `OPENROUTER_MODEL` | `x-ai/grok-4.3` para `elevec_sdr` | Modelo principal — o SOP da Renata foi validado neste modelo, não no default do harness |
| `OPENROUTER_MIDIA_MODEL` | --- | Modelo para processamento de mídia |
| `OUTBOUND_MODE` | `real` | Modo outbound compartilhado entre todos os canais |
| **Twilio** (se habilitado) | | |
| `TWILIO_ACCOUNT_SID` | --- | Account SID do Twilio |
| `TWILIO_API_KEY_SID` | --- | API Key SID para envio de mensagens e download de mídia |
| `TWILIO_API_KEY_SECRET` | --- | API Key Secret para envio de mensagens e download de mídia |
| `TWILIO_FROM_NUMBER` | `whatsapp:+14155238886` | Numero do WhatsApp remetente |
| **Meta WhatsApp Cloud API** (se habilitado) | | |
| `META_PHONE_NUMBER_ID` | --- | Phone Number ID (não é o número em E.164) |
| `META_ACCESS_TOKEN` | --- | System User Access Token PERMANENTE com permissão whatsapp_business_messaging |
| `META_GRAPH_API_VERSION` | `v23.0` | Versão da Graph API |
| **uazapi** (se habilitado) | | |
| `UAZAPI_BASE_URL` | `https://meucliente.uazapi.com` | Subdomínio da instância (sem barra final) |
| `UAZAPI_INSTANCE_TOKEN` | --- | Fallback estático opcional — token chega no payload do webhook por mensagem |
| **Evolution API** (canal do cutover da Renata) | | |
| `EVOLUTION_BASE_URL` | `https://evolution.ju39tu.easypanel.host` | URL base do servidor Evolution |
| `EVOLUTION_API_KEY` | --- | Autentica envio e download de mídia decifrada |
| `EVOLUTION_INSTANCE` | `instancia-apioficial` | Nome da instância — a integração é `WHATSAPP-BUSINESS` (Cloud API por baixo) |
| **Agente `elevec_sdr` — Google Calendar** (obrigatório se o worker roda esse agente) | | |
| `GOOGLE_CLIENT_ID` | --- | OAuth Client ID |
| `GOOGLE_CLIENT_SECRET` | --- | OAuth Client Secret |
| `GOOGLE_REFRESH_TOKEN` | --- | Refresh token permanente de uma conta com escrita no calendário |
| `GOOGLE_CALENDAR_ID` | e-mail do calendário | Agenda onde a Renata marca consultorias |
| **Agente `elevec_sdr` — Handover** (obrigatório junto com o grupo Google acima) | | |
| `HANDOVER_NOTIFY_PHONE` | --- | Telefone (E.164 ou dígitos) avisado quando `human_handover` desliga o agente. Vazio = handover silencioso — fail-fast no boot em modo real |
| **Agente `elevec_sdr` — Pipedrive** (opcional — não entra no fail-fast) | | |
| `PIPEDRIVE_API_TOKEN` | --- | Vazio = fase continua sendo gravada no banco, card não se move, log `crm_pipedrive_nao_configurado` |
| `PIPEDRIVE_STAGE_QUALIFICADO` | `12` | Confira contra o pipeline real antes de subir — id errado move card de verdade |
| `PIPEDRIVE_STAGE_AGENDADO` | `13` | Idem |
| **Agente `elevec_sdr` — Follow-up** | | |
| `FOLLOWUP_ENABLED` | **`false` no primeiro deploy** | Ver seção própria abaixo — não ligar no dia do cutover |
| `FOLLOWUP_INTERVAL_SECONDS` | `300` | Intervalo entre rodadas do loop no Worker |
| `FOLLOWUP_BATCH_SIZE` | `10` | Tamanho do lote reivindicado por rodada |
| `FOLLOWUP_NIVEL1_MINUTOS` | `15` | Ancorado em `last_inbound_at`, não `last_interaction_at` |
| `FOLLOWUP_NIVEL2_MINUTOS` | `75` | Idem |
| `FOLLOWUP_NIVEL3_MINUTOS` | `1380` | Idem |
| `FOLLOWUP_JANELA_MARGEM_MINUTOS` | `30` | Folga antes das 24h da janela da Cloud API |
| **Agente `elevec_sdr` — Balões** | | |
| `BALAO_DELAY_MS` | `700` | Espaçamento entre balões enviados em sequência |
| `BALAO_MAX_COUNT` | `10` | Teto de balões por resposta — protege `LEASE_SECONDS` |
| **Worker tuning** | | |
| `POLL_INTERVAL_SECONDS` | `1.0` | Intervalo de polling na fila |
| `LEASE_SECONDS` | `60` | Tempo máximo de processamento antes de retry |
| `MAX_ATTEMPTS` | `3` | Numero máximo de tentativas por mensagem |
| `MEDIA_IMAGE_ENABLED` | `true` | Habilitar processamento de imagens |
| `MEDIA_AUDIO_ENABLED` | `true` | Habilitar processamento de áudio |
| `LLM_RATE_LIMIT_REQUESTS_PER_SECOND` | `0.5` | Limite de requisições por segundo ao LLM |
| `LLM_RATE_LIMIT_MAX_BURST` | `10` | Maximo de requisições em rajada ao LLM |
| `CONTEXT_STRATEGY` | `summarize` para `elevec_sdr` | O n8n mantinha janela de 12 mensagens; `SUMMARIZE_KEEP_MESSAGES=10` é quem manda com essa estratégia |
| `TRIM_KEEP_TURNS` | `5` | Só vale se `CONTEXT_STRATEGY=trim` |
| `SUMMARIZE_TRIGGER_TOKENS` | `4000` | Tokens que disparam a sumarização |
| `SUMMARIZE_KEEP_MESSAGES` | `10` | Mensagens a manter após sumarizar |
| `SUMMARIZE_MODEL` | --- | Modelo usado para sumarização |
| `MEMORY_ENABLED` | `true` | Habilitar memória semântica |
| `MEMORY_SEARCH_LIMIT` | `5` | Maximo de memórias retornadas por busca |
| `EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Modelo de embeddings |
| `EMBEDDING_DIMS` | `1536` | Dimensões do vetor de embeddings |

> Variáveis inbound de assinatura ficam **somente no serviço `api`** (onde a
> validação acontece): `TWILIO_AUTH_TOKEN`/`TWILIO_WEBHOOK_URL`,
> `META_APP_SECRET`/`META_VERIFY_TOKEN`, `EVOLUTION_WEBHOOK_SECRET`. As
> credenciais outbound ficam **no serviço `worker`**: `TWILIO_*` (envio),
> `META_PHONE_NUMBER_ID`/`META_ACCESS_TOKEN`,
> `UAZAPI_BASE_URL`/`UAZAPI_INSTANCE_TOKEN`, `EVOLUTION_*`.

> **Fail-fast**: em `OUTBOUND_MODE=real`, API e Worker derrubam o boot se um
> canal está "tocado parcialmente" (ex: alguma credencial Evolution vazia
> com as outras preenchidas), ou se o grupo Google+Handover do `elevec_sdr`
> está parcialmente preenchido. Para desabilitar um canal ou o agente SDR,
> **zere todas** as credenciais dele.

### `CHATWOOT_WEBHOOK_SECRET` — ainda não existe

O plano original desta fase previa documentar `CHATWOOT_*` aqui como se já
fizesse parte da configuração do Worker/API. **Não faz** — o webhook do
ChatWoot (`POST /webhook/chatwoot`) está bloqueado na Task 5 da Fase 3
esperando um payload real do ChatWoot para confirmar o formato do evento
(ver `docs/superpowers/plans/2026-07-27-fase3-perifericos.md`). Não existe
`CHATWOOT_WEBHOOK_SECRET` em `Settings` (`shared/config.py`) hoje. Quando essa
task for concluída, a variável entra na **API** (mesma doutrina do
`EVOLUTION_WEBHOOK_SECRET`: obrigatória em produção, 32+ caracteres). Até lá,
não cadastre nada relacionado a ChatWoot no Railway — não há rota pra
receber.

### Frontend

| Variavel | Valor / Exemplo | Descricao |
|----------|----------------|-----------|
| `DATABASE_URL` | `${{db.*}}` (reference) | Connection string do PostgreSQL (para Better Auth) |
| `ENVIRONMENT` | `production` | Ativa guard rails de produção no frontend |
| `INTERNAL_API_URL` | `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000` | URL interna da API (rede privada Railway) |
| `INTERNAL_SERVICE_TOKEN` | --- | Token para autenticar nas rotas `/api/*` **(shared com API)** |
| `BETTER_AUTH_SECRET` | --- | Secret para sessões do Better Auth (gerar com `openssl rand -base64 32`) |
| `BETTER_AUTH_URL` | `https://frontend-*.up.railway.app` | URL pública do Frontend (usada pelo Better Auth para callbacks) |
| `ADMIN_EMAIL` | `admin@empresa.com` | Email do primeiro acesso ao painel |
| `ADMIN_PASSWORD` | --- | Senha do primeiro acesso ao painel; troque após o login inicial |
| `ADMIN_NAME` | `Admin` | Nome exibido do primeiro usuário (opcional) |

---

## `FOLLOWUP_ENABLED=false` no primeiro deploy — e por quê

`FOLLOWUP_ENABLED` fica `false` em todo primeiro deploy que rode o agente
`elevec_sdr`, deliberadamente, mesmo depois do cutover estar completo e
estável. Duas dívidas concretas sustentam isso, as duas registradas na Fase 3:

1. **O wiring de `main()` que liga a régua tem cobertura zero.** Em
   `src/whatsapp_langchain/worker/main.py:276`,
   `followup_task = iniciar_followup(pool, outbounds)` — nenhum teste da
   suíte falha se essa linha virar `followup_task = None`. A lógica interna
   da régua (`worker/followup.py`) está bem testada; o fio que a liga dentro
   do processo do worker, não.
2. **Trocar `except Exception` por `except BaseException`** no loop
   (`_loop_followup`, `worker/main.py:165`) faria a task engolir
   `asyncio.CancelledError` — o sinal de shutdown gracioso — e o worker
   pararia de desligar limpo. Hoje o código está correto (`except
   Exception`); a dívida é a fragilidade de alguém "simplificar" isso sem
   perceber a consequência.

Nenhuma das duas dívidas manda mensagem indevida para um lead — as duas
falham fechadas. Mas ligar a régua sem cobrir a primeira é ligar no escuro:
se `iniciar_followup` silenciosamente parar de ser chamado num refactor
futuro, nada no CI apontaria. Ligar `FOLLOWUP_ENABLED=true` é uma decisão
**separada**, tomada depois que o resto do cutover está estável (ver
`docs/CUTOVER.md`, passo 12), e idealmente depois de cobrir a dívida 1.

---

## Bootstrap do primeiro admin

Se `auth."user"` estiver vazio, o primeiro acesso ao `/login` cria
automaticamente o primeiro admin usando `ADMIN_EMAIL` e `ADMIN_PASSWORD`
definidos no serviço `frontend`.

Fluxo recomendado:

```bash
# Service: frontend
ADMIN_EMAIL=admin@empresa.com
ADMIN_PASSWORD=uma-senha-forte-aqui
ADMIN_NAME=Admin
```

Depois disso:
- entre pelo `/login`
- valide acesso ao painel
- troque a senha no `/settings`

Opcional em ambientes compartilhados:
- remova ou rotacione `ADMIN_PASSWORD` depois do primeiro login

> O signup público do Better Auth fica desabilitado e as rotas `/api/auth/sign-up/*`
> retornam `404`.
> O frontend também falha cedo em production se `INTERNAL_SERVICE_TOKEN` ou
> `BETTER_AUTH_SECRET` estiverem fracos.

---

## Checklist de Deploy

1. Criar o projeto no Railway
2. Criar os 4 serviços (db, api, worker, frontend) apontando para o mesmo repo
3. Em cada serviço, Service Settings → Config as Code → Custom Config File Path, apontando para `railway.db.json` / `railway.api.json` / `railway.worker.json` / `railway.frontend.json` respectivamente
4. Configurar variáveis do DB (user, password, database, pgdata)
5. Configurar `DATABASE_URL` com reference variables nos 3 serviços
6. Configurar variáveis específicas de cada serviço (tabelas acima) — Worker: pelo menos um canal de mensageria completo (hoje, `elevec_sdr` usa Evolution) + `FOLLOWUP_ENABLED=false`
7. Confirmar `OUTBOUND_MODE=real` na API **e** no Worker
8. Gerar domínio público para API e Frontend
9. Atualizar `TWILIO_WEBHOOK_URL` (se Twilio habilitado) com o domínio real da API — o webhook da Evolution é cadastrado direto no painel da instância, não por env var (ver `docs/CUTOVER.md`, passo 8)
10. Atualizar `BETTER_AUTH_URL` com o domínio real do Frontend
11. Confirmar 1 réplica na API **e** 1 réplica no Worker (já vem de `railway.api.json`/`railway.worker.json` — só confira que ninguém sobrescreveu pelo dashboard)
12. Adicionar volume ao serviço DB (`/var/lib/postgresql/data`)
13. Verificar migrações (rodam automaticamente no startup da API — checar logs por `migration_applying`; sequência atual vai de `001_initial.sql` a `016_blocklist_opt_out_n8n.sql` -- a 016 é a que importa os 26 opt-outs, confira que ela aparece)
14. Testar health check: `GET https://api-*.up.railway.app/health`
15. Definir `ADMIN_EMAIL` e `ADMIN_PASSWORD` no serviço `frontend`
16. Acessar `/login`, validar o bootstrap automático do primeiro admin e trocar a senha
17. Habilitar o TCP Proxy no serviço `db` (`railway tcp-proxy create --port 5432 --service db` — ver "Como alcançar o Postgres de produção de fora do Railway" acima) — pré-requisito para o passo 18 e para o roteiro de cutover conseguirem falar com o Postgres a partir de um laptop
18. Backup do volume do `db` (seção acima) — **antes** de qualquer coisa que toque produção
19. Seguir `docs/CUTOVER.md` para o roteiro completo de corte (rodar `preflight_cutover.py`, desligar n8n, migrar dados, repontar webhook)

> Este checklist cobre o **deploy da infraestrutura**. O corte de fato — desligar
> o n8n, migrar os dados do Supabase, repontar o webhook e monitorar a
> primeira hora — é `docs/CUTOVER.md`, não este documento.
