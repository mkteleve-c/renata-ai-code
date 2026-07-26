# Canal Evolution API

Guia do canal `evolution`: como a mensagem entra, como a resposta sai, e o que
configurar para não cair nas armadilhas conhecidas.

## O que é esta integração

A Evolution API é um proxy HTTP para WhatsApp que suporta dois backends. Esta
instância roda o segundo:

| Integração | Por baixo |
|---|---|
| `WHATSAPP-BAILEYS` | WhatsApp Web por engenharia reversa |
| **`WHATSAPP-BUSINESS`** | **Meta Cloud API oficial** — a Evolution só faz de proxy |

Instância: `instancia-apioficial`. Confirmado via `GET /instance/fetchInstances`:

```
name=instancia-apioficial  status=open  integration=WHATSAPP-BUSINESS
number=1025192374009897
```

> O campo `number` é o **Phone Number ID da Meta**, não um telefone em E.164.
> Confundir os dois leva a procurar um número que não existe.

Apesar do backend oficial, **o payload do webhook chega em formato Baileys** —
a Evolution normaliza os dois casos.

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `EVOLUTION_BASE_URL` | sim | URL do servidor, sem barra final |
| `EVOLUTION_API_KEY` | sim | autentica envio e download de mídia |
| `EVOLUTION_INSTANCE` | sim | nome da instância |
| `EVOLUTION_WEBHOOK_SECRET` | não | quando preenchida, a rota exige este valor no header |

### A armadilha de deploy mais provável

**O webhook inbound funciona sem nenhuma credencial.** Ele aceita qualquer
`instance` e enfileira com `channel='evolution'`. As credenciais só são usadas
no *outbound*.

Consequência: se você apontar o webhook da Evolution para a API e deixar as
três variáveis vazias, **o boot passa**. O fail-fast do harness só dispara em
canal *parcialmente* configurado; zero variáveis conta como "canal
desabilitado". Aí toda mensagem entra na fila e morre no worker, uma a uma, com
`Canal 'evolution' não está habilitado`.

O worker emite um aviso no boot quando encontra fila de um canal sem cliente
correspondente. Mas a regra é simples: **as três andam juntas**.

### Não reutilize a API key como secret do webhook

O header do secret e o header de autenticação da Evolution têm nomes
parecidos, o que convida ao erro. Reutilizar o valor de `EVOLUTION_API_KEY` em
`EVOLUTION_WEBHOOK_SECRET` exporia, para quem só deveria poder chamar o
webhook, a credencial que envia mensagens e baixa mídia. Gere um valor
independente:

```bash
openssl rand -base64 32
```

Com a variável vazia a rota fica aberta — aceitável em dev, **não em produção**:
um POST com `fromMe: true` e um telefone qualquer desliga o agente para aquele
lead de forma permanente.

## Entrada: `POST /webhook/evolution?agent=<id>`

Payload típico:

```json
{
  "event": "messages.upsert",
  "instance": "instancia-apioficial",
  "data": {
    "key": { "remoteJid": "5511987654321@s.whatsapp.net",
             "fromMe": false, "id": "wamid.XXXX" },
    "pushName": "Fulano",
    "messageType": "conversation",
    "message": { "conversation": "Olá" }
  }
}
```

`data` também é aceito como lista — formato nativo do Baileys para
`messages.upsert`. Variações do nome do evento (`MESSAGES_UPSERT`,
`messages-upsert`) são normalizadas.

> **`remoteJidAlt` não existe nesta integração.** Verificado em 50 de 50
> mensagens reais: o campo é conceito do Baileys e a integração
> `WHATSAPP-BUSINESS` não o popula. A resolução de telefone usa apenas
> `remoteJid`.

### Gate de ingestão — a ordem importa

Roda na API, **antes** de enfileirar, e replica o SQL que o n8n usava:

