# Integração uazapi (uazapiGO)

Guia de configuração do canal uazapi neste projeto. Foca em **como ligar sua
instância no harness** — para detalhes completos da API uazapi (endpoints,
schemas, comportamentos), use a documentação oficial da uazapi e o código do
parser em `src/whatsapp_langchain/server/routes/webhook_uazapi.py` + cliente
em `src/whatsapp_langchain/worker/uazapi_client.py`.

## O que é uazapi

API HTTP **não-oficial** baseada em [Baileys](https://github.com/WhiskeySockets/Baileys), a biblioteca que conversa direto com o protocolo WhatsApp Web. Cada cliente da uazapi roda numa **instância dedicada**, com subdomínio próprio (ex: `https://meucliente.uazapi.com`) e um **token de instância** que autentica todas as chamadas.

Diferenças importantes vs Twilio/Meta:

| Aspecto | Twilio / Meta | uazapi |
|---|---|---|
| Oficial? | Sim | Não — Baileys/WhatsApp Web |
| Aprovação WhatsApp Business | Obrigatória (Meta) ou via Twilio | Não exige |
| Custo | Por mensagem ou plano | Plano fixo da instância |
| URL outbound | endpoint global | subdomínio por instância |
| Auth outbound | Basic Auth / Bearer | header `token` (token da instância) |
| Auth inbound (assinatura) | HMAC-SHA1 / HMAC-SHA256 | **não há** — defesa = token no payload |
| Mídia inbound | URL pública (Twilio) ou `media_id` (Meta) | URL pública (`fileURL`) |
| Templates HSM | Suportados | Não existem (mensagens livres dentro de WhatsApp Web) |
| Estabilidade | Alta | Depende da uazapi e do estado da sessão Baileys |

> **Risco operacional**: por usar Baileys, a uazapi está sujeita a banimentos do número pelo WhatsApp se o uso não respeitar limites de envio/comportamento. Use com critério, principalmente em volume alto. Os outros canais (Twilio/Meta) são preferíveis para operação crítica.

## Visão geral

```text
Usuário WhatsApp
       │
       ▼
uazapi (instância: https://meucliente.uazapi.com)
       │  POST /webhook/uazapi?agent=<id>
       │  body JSON com EventType, message, chat, token, ...
       ▼
https://${DOMAIN}/webhook/uazapi  (API FastAPI atrás do Traefik)
       │   - filtra fromMe / isGroup
       │   - extrai phone, body, mídia, instance_token
       │   - persiste em message_queue (channel='uazapi', outbound_token=token)
       ▼
PostgreSQL
       │
       ▼
Worker  ──► UazapiClient.send_message(to, body, token=outbound_token)
       │   POST {base_url}/send/text
       │   header: token: <instance_token>
       ▼
uazapi  ──► WhatsApp
```

## Conceitos

| Termo | O que é | Onde aparece |
|---|---|---|
| **Instância** | Sessão Baileys associada a um número WhatsApp. Tem subdomínio próprio | `UAZAPI_BASE_URL` |
| **Token da instância** | Token que autentica chamadas outbound (header `token`). Devolvido por `POST /instance/init` no provisionamento | `UAZAPI_INSTANCE_TOKEN` (fallback estático) ou `message_queue.outbound_token` (vindo do payload por mensagem) |
| **EventType** | Tipo de evento do webhook (`messages`, `messages_update`, `connection`, ...) | parser ignora tudo que não seja `messages` |
| **fromMe** | Mensagem foi enviada pela própria instância (loop guard) | filtrado no parser |

A uazapi inclui o token no **próprio payload** de cada webhook (campo `token` top-level). Fazemos isso por mensagem em vez de configurar via env porque:

1. Suporta multi-instância sem mexer em config (várias instâncias mandando webhook pra mesma URL com agentes diferentes).
2. Rotação de token não exige reiniciar o worker.
3. Permite que o payload seja a fonte da verdade no caso de troca de instância no painel uazapi.

## Pré-requisitos

- Stack rodando com `${DOMAIN}` público e TLS válido (skills `infra-setup` + `domain-setup` + `deploy`).
- Conta uazapi com pelo menos uma instância criada e conectada (QR code escaneado).
- Acesso ao painel da instância para configurar webhook URL e ler o token.

## 1. Coletar dados no painel uazapi

1. **`UAZAPI_BASE_URL`**: subdomínio da instância (ex: `https://meucliente.uazapi.com`). Sem barra final.
2. **Token da instância**: copie do painel ou de `POST /instance/init`. Esse token vai aparecer no payload do webhook em cada mensagem; preencher como env é **opcional** (fallback caso o webhook não traga).

## 2. Preencher `.env.prod`

```bash
cd deploy
nano .env.prod
```

```bash
OUTBOUND_MODE=real

# Subdomínio da instância — obrigatório
UAZAPI_BASE_URL=https://meucliente.uazapi.com

# Fallback estático opcional. O token chega no payload do webhook e é
# persistido em message_queue.outbound_token; este aqui só é usado quando
# o payload de uma mensagem específica não trouxer o token.
UAZAPI_INSTANCE_TOKEN=
```

> **Habilitar uazapi = preencher `UAZAPI_BASE_URL`**. Para desabilitar, deixe vazio. Com `OUTBOUND_MODE=real`, o boot falha se o canal estiver "tocado parcialmente" (algum env de outro canal preenchido pela metade).

Para uso multi-canal (uazapi + Twilio + Meta simultaneamente), preencha as credenciais dos três; o roteamento por `message_queue.channel` é automático.

## 3. Restart dos serviços

```bash
cd deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

No log do worker, deve aparecer:

```
uazapi_client_ready  outbound_mode=real  base_url=https://meucliente.uazapi.com  has_static_token=False
```

## 4. Configurar webhook no painel uazapi

No painel da instância, configure:

- **URL do webhook**: `https://${DOMAIN}/webhook/uazapi?agent=<agent_id>`
  - Ex: `https://template.vps.illumiai.com/webhook/uazapi?agent=rhawk_assistant`
- **Eventos**: minimamente `messages`. Você pode habilitar mais — eventos diferentes de `messages` são ignorados com 200.
- **Filtro recomendado**: `excludeMessages: ["wasSentByApi"]` — evita loops com mensagens enviadas pelo próprio worker.

> O parser também filtra `fromMe=true` independentemente, mas ligar o filtro no painel reduz tráfego.

## 5. Smoke test

Envie uma mensagem do seu WhatsApp pessoal para o número da instância uazapi.

```bash
cd deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f api worker | \
  grep -E "(uazapi_|webhook_uazapi|message_id|status=done)"
```

Sequência esperada:

1. `webhook_uazapi_received` — API recebeu o evento `messages`
2. `message_enqueued` ou `message_buffered` — entrou na fila com `channel='uazapi'` e `outbound_token=<token>`
3. `message_claimed` — worker pegou
4. `uazapi_typing_sent` — best-effort: presence=composing
5. `uazapi_message_sent` — resposta enviada via `/send/text`
6. `message_done` — fila marcou done

Se algum passo falta, veja a skill `debug-queue`.

## Hardening (sem assinatura)

A uazapi **não assina** o body do webhook. Defesas disponíveis:

1. **Token no payload (default)**: sem o token correto, o worker não consegue responder no canal certo — uma mensagem forjada sem token nem chega a fechar o ciclo (é enfileirada e marcada como failed por ausência de token).
2. **Restringir por IP no Traefik** (opcional): se você sabe os IPs públicos dos servidores uazapi, adicione um middleware de IP allowlist na rota.
3. **Header secreto adicional** (opcional): a uazapi suporta header customizado no webhook config. Adicione e valide no FastAPI antes de passar para o parser.

Os dois últimos exigem editar `docker-compose.prod.yml` ou o webhook handler — não são default no template.

## Payload inbound (formato real auditado)

A uazapi entrega:

```json
{
  "EventType": "messages",
  "BaseUrl": "https://meucliente.uazapi.com",
  "instanceName": "<id>",
  "owner": "<owner id>",
  "token": "<instance token — usado para outbound>",
  "chatSource": "...",
  "chat": {
    "wa_chatid": "5511999999999@s.whatsapp.net",
    "phone": "5511999999999",
    "name": "Fulano",
    "wa_isGroup": false,
    ...
  },
  "message": {
    "messageid": "ABCD...",
    "chatid": "5511999999999@s.whatsapp.net",
    "fromMe": false,
    "isGroup": false,
    "messageType": "Conversation",
    "text": "Olá",
    "fileURL": null,
    "sender": "5511999999999@s.whatsapp.net",
    "senderName": "Fulano"
  }
}
```

O parser (`server/routes/webhook_uazapi.py`):

- aceita formato atual (`EventType`/`message`/`chat`) e legado (`event`/`data`)
- aceita `message` como objeto único ou lista
- ignora eventos que não sejam `messages` (200 ignored)
- filtra `fromMe=true` e mensagens de grupo (`isGroup` ou `wa_isGroup`)
- extrai `chatid` em ordem: `message.chatid` → `message.sender` → `chat.wa_chatid` → `chat.phone`
- normaliza para `+E.164` (descarta JIDs `@g.us`)
- extrai `instance_token` em ordem: `payload.token` → `payload.instance_token` → `payload.instanceToken` → `data.token` → `data.instance_token` → `data.owner_token` → fallback `UAZAPI_INSTANCE_TOKEN`
- mapeia `messageType` (case-insensitive): `conversation`/`extendedtextmessage`/`text` → texto; `imagemessage` → `image/jpeg`; `audiomessage`/`pttmessage` → `audio/ogg`; `videomessage` → `video/mp4`; `documentmessage` → `application/octet-stream`
- enfileira com `channel=MessagingChannel.UAZAPI` e `outbound_token=<token>`

## Outbound — `/send/text` e `/message/presence`

`UazapiClient.send_message(to, body, token=outbound_token)`:

```http
POST {base_url}/send/text
token: {instance_token}
Content-Type: application/json

{"number": "5511999999999", "text": "..."}
```

- `to` vai como dígitos (sem `+`, sem `whatsapp:`, sem sufixo `@s.whatsapp.net` — o cliente normaliza)
- mensagens > 4096 caracteres são divididas em chunks (mesmo splitter do Meta)
- retorna o `id` da última chunk enviada
- erros 4xx/5xx levantam `UazapiSendError(status_code, detail)` — entram no fluxo de retry com backoff
- **prioridade do token**: parâmetro `token=` da chamada > `instance_token` do construtor (fallback)

`UazapiClient.send_typing(to, message_sid, token=outbound_token)`:

```http
POST {base_url}/message/presence
token: {instance_token}

{"number": "5511999999999", "presence": "composing", "delay": 25000}
```

- presence é assíncrona — uma chamada mantém "digitando..." por até 5min, re-emitindo a cada 10s
- cancelada automaticamente quando enviamos a próxima mensagem para o mesmo chat
- `message_sid` é ignorado (uazapi não tem leitura por messageid neste endpoint)

## Limitações conhecidas

- **Múltiplas mídias num só payload**: nem cobre. Apenas a primeira é processada (igual ao Twilio).
- **Ban risk**: por ser Baileys, a instância pode ser banida pelo WhatsApp em volumes altos ou padrões anômalos.
- **Reconexão**: se o QR cair (logout do WhatsApp Web), o webhook para de chegar até a instância ser reconectada no painel uazapi. Não há fail-fast no nosso lado — você só percebe pela ausência de mensagens.

## Cutover e rollback

Como uazapi é roteado por mensagem (`channel='uazapi'`), conviver com Twilio/Meta é só preencher as credenciais.

### Habilitar uazapi sem mexer em outros canais

1. Preencher `UAZAPI_BASE_URL` no `.env.prod`.
2. `docker compose ... up -d` — worker recria com cliente uazapi adicional.
3. Configurar webhook no painel uazapi para apontar pra `https://${DOMAIN}/webhook/uazapi?agent=<id>`.
4. Smoke test.

### Desabilitar uazapi

1. Zerar `UAZAPI_BASE_URL` (e `UAZAPI_INSTANCE_TOKEN` se preenchido) no `.env.prod`.
2. `docker compose ... up -d`.
3. **Desativar webhook no painel uazapi** (ou apontar para outra URL).

Mensagens em voo na fila com `channel='uazapi'` falharão no envio (cliente desabilitado) e serão marcadas como failed após esgotar retries — drene a fila antes se quiser saída limpa.

## Troubleshooting

| Sintoma | Causa provável | Correção |
|---|---|---|
| Worker reiniciando: "uazapi: nenhum token disponível" | Webhook chegou sem token e `UAZAPI_INSTANCE_TOKEN` vazio | Conferir no painel uazapi se o token está sendo enviado; preencher fallback estático |
| `webhook_uazapi_received skipped=1` mas nada na fila | Mensagem `fromMe=true` ou de grupo | Comportamento esperado — uazapi pode reenviar status updates como `messages` em alguns casos |
| `404` em `/send/text` | `UAZAPI_BASE_URL` errado | Conferir subdomínio correto da instância; sem barra final |
| `401 Unauthorized` em `/send/text` | Token errado/expirado | Renovar token no painel uazapi; o webhook seguinte trará o token novo automaticamente |
| Mensagem some sem ir pra fila | `chatid` em formato inesperado (JID de grupo, sem `@s.whatsapp.net`) | Logs do parser mostram `uazapi_invalid_chatid` |
| Imagem/áudio chega mas não processa | `messageType` não mapeado | Adicionar entrada em `MEDIA_TYPE_MAP` em `webhook_uazapi.py` |

## Documentos relacionados

- [`TWILIO.md`](TWILIO.md) — canal Twilio
- [`META.md`](META.md) — canal Meta WhatsApp Cloud API
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — visão multi-canal
- [`DEPLOY.md`](DEPLOY.md) — deploy oficial Docker + Traefik
- skill `debug-queue` — troubleshooting da fila
- `src/whatsapp_langchain/server/routes/webhook_uazapi.py` — parser do webhook (formato real auditado)
- `src/whatsapp_langchain/worker/uazapi_client.py` — cliente outbound (`/send/text`, `/message/presence`)
