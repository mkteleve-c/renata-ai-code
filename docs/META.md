# Integração Meta WhatsApp Cloud API

Guia completo para configurar inbound + outbound via Meta WhatsApp Cloud API.
Análogo a [`TWILIO.md`](TWILIO.md), mas para o canal Meta.

> Para o passo-a-passo automatizado, use a skill `meta-setup`.
> Este documento é a referência detalhada (conceitos, payloads, troubleshooting).

## Visão geral

```text
Usuário WhatsApp
       │
       ▼
Meta WhatsApp Cloud API (graph.facebook.com)
       │  GET  /webhook/meta?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
       │  POST /webhook/meta?agent=<id>
       │  X-Hub-Signature-256: sha256=<HMAC-SHA256(app_secret, raw_body)>
       ▼
https://${DOMAIN}/webhook/meta  (API FastAPI atrás do Traefik)
       │
       ▼
PostgreSQL (message_queue, channel='meta')
       │
       ▼
Worker  ──► MetaClient.send_message()  ──► Graph API ──► WhatsApp
```

Diferenças vs Twilio:

| Aspecto | Twilio | Meta Cloud API |
|---|---|---|
| Webhook content-type | `application/x-www-form-urlencoded` | `application/json` |
| Assinatura | `X-Twilio-Signature` (HMAC-SHA1) | `X-Hub-Signature-256` (HMAC-SHA256) |
| Identidade do remetente | `From=whatsapp:+5511...` | `messages[].from = "5511..."` (sem `+`) |
| ID da mensagem | `MessageSid=SMxxx...` | `messages[].id = "wamid.HBg..."` |
| Mídia inbound | URL pública (Basic Auth) | `media_id` — exige `GET /{media_id}` na Graph API com Bearer |
| Typing indicator | API dedicada (Beta) | Não existe — usamos `status=read` |
| Texto outbound | até 1600 caracteres | até 4096 caracteres |
| Outbound auth | Basic Auth (API Key) | Bearer (System User Token) |
| Status updates | webhook separado opcional | chegam no MESMO webhook (ignorados pelo parser) |

## Conceitos

| Termo | O que é | Onde aparece |
|---|---|---|
| **Verify Token** | Token que **você inventa**. Meta envia no GET de handshake e a API compara | `META_VERIFY_TOKEN` |
| **App Secret** | Segredo do App Meta. Usado no HMAC-SHA256 do body | `META_APP_SECRET` |
| **Phone Number ID** | ID do número Business (NÃO é o número em E.164!) | `META_PHONE_NUMBER_ID` |
| **System User Access Token** | Bearer permanente com permissão `whatsapp_business_messaging` | `META_ACCESS_TOKEN` |
| **WABA ID** | WhatsApp Business Account ID (auditoria/logs) | (não usamos) |

## Pré-requisitos

- Stack rodando com `${DOMAIN}` público e TLS válido (skills `infra-setup` + `domain-setup` + `deploy`).
- Conta Meta Business Manager + App com produto WhatsApp.
- Número WhatsApp Business adicionado ao app.

## 1. Coletar credenciais no painel do Meta

1. **App Secret** — `developers.facebook.com/apps` → seu App → **Settings → Basic** → "App Secret" → "Show".

2. **Phone Number ID** — **WhatsApp → API Setup**, dropdown "From". O "Phone number ID" é numérico longo, **não** o número em E.164.

3. **System User Access Token (PERMANENTE)**:

   a. `business.facebook.com/settings` → **Users → System Users**
   b. Crie/selecione um System User com role "Admin"
   c. **Add Assets** → adicione seu App Meta com permissão "Manage app"
   d. **Generate New Token** → escolha o App → marque `whatsapp_business_messaging` (e opcional `whatsapp_business_management`)
   e. **Copie o token AGORA** — só é exibido uma vez

   Prefira System User Token a qualquer "test token" do Console — testes vencem em 24h.

4. **Verify Token (você define)** — `openssl rand -hex 32`. O **mesmo valor** vai no `.env.prod` e no painel Meta.

## 2. Preencher `.env.prod`

```bash
cd deploy
nano .env.prod
```

```bash
# Modo outbound — em produção, sempre real
OUTBOUND_MODE=real

# Inbound (handshake + assinatura)
META_VERIFY_TOKEN=<token gerado por você>
META_APP_SECRET=<App Secret do Meta>
META_VALIDATE_SIGNATURE=true

# Outbound (Graph API)
META_PHONE_NUMBER_ID=<Phone Number ID>
META_ACCESS_TOKEN=<System User Token>
META_GRAPH_API_VERSION=v23.0
```