1. **Resolver telefone** a partir de `remoteJid`; rejeita grupos (`@g.us`)
2. **Blocklist** — igualdade sobre o telefone canônico
3. **Variações do 9º dígito** — gera as formas com e sem o 9
4. **`fromMe = true`** → desliga agente e follow-up, descarta
5. **Ler o lead e checar `agent_active`** → se `false`, descarta **sem escrever**
6. **Upsert** — canoniza a chave, promove `formulario_preenchido → iniciou_conversa`
7. **Enfileira** com `channel='evolution'`

> O passo 5 vir **antes** do 6 não é detalhe de estilo. Se o upsert viesse
> primeiro, todo lead em handover teria `followup_count` zerado a cada mensagem
> recebida — e ao ser reativado receberia a escada de follow-up desde o início.

### Duas representações de telefone

- **Canônico** — só dígitos, brasileiro sem o 9: `551187654321`. Usado em
  `leads_crm`, `blocklist`, `legacy_chat_history`.
- **E.164 do harness** — canônico com `+`: `+551187654321`. Usado em
  `message_queue.phone_number` e no `thread_id`.

A conversão é sempre explícita, via `to_e164()` / `from_e164()`.

### Deduplicação

A Evolution reentrega o webhook em timeout ou resposta ≥400. Um índice único
parcial em `(channel, agent_id, message_id)` impede que a reentrega vire
segunda linha na fila; a rota responde 200 com motivo `duplicata`.

## Saída

`POST /message/sendText/{instance}`, header `apikey`, body
`{number, text, delay}`. O `number` vai **sem** o `+`. Mensagens acima de
**4096 caracteres** são quebradas em partes — limite da Cloud API oficial.

### Por que não existe "digitando…"

`send_typing` é no-op nesta integração, e a razão é que o indicador
**simplesmente não está disponível**: o endpoint de presença responderia HTTP
400 por mensagem, e o parâmetro `delay` do `sendText` não produz indicador
algum em `WHATSAPP-BUSINESS`. O `delay_ms` continua exposto no cliente porque
funciona em instâncias Baileys.

Isso não é regressão em relação ao n8n — ele envia pela mesma Evolution e tem a
mesma limitação.

## Mídia

A URL que chega em `message.audioMessage.url` aponta para **mídia criptografada
do Baileys**. Um `GET` nela devolve bytes inúteis. O conteúdo decifrado sai de:

```
POST /chat/getBase64FromMediaMessage/{instance}
```

Esse endpoint **exige a key completa** — testado contra a API real, uma key
contendo só o `id` devolve `400 TypeError`. Por isso a key inteira é gravada em
`message_queue.provider_message_key` (JSONB) no momento da ingestão, e o worker
a propaga até o download.

## Limitações conhecidas

Duas coisas não foram verificadas contra o servidor real e estão cobertas
apenas por mock. Ambas devem ser exercitadas antes do cutover:

1. **O formato do `POST /message/sendText`.** A autenticação foi validada
   contra o servidor, mas o envio nunca foi disparado — fazê-lo entregaria uma
   mensagem de WhatsApp a uma pessoa real. Teste manualmente contra o seu
   próprio número antes de considerar o canal pronto.
2. **A forma da key de mídia e o `MEDIA_TYPE_MAP`.** A instância não tem
   nenhuma mensagem de mídia armazenada (`findMessages` filtrando por
   `audioMessage` e `imageMessage` devolve 0), então ambos são **inferidos** da
   documentação do Baileys. O primeiro áudio real que chegar é o teste de
   verdade.

## Diagnóstico

| Sintoma | Causa provável |
|---|---|
| Mensagens na fila, todas falhando com "canal não habilitado" | as três variáveis vazias com webhook apontado |
| Webhook devolve 401 | `EVOLUTION_WEBHOOK_SECRET` preenchida e header ausente ou errado |
| Lead parou de receber respostas | `agent_active=false` — humano respondeu pelo aparelho, ou etiqueta de pausa |
| Resposta duplicada | reentrega fora da janela de dedupe; conferir `message_id` na fila |
| Áudio vira "mídia não suportada" | sem `provider_message_key` e sem URL utilizável |
