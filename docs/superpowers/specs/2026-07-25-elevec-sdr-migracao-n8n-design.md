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

### Qualidade da chave `phone` (medido)

| Formato de `leads_crm.phone` | Linhas |
|---|---|
| `+55DD9XXXXXXXX` (E.164 com `+`, vindo do formulário) | 2.109 |
| `55DD9XXXXXXXX` (dígitos, **com** o 9) | 415 |
| `55DDXXXXXXXX` (dígitos, canônico **sem** o 9) | 769 |
| Malformados (4 a 13 caracteres, inclusive um `null`) | 26 |

**3.319 linhas correspondem a 3.164 pessoas — há 155 duplicatas.** A causa é
demonstrável: `Add New Lead` procura o lead com
`WHERE phone IN (phone_v1, phone_v2)`, e ambos são strings **só de dígitos** —
portanto nunca casam com as 2.109 linhas que começam com `+`. Quando um lead de
formulário manda a primeira mensagem, o n8n **cria uma segunda linha** em vez de
atualizar a existente. Casos com 3 linhas para a mesma pessoa foram observados,
com fases divergentes (`formulario_preenchido` + `iniciou_conversa`).

Isso não é hipótese: é o estado atual do banco, e condiciona toda a migração.

## Decisões de arquitetura

| # | Decisão | Motivo |
|---|---|---|
| 1 | Nativo desde o dia 1 (sem n8n como backend) | requisito do cliente: descartar n8n |
| 2 | Postgres próprio no Railway (`Dockerfile.db`, pgvector), **privado** | sem n8n externo, não há motivo para expor |
| 3 | Novo canal `evolution` no harness | mantém instância e número atuais intactos |
| 4 | Gate de ingestão na API, antes de enfileirar | `fromMe` é eco; não pode virar mensagem processada |
| 5 | Follow-up como task asyncio no Worker + `FOR UPDATE SKIP LOCKED` | evita serviço novo; advisory lock não sobrevive a pool de conexões |
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
│ followup.py  ← asyncio + SKIP LOCKED, a cada 5 min    │
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
| `worker/followup.py` | cron de 5 min, reivindica com `SKIP LOCKED` | `leads`, cliente outbound |
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
  tests/unit/test_merge_duplicatas.py
  tests/integration/test_webhook_evolution.py
  tests/integration/test_followup.py

TOCADOS
  shared/models.py      → MessagingChannel.EVOLUTION
  shared/config.py      → settings de Evolution, ChatWoot, Google, Pipedrive
  worker/processor.py   → envio de múltiplos balões
  worker/media.py       → download_media por canal (Evolution usa base64)
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
  phone                TEXT PRIMARY KEY
                       CHECK (phone ~ '^[0-9]{8,15}$'),   -- só dígitos, E.164 sem '+'
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