Para usar **só Meta**, deixe `TWILIO_*` e `UAZAPI_*` vazios — canais sem credenciais ficam desabilitados. Para usar **Meta + Twilio + uazapi simultaneamente**, preencha os três; o roteamento por mensagem é automático (`message_queue.channel`).

> **Fail-fast em modo real**: o boot da API e do Worker falha se Meta foi "tocado parcialmente" — ex: `META_VERIFY_TOKEN` preenchido mas `META_PHONE_NUMBER_ID` vazio. Para desabilitar, **zere todas** as credenciais Meta.

## 3. Restart dos serviços

```bash
cd deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
# api e worker são recriados (compose detecta diff no env)
```

## 4. Validar handshake LOCALMENTE antes de cadastrar no Meta

O Meta dispara o GET de handshake assim que você clica "Verify and save" — se a API responder errado, o cadastro falha e você precisa corrigir antes de tentar de novo. Faça o teste antes:

```bash
DOMAIN=$(grep ^DOMAIN= deploy/.env.prod | cut -d= -f2-)
VERIFY=$(grep ^META_VERIFY_TOKEN= deploy/.env.prod | cut -d= -f2-)

curl -i "https://$DOMAIN/webhook/meta?hub.mode=subscribe&hub.verify_token=$VERIFY&hub.challenge=ping123"
```

Esperado:

```
HTTP/2 200
content-type: text/plain; charset=utf-8

ping123
```

Erros comuns:

- `403 Verify token mismatch` — `META_VERIFY_TOKEN` no `.env.prod` não bate com o que você está enviando no curl
- `403 Invalid hub.mode` — querystring sem `hub.mode=subscribe`
- `500 Meta verify token not configured` — variável não setada no container (recriar `up -d`)

## 5. Cadastrar webhook no painel do Meta

1. `developers.facebook.com/apps` → seu App → **WhatsApp → Configuration**.
2. **Webhook → Edit**:
   - **Callback URL**: `https://${DOMAIN}/webhook/meta?agent=<agent_id>` (ex: `?agent=rhawk_assistant`)
   - **Verify token**: o **mesmo** valor de `META_VERIFY_TOKEN`
3. Click **Verify and save** — Meta dispara o GET imediatamente. Passou → "Connected".
4. **Webhook fields**: marcar `messages` (essencial). Opcional: `message_template_status_update`, `phone_number_quality_update`.
5. **Subscribed apps**: garantir que o App está subscrito ao número Business.

## 6. Smoke test produtivo

Envie uma mensagem do seu WhatsApp pessoal para o número Business cadastrado.

```bash
cd deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f api worker | \
  grep -E "(meta_|webhook_meta|message_id|status=done)"
```

Sequência esperada:

1. `webhook_meta_received` — API recebeu
2. `message_enqueued` ou `message_buffered` — entrou na fila com `channel='meta'`
3. `message_claimed` — worker pegou
4. `meta_marked_as_read` — best-effort (substitui o typing dos outros canais)
5. `meta_message_sent` — resposta enviada via Graph API
6. `message_done` — fila marcou done

Se algum passo falta, abra a skill `debug-queue`.

## Validação de assinatura

A cada POST, a Meta calcula `HMAC-SHA256(META_APP_SECRET, raw_body)` e envia o resultado em `X-Hub-Signature-256: sha256=<hex>`.

Nossa API:

1. lê o **raw body** antes de parsear JSON (a ordem importa)
2. recalcula o HMAC com `META_APP_SECRET`
3. compara com `hmac.compare_digest`

```python
expected = hmac.new(
    settings.meta_app_secret.encode("utf-8"),
    raw_body,
    hashlib.sha256,
).hexdigest()
```

Pegadinhas:

| Sintoma | Causa | Correção |
|---|---|---|
| 403 `Invalid signature` em todos os requests | App Secret errado | Confirme que pegou de **Settings → Basic** do mesmo App; não confunda com Client Token |
| 403 esporádico | Body modificado por proxy/CDN | Traefik não modifica; investigue se há CDN no caminho |
| 403 `Missing X-Hub-Signature-256` | Header faltando | Não é o Meta — é alguém testando o endpoint sem assinar |
| 403 `Invalid signature format` | Header não começa com `sha256=` | Idem |
| 500 `Meta app secret not configured` | `META_APP_SECRET` vazio mas `META_VALIDATE_SIGNATURE=true` | Preencher ou desligar a validação (não recomendado em prod) |