-- Leads cujo telefone não converge para o formato canônico. Não se perde nada
-- em silêncio: fica aqui para inspeção manual.
CREATE TABLE leads_descartados (
  phone_original TEXT,
  motivo         TEXT,
  payload        JSONB,
  created_at     TIMESTAMPTZ DEFAULT now()
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

Leitura única, sem dependência permanente. **Não é cópia direta** — a chave
precisa ser normalizada e as duplicatas fundidas antes de entrar num banco onde
`phone` é PK canônica.

**1. Normalizar** — para cada linha: remover tudo que não é dígito e o `+`; se
tiver 10–11 dígitos, prefixar `55`; se casar `^55(\d{2})9(\d{8})$`, remover o 9º
dígito. Brasileiro converge para `55DDXXXXXXXX`.

Números que **não** são brasileiros passam adiante só com os dígitos, sem
canonicalização — a regra do 9º dígito é brasileira e aplicá-la a um número
estrangeiro o corromperia. Medição: 3.301 dos 3.319 são BR válidos e **nenhum**
é estrangeiro legítimo; os ~18 restantes são erros de digitação do formulário
(DDIs impossíveis como `98`, `99`, `67` com 9 dígitos).

O que não vira nem BR canônico nem sequência plausível de 8–15 dígitos vai para
`leads_descartados` com o motivo — **nada some em silêncio**. Um deles está em
`qualificado` (lead ativo) e deve ser resolvido manualmente antes do cutover, não
descartado.

**2. Fundir duplicatas** — agrupar pelo telefone canônico. Regra de merge,
determinística e nesta ordem:

| Campo | Regra |
|---|---|
| `phase` | `agendou_sessao` > `desqualificado` = `perdido` > `qualificado` > `iniciou_conversa` > `formulario_preenchido`. `NULL` perde de qualquer fase |
| `last_interaction_at`, `created_at` | mais recente / mais antigo, respectivamente |
| `pipedriveid`, `email`, `name`, `username`, `source` | primeiro valor não-nulo, priorizando a linha de fase mais avançada |
| `followup_count` | o maior |
| `agent_active`, `followup_active` | `false` vence — se qualquer linha está pausada, o lead fica pausado |
| `metadata` | merge dos JSONB, com `linhas_fundidas` guardando os telefones originais |

`agent_active = false` vencendo é deliberado: errar para o lado de não mandar
mensagem é recuperável; errar para o lado de mandar para quem pediu silêncio,
não.

**`agent_reactivate_at` não entra no coalesce.** Ali o `NULL` é estado
significativo — "nenhuma reativação agendada" — e não ausência de dado.
Preenchê-lo com o valor da linha perdedora ressuscita um handover expirado.
Ele acompanha a linha que ganhou o `agent_active`.

**`agendou_sessao` vence `perdido` e `desqualificado`** porque reunião agendada
é fato verificável — existe evento no Google Calendar — enquanto as duas fases
terminais são julgamento. Um `perdido` velho não pode enterrar um lead que
marcou reunião depois.

**3. Histórico** — últimas **12 mensagens** por `session_id` →
`legacy_chat_history`, com o telefone já canonicalizado. Verificado: as 736
sessões casam exatamente com um lead e 735 já estão em formato canônico; a
exceção é tratada pela mesma normalização.

**4. Blocklist** — os 28 números hardcoded em `Filtro e Permissao v002` →
tabela `blocklist`, também canonicalizados.

**Validações obrigatórias pós-migração**, todas bloqueantes:

- soma de leads na origem = leads migrados + descartados
- nenhuma linha em `leads_crm` fora do padrão `^55\d{10}$`
- contagem por `phase` no destino ≥ contagem na origem para as fases avançadas
  (a fusão só pode promover, nunca rebaixar)
- 100% dos `session_id` de `legacy_chat_history` existem em `leads_crm`
- relatório do que foi fundido, revisado por humano antes do cutover

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
| `CONTEXT_STRATEGY` | `trim` | equivale a `contextWindowLength: 12` |
| `TRIM_KEEP_TURNS` | **`6`** | 6 turnos ≈ 12 mensagens; o harness conta turnos, não mensagens |
| `MEMORY_ENABLED` | `false` | paridade |
| Saída | `{messages: [string]}`, `minItems: 1` | idem n8n |
| `thread_id` | `{phone_canonico}:elevec_sdr` | convenção do harness |

### Como a saída estruturada é obtida

**Não usar `response_format` nativo do LangGraph no mesmo agente que tem tools.**
Estruturar a resposta e chamar ferramentas no mesmo turno é fonte conhecida de
conflito: o modelo tende a devolver o JSON em vez de chamar a tool, ou a quebrar
o schema quando há tool call pendente.

O n8n não faz isso — o `outputParserStructured` **parseia o texto final** depois
que o ciclo de tools terminou. Replicamos o mesmo mecanismo: o agente roda o
loop normal de tools e, ao final, o conteúdo da última `AIMessage` é parseado
como JSON contra o schema. É o comportamento que está em produção hoje, e evita
o conflito por construção.

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

## Mídia (áudio e imagem)

A transcrição e a descrição já existem no harness (`worker/media.py`, via
`OPENROUTER_MIDIA_MODEL`) e são reaproveitadas sem mudança. **O download não.**

`download_media` (`worker/media.py:50`) é específico do Twilio: autentica com
`twilio_api_key_sid`/`twilio_api_key_secret` e faz um `GET` direto na URL. Isso
não funciona para a Evolution — a URL que chega em `message.audioMessage.url` é
mídia **criptografada do Baileys**, inútil num `GET` simples. É por isso que o
n8n tem os nós `Get Audio` → `Base64` antes de transcrever.

Mudança necessária: `download_media` passa a receber o canal e ganha um caminho
para Evolution, usando `POST /chat/getBase64FromMediaMessage/{instance}` com o
header `apikey`, que devolve o conteúdo já decifrado em base64.

Sem isso, áudio e imagem — que o agente processa hoje — falhariam em produção.

## Gate de ingestão

Ordem exata, replicando o SQL `Add New Lead`:

1. **Resolver telefone** — pontua `remoteJidAlt` e `remoteJid`: base 2 se tiver
   12–14 dígitos, +2 se contiver `@s.whatsapp.net`, +1 se começar com `55`.
   Maior score vence. Sem candidato válido → descarta.
2. **Blocklist** — **igualdade** sobre o telefone canônico, não sufixo. O n8n usa
   `endsWith` porque compara formatos heterogêneos; com os dois lados
   canonicalizados na mesma função, sufixo só adiciona risco de bloquear
   terceiro por coincidência de final. Casou → descarta.
3. **Variações do 9º dígito** — `^55(\d{2})(\d{8,9})$` gera `phone_v1` (com 9) e
   `phone_v2` (sem 9).
4. **`fromMe = true`** — humano respondeu pelo celular: `agent_active = false`,
   `followup_active = false`, `agent_reactivate_at = NULL`, descarta.
5. **Ler o lead e checar `agent_active`** — se `false`, descarta **sem escrever
   nada**.
6. **Upsert** — busca por qualquer variação, **grava sempre `phone_v2`**,
   atualiza `last_interaction_at`, zera `followup_count`, promove
   `formulario_preenchido → iniciou_conversa`.
7. **Enfileira** com `channel = 'evolution'`.

> **A ordem entre 5 e 6 importa e é fácil de errar.** No SQL do n8n, o CTE
> `gate` guarda o `UPDATE` (`WHERE gate.should_continue = 1`): um lead com
> `agent_active = false` **não** tem `last_interaction_at` renovado nem
> `followup_count` zerado. Se o upsert viesse antes da checagem, todo lead em
> handover teria o contador de follow-up reiniciado a cada mensagem recebida —
> e ao ser reativado, receberia a escada de follow-up do zero. A checagem vem
> antes da escrita.

## Follow-up

Task asyncio no Worker, a cada 5 minutos.

**Exclusão mútua sem advisory lock.** O advisory lock do Postgres é *por
sessão*: com pool de conexões, a conexão volta ao pool ainda segurando o lock, e
se for reciclada o lock evapora sem aviso. Corretude exigiria uma conexão
dedicada fora do pool — complexidade desnecessária.

**Reivindicar e só então enviar.** Uma única instrução atômica seleciona e marca
os leads, e o envio acontece **depois**, fora da transação:

```sql
UPDATE leads_crm
SET followup_count = followup_count + 1,
    last_interaction_at = now()
WHERE phone IN (
  SELECT phone FROM leads_crm
  WHERE followup_active
    AND agent_active
    AND phase NOT IN ('agendou_sessao','desqualificado','perdido','qualificado')
    AND (
      (followup_count = 0 AND last_interaction_at < now() - interval '15 minutes')
      OR (followup_count = 1 AND last_interaction_at < now() - interval '1 hour')
      OR (followup_count = 2 AND last_interaction_at < now() - interval '23 hours')
    )
  ORDER BY last_interaction_at
  LIMIT 10
  FOR UPDATE SKIP LOCKED
)
RETURNING phone, name, followup_count;
```

O `followup_count` devolvido pelo `RETURNING` já é o **novo** valor (1, 2 ou 3),
que é exatamente o nível da mensagem a enviar — o mesmo `next_level` que o n8n
calcula com `followup_count + 1`.

Duas réplicas nunca pegam o mesmo lead, e **nenhuma transação fica aberta
durante uma chamada HTTP** — que é o defeito de segurar `FOR UPDATE` enquanto se
espera a Evolution responder.

> **Divergência consciente do n8n.** Lá o `Atualizar Status` roda *depois* do
> envio: se o envio falha, o contador não sobe e a próxima rodada tenta de novo.
> Aqui o contador sobe antes, então uma falha de envio faz o lead **pular um
> nível** de follow-up. É a troca certa: perder um follow-up é irrelevante;
> mandar a mesma mensagem duas vezes para um lead, não.

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
  `remoteJidAlt`, blocklist por igualdade canônica (inclusive o caso em que o
  mesmo número chega com `+`, com 9 e sem 9)
- `test_gate_ingestao.py` — `fromMe`, `agent_active=false`, blocklist,
  promoção de phase
- `test_saida_baloes.py` — parse do `{messages:[...]}`, fallback de balão único
- `test_merge_duplicatas.py` — normalização dos 4 formatos reais (`+`, com 9,
  sem 9, malformado), precedência de `phase`, `agent_active=false` vencendo,
  e o caso observado de 3 linhas para a mesma pessoa

**Integração:**
- `test_webhook_evolution.py` — payload real, enfileiramento correto
- `test_followup.py` — os 3 níveis com relógio controlado; duas tarefas
  concorrentes reivindicando ao mesmo tempo não podem pegar o mesmo lead; e
  falha de envio deixa o contador já incrementado (divergência documentada)

## Deploy no Railway

Quatro serviços (`docs/RAILWAY.md` cobre a mecânica):

| Serviço | Dockerfile | Notas |
|---|---|---|
| `db` | `Dockerfile.db` | pgvector, volume em `/var/lib/postgresql/data`, **privado** |
| `api` | `Dockerfile.api` | público; recebe webhooks Evolution e ChatWoot; **1 réplica** na v1 |
| `worker` | `Dockerfile.worker` | privado; **1 réplica** na v1 |
| `frontend` | `Dockerfile.frontend` | painel admin |

> `docs/RAILWAY.md` sugere 2 réplicas para a API, mas o rate limit do harness é
> **em memória**: com 2 réplicas o limite efetivo por telefone dobra
> silenciosamente (30/h vira até 60/h). Na v1 fixamos 1 réplica. Escalar exige
> antes mover o rate limit para o Postgres — fora do escopo desta entrega.

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
3. **Periféricos** — follow-up com reivindicação atômica, webhook ChatWoot
4. **Migração de dados** — `migrar_supabase.py`, fusão de duplicatas, relatório
   de merge revisado por humano, validações bloqueantes
5. **Cutover** — na ordem:
   1. desligar os 6 workflows n8n
   2. rodar a migração e conferir as validações
   3. repontar o webhook da instância `instancia-apioficial` para
      `https://<api>/webhook/evolution?agent=elevec_sdr`
   4. **repontar o webhook do ChatWoot** para `https://<api>/webhook/chatwoot`
      — sem este passo a etiqueta `pausar_agente` para de funcionar em silêncio,
      e o único sinal seria o agente respondendo por cima de um humano
   5. conferir `max(last_interaction_at)` antes e depois
   6. monitorar a primeira hora com a fila à vista

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
| Fusão de duplicatas escolhe a linha errada | alto | regra determinística; relatório revisado por humano antes do cutover; linhas originais preservadas em `metadata.linhas_fundidas` |
| Rotação do OAuth Client do Google | alto | documentado; ninguém rotaciona sem aviso |
| Cutover perde mensagens | médio | fora do horário comercial; conferir `max(last_interaction_at)` antes/depois |
| Divergência de canonicalização | médio | `phone.py` único, com testes |
| Esquecer de repontar o ChatWoot | médio | passo explícito no cutover; falha é silenciosa |
| Grok-4.3 responder fora do schema | baixo | parse do texto final (não `response_format`) + retry + fallback de balão único |
| Follow-up duplicado sob réplicas | baixo | `FOR UPDATE SKIP LOCKED` |

### Riscos avaliados e descartados

Levantados na revisão adversarial e **refutados com consulta ao banco** — ficam
registrados para não serem reinvestigados:

- **Enxurrada de follow-up no cutover.** Zero leads elegíveis pela regra atual
  no momento da medição; 1.044 dos leads ativos já estão em `followup_count = 3`
  (esgotados). Não há represa acumulada.
- **`session_id` do histórico não casar com o lead canônico.** As 736 sessões
  casam exatamente com uma linha de `leads_crm`, e 735 já estão no formato
  canônico de 12 dígitos.

## A porta de entrada: template e segmentação

Levantado em 26/07/2026, depois do desenho original. **Muda o papel da Renata no
sistema** e o escopo das Fases 3 e 4.

### A janela de 24h condiciona tudo

A integração é a Cloud API oficial. Sem uma mensagem de entrada recente do lead,
**texto livre é rejeitado pela Meta** — só template aprovado alcança quem não
escreveu primeiro. Duas consequências:

- **O degrau de 23 horas do follow-up encosta no limite.** Se o relógio virar
  antes do envio (retry, fila lenta, worker ocupado), a mensagem é rejeitada,
  não atrasada.
- **Lead de formulário nunca abriu janela.** É o template que puxa a conversa.

### O template

Endpoint `POST /message/sendTemplate/{instance}` — **não** o `sendText` que a
Fase 1 implementou. Um parâmetro só, o primeiro nome, no header:

```json
{ "number": "<telefone>", "language": "pt_BR",
  "name": "boas_vindas_renata_respondiapp_03",
  "components": [{"type": "header",
                  "parameters": [{"type": "text", "text": "<primeiro nome>"}]}] }
```

Dois templates ativos, um por origem — o que casa com o enum `lead_source`:

| Template | Workflow | Origem |
|---|---|---|
| `boas_vindas_renata_linkedin_02` | `#00 ZAPIER \| Formulário Linkedin` | `linkedin_form` |
| `boas_vindas_renata_respondiapp_03` | `YAY FORMS`, `#00 ZAPIER` | `respondiapp_form` |

Quando o lead responde ao template, ele entra pelo webhook normal e cai na
Renata — não há roteamento especial (confirmado com o cliente).

### A Renata atende um segmento, não todos os leads

O `YAY FORMS` classifica por faturamento declarado no formulário e roteia:

| Faixa | Destino |
|---|---|
| Menos de R$ 3 mil | desqualificado |
| R$ 3 a 5 mil | desqualificado |
| **R$ 5 a 8 mil, que NÃO agendou** | **template → Renata** |
| R$ 8 a 25 mil | closer humano (Silvio ou Ivana), com rodízio |
| Acima de R$ 25 mil | Silvio direto |

Ainda com um `if` antes: só se o telefone do formulário não estiver vazio.

**A Renata é o caminho de recuperação de uma faixa específica, não a porta de
entrada.** Faturamento alto vai direto para humano; baixo é descartado. Ela pega
quem está no meio e não agendou sozinho — o que explica o portão de faturamento
do SOP: o lead **já declarou** a faixa no formulário, e ela confirma na conversa.

### Impacto nas fases

- **Fase 3** precisa de `sendTemplate` no `EvolutionClient`, não só `sendText`.
  E o follow-up precisa saber se a janela ainda está aberta antes de mandar
  texto livre.
- **Fase 4** pode ter escopo menor que o previsto: se a IA só atende a faixa de
  5-8k sem agendamento, os 2.559 leads em `formulario_preenchido` não são todos
  dela. Conferir a distribuição antes de migrar.

### Registro de segurança

A `apikey` está **hardcoded em texto claro** no nó `WA Template` do `YAY FORMS`,
no campo de header — não vem de credencial do n8n. Ela viaja em qualquer export
ou backup do workflow, e é o mesmo token da Meta que envia mensagem e baixa
mídia. Não afeta a migração (no harness vira variável de ambiente), mas segue
exposta enquanto o n8n existir.

## Fora de escopo

- `#00 ZAPIER | Formulário Linkedin` (71 nós) e `#0 Form` — entrada de leads por
  formulário continua fora do harness nesta etapa
- `Green Webhook` (D4Sign, contratos) — não faz parte do SDR
- Memória semântica — proposta para depois da v1, com medição antes/depois
- Migração do histórico completo (8.912 mensagens) — só a janela de 12 por sessão