Para investigar temporariamente: defina `META_VALIDATE_SIGNATURE=false`, restart, reproduza o erro, **volte para `true` antes de seguir em produção**.

## Payload inbound (para parser/agente)

Estrutura simplificada do POST:

```json
{
  "object": "whatsapp_business_account",
  "entry": [{
    "changes": [{
      "value": {
        "metadata": {
          "phone_number_id": "1234567890",
          "display_phone_number": "+5511..."
        },
        "messages": [{
          "from": "5511999999999",
          "id": "wamid.HBgMNTUx...",
          "type": "text",
          "text": {"body": "Olá"}
        }]
      }
    }]
  }]
}
```

O parser (`server/routes/webhook_meta.py`):

- ignora `object != "whatsapp_business_account"` (200 ignored)
- ignora `value.statuses` quando não há `messages` (status updates: delivered/read/sent)
- normaliza `from` adicionando `+` quando vem sem
- extrai `text`, `image`, `audio`, `video`, `document`, `sticker` — para mídia, captura `caption` quando existe e marca placeholder porque o download via Graph não está implementado
- `location`, `contacts`, `interactive`, `button`, `reaction` viram placeholder de "tipo X não suportado"
- enfileira com `channel=MessagingChannel.META`, `to_number=display_phone_number`, `message_id=wamid`

## Outbound — Graph API

`MetaClient.send_message(to, body)`:

```http
POST https://graph.facebook.com/v23.0/{phone_number_id}/messages
Authorization: Bearer {access_token}
Content-Type: application/json

{
  "messaging_product": "whatsapp",
  "to": "5511999999999",
  "type": "text",
  "text": {"body": "..."}
}
```

- `to` vai sem `+` e sem `whatsapp:` (`MetaClient` faz a normalização)
- mensagens > 4096 caracteres são divididas em chunks (separadores: `\n\n` → `\n` → ` ` → corte forçado)
- retorna o `wamid` da última chunk enviada
- erros 4xx/5xx levantam `MetaSendError(status_code, detail)` — entram no fluxo de retry com backoff

`MetaClient.send_typing(to, message_sid)` chama `POST /messages` com `status=read` — substitui o typing dos outros canais (a Cloud API não expõe API equivalente).

## Limitações conhecidas

- **Mídia inbound não baixa via Graph**: payload chega com `media_id`, parser só captura `caption` e adiciona placeholder. Para ativar, estender `worker/media.py` para resolver `media_id → URL temporária` (`GET /{media_id}`) e baixar com Bearer.
- **Templates outbound (HSM)** não enviados — só respondemos dentro da janela de 24h após mensagem do usuário.
- **Reactions, location, contacts, interactive, button**: o webhook recebe, mas o parser marca como "tipo X não suportado" e o agente vê apenas o placeholder.

## Cutover e rollback

Como o roteamento é por mensagem (`message_queue.channel`), habilitar/desabilitar Meta é só preencher/zerar credenciais.

### Cutover Twilio → Meta (mantendo os dois temporariamente)

1. Preencher os 4 envs `META_*`; manter `TWILIO_*` preenchido.
2. `docker compose ... up -d` — worker recria com clientes Twilio + Meta.
3. Cadastrar webhook no Meta (passos 4–5 acima).
4. Smoke test pelo número Meta.
5. **Desativar webhook no Twilio Console** (mensagens param de chegar lá).
6. Para parar 100% Twilio: zerar credenciais e `up -d`.

Mensagens em voo: as que estão na fila com `channel='twilio'` continuam sendo respondidas pelo cliente Twilio (que segue habilitado).

### Desabilitar Meta

1. Zerar as 4 envs `META_*`.
2. `docker compose ... up -d`.
3. **Desativar webhook no Meta** (Webhook → Edit → desmarcar `messages`).

## Documentos relacionados

- [`TWILIO.md`](TWILIO.md) — canal Twilio
- [`UAZAPI.md`](UAZAPI.md) — canal uazapi/uazapiGO
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — visão multi-canal
- [`DEPLOY.md`](DEPLOY.md) — deploy oficial Docker + Traefik
- skill `meta-setup` — passo-a-passo automatizado
- skill `debug-queue` — troubleshooting da fila
