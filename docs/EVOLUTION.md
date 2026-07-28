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
| `EVOLUTION_WEBHOOK_SECRET` | **em produção, sim** | valor que a rota exige no header do webhook |

### O secret é obrigatório em produção

Com `ENVIRONMENT=production` e o canal Evolution configurado (qualquer uma das
três variáveis preenchida), `validate_runtime_settings()` **derruba o boot** se
`EVOLUTION_WEBHOOK_SECRET` estiver vazia ou tiver menos de 32 caracteres. Vale
inclusive com `OUTBOUND_MODE=mock` — quem abre a porta é a rota inbound, que não
olha o modo outbound.

O motivo é o custo de deixá-la aberta. A Evolution não assina o body: o header
estático é o único gate do inbound. Sem ele, um POST anônimo com texto qualquer
e `remoteJid` escolhido pelo atacante cria o lead, entra na fila, invoca o
agente e **faz sair mensagem de WhatsApp pelo número oficial Meta do cliente**
para o telefone que ele quiser — custo de LLM, `leads_crm` envenenado e risco de
ban do número. A URL é adivinhável: os ids de agente são públicos neste
repositório template.

Em dev (`ENVIRONMENT=development`) a variável continua opcional e a rota fica
aberta.

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

Reutilizar também não passa no fail-fast por acaso: o boot em produção só checa
tamanho e presença, então a única proteção contra reuso é a disciplina.

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

1. **Resolver telefone** a partir de `remoteJid`; rejeita grupos (`@g.us`) e
   LIDs (`@lid`)
2. **Blocklist** — igualdade sobre o telefone canônico
3. **Variações do 9º dígito** — gera as formas com e sem o 9
4. **`fromMe = true`** → desliga agente e follow-up, descarta
5. **Ler o lead e checar `agent_active`** → se `false`, descarta a mensagem, mas
   **grava `last_inbound_at`**: a janela de 24h da Cloud API é fato sobre a Meta,
   não sobre o nosso funil. Congelar esse relógio durante um handover humano
   deixaria o lead inalcançável pela régua na retomada, com a janela real aberta.
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

Nem todo `remoteJid` é telefone. O **`@lid`** (LinkedID) está em rollout no
WhatsApp e é identificador opaco: `551188654321@lid` tem o formato exato de um
brasileiro sem o 9 e viraria a chave primária de um lead real — duas pessoas na
mesma linha de `leads_crm`. É recusado na entrada, junto com `@g.us`.

Também é recusado o que **se declara brasileiro e não fecha com forma válida**:
DDI 55 sem correspondência, e 0 de tronco que sobra em número inexistente
(`011187654321` → 11 dígitos sem o 9 do celular). Vai para `leads_descartados`,
não vira identidade nova.

### O que fica em `leads_descartados`

Toda mensagem que o gate recusa por telefone é retida com o payload completo do
webhook — `agent` (query string), `instance` (topo do corpo) e `data` (a
mensagem). É o suficiente para reconstruir o POST original e reprocessar o
descarte depois de corrigir a causa.

### Deduplicação

A Evolution reentrega o webhook em timeout ou resposta ≥400. Um índice único
parcial em `(channel, agent_id, phone_number, message_id)` impede que a
reentrega vire segunda linha na fila; a rota responde 200 com motivo
`duplicata`. Rajada absorvida pelo debounce entra em
`message_queue.message_ids_absorvidos`, que o lookup também consulta.

> **Antes de deployar uma versão que mude essa chave**, leia
> [DATABASE.md → migração que troca índice de `ON CONFLICT`](DATABASE.md#migração-que-troca-índice-de-on-conflict-exige-parada-não-rolling-deploy).
> A migração dropa o índice que o código antigo usa no `ON CONFLICT`, e API
> velha servindo depois das migrações = parada total de ingestão nos quatro
> canais.

O lookup de duplicata roda **antes do rate limit e antes do gate**. Reentrega é
o mesmo evento: contá-la como mensagem nova consumia cota (com
`RATE_LIMIT_PER_HOUR=30`, 15 mensagens do lead mais uma reentrega de cada já
estouram a janela — e a partir daí a mensagem seguinte, legítima, sumiria com
200 e sem reentrega) e ainda renovava `last_interaction_at`/`followup_count` no
gate.

### Nada nesta rota responde 4xx por erro de configuração

Resposta ≥400 faz a Evolution reentregar, e reentrega não conserta configuração.
Por isso respondem **200 + log de erro**, não 4xx:

| Situação | Motivo na resposta |
|---|---|
| `?agent=` com typo (agente fora do catálogo) | `agente_desconhecido` |
| Body que não é JSON válido | `json_invalido` |
| JSON válido fora do formato (lista, string) | `payload_invalido` |
| Rate limit estourado | `rate_limit` |

O 401 do secret é a exceção deliberada: responder 200 a quem não sabe o segredo
engoliria em silêncio tanto o ataque quanto o secret trocado por engano.

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

**A mídia desta integração não é criptografada.** A `url` do nó de mídia aponta
para `lookaside.fbsbx.com` (CDN da Cloud API) e responde a um `GET` com a apikey
da instância em `Authorization: Bearer`. É assim que `worker/media.py` baixa —
praticamente o mesmo caminho do Twilio, trocando BasicAuth por Bearer.

A Fase 1 assumiu o formato Baileys e construiu o download errado. O payload real
de um áudio e de uma imagem enviados do celular para a instância está em
`docs/evidencias/payload-midia-cloud-api.json` (redigido) e desmente a premissa:

| Assumido (Baileys) | Real (`WHATSAPP-BUSINESS`) |
|---|---|
| campo `mimetype` | **`mime_type`**, com underscore |
| `mediaKey`, `directPath`, `fileEncSha256` presentes | **ausentes** |
| url em `mmg.whatsapp.net`, cifrada | `lookaside.fbsbx.com`, aberta |
| download por `POST /chat/getBase64FromMediaMessage` | **`GET` na url com `Authorization: Bearer <apikey>`** |

O `GET` foi exercitado contra a instância real:

| Nó | Resultado |
|---|---|
| `audioMessage` | HTTP 200, 2264 bytes, `content-type: audio/ogg`, magic bytes `OggS` |
| `imageMessage` | HTTP 200, 9776 bytes, `content-type: image/jpeg`, magic bytes `JFIF` |

O download **não depende de `OUTBOUND_MODE`** — é um `GET` numa CDN, leitura
pura, igual ao que os demais canais já fazem em `mock`. Nada é enviado ao lead
por causa dele.

`EvolutionClient.baixar_midia` (o `getBase64FromMediaMessage`) continua no
cliente **sem chamador**, documentado como caminho de instância **Baileys** —
lá a URL é cifrada e esse endpoint é a única via. Este repositório é um
template: para um cliente que rode Baileys, o conserto é trocar o ramo da
Evolution em `download_media` por uma chamada ao método, usando a
`provider_message_key` que a fila já persiste.

`message_queue.provider_message_key` (JSONB, migração `008`) continua sendo
gravada, mas **não é mais via de download**. Ela existia só para carregar a key
até o `getBase64FromMediaMessage`. Ficou por dois motivos: correlacionar a linha
da fila com a mensagem do lado da Evolution/Meta em investigação, e servir o
caminho Baileys acima. A migração não é revertida — migração aplicada é
imutável e o ganho seria nulo.

### O `media_type` vem do payload, não de inferência

O MIME é lido de **`mime_type` ou `mimetype`**, nessa ordem: a Cloud API manda o
primeiro (`"audio/ogg; codecs=opus"`), o Baileys manda o segundo, e o template
atende as duas. É esse valor que vira `message_queue.media_type`.

O mapa por campo (`imageMessage` → `image/jpeg`, `audioMessage` → `audio/ogg`,
etc.) é o **último recurso**, só quando nenhuma das duas formas vem. Ler apenas
`mimetype` fazia todo payload da Cloud API cair nesse default — funcionava por
acidente no caso comum e erraria em qualquer outro: um `audio/mp4` virava
`audio/ogg`, e a transcrição saía com o formato errado.

Inferir pelo `messageType` foi removido: em mensagem embrulhada o tipo declarado
é o do **envelope**, então um `documentMessage` com `imageMessage` dentro virava
`application/octet-stream` — que o preprocessor classifica como "mídia não
suportada" — para uma foto que o lead mandou com legenda.

Os consumidores aguentam o MIME com parâmetro: `_media_kind` testa
`startswith("audio/")` e `_audio_format_from_media_type` acha `"ogg"` dentro de
`"audio/ogg; codecs=opus"`.

### Figurinha não é mídia

`stickerMessage` vira o texto `[figurinha]`, sem `media_url` nem `media_type`.
Tratá-la como imagem custava um download mais uma chamada multimodal ao LLM por
figurinha — para descrever uma figurinha — e webp animado provavelmente nem
seria aceito pelo modelo. Como marcador de texto, o agente sabe o que chegou,
responde no fluxo, e o gate roda normalmente (diferente de uma reação, a
figurinha é uma mensagem no chat e merece resposta).

Áudio de voz (PTT) não tem campo próprio: é `audioMessage` com `ptt: true` (e
`voice: true` na Cloud API). Não existe `pttMessage`.

## Verificado contra a instância real

O envio foi exercitado em 26/07/2026 contra a instância `instancia-apioficial`
(número `+55 11 91998-0518`, nome verificado *Comercial - Eleve-C*, qualidade
GREEN), com janela de 24h aberta por uma mensagem de entrada:

| O que | Resultado |
|---|---|
| Resposta do `sendText` | `{"key":{"id":"wamid…","remoteJid":"…","fromMe":true}, "status":"PENDING"}` |
| Telefone **canônico de 12 dígitos**, sem o 9 | **entrega** |
| `number` sem o `+` | aceito |
| `remoteJidAlt` no payload de entrada | ausente, como esperado |

**A Evolution normaliza a resposta da Cloud API para o formato Baileys** antes
de devolver — o `{"messages":[{"id":…}]}` nativo da Meta não chega ao cliente.

Distribuição real de formas de telefone em 50 mensagens recentes: **35 com 12
dígitos (sem o 9) e 15 com 13 (com o 9)**. A base mista é tráfego corrente, não
resíduo legado — é o que justifica a canonicalização.

> **A janela de 24h da Cloud API oficial vale para tudo.** Sem uma mensagem de
> entrada recente, texto livre é rejeitado pela Meta: só template aprovado
> alcança quem não escreveu primeiro. Isso condiciona o follow-up (o degrau de
> 23 horas encosta no limite) e a primeira abordagem a leads de formulário, que
> nunca abriram janela nenhuma.

### Rede de segurança no envio

Mesmo com o shape da resposta agora verificado, o cliente extrai o id de
`{"key":{"id":…}}`, de `{"messages":[{"id":…}]}` e de qualquer um dos dois
aninhado em `data`. E **2xx sem id reconhecível não é erro** — a mensagem já
saiu; levantar ali fazia o processor marcar `failed`, tentar de novo e entregar
a mesma mensagem três vezes.

Sai um `warning` (`evolution_send_sem_id`) com o corpo recebido: se ele aparecer
no log depois do cutover, é sinal de que o parser precisa de mais um shape —
não de que a entrega falhou.

## Diagnóstico

| Sintoma | Causa provável |
|---|---|
| Mensagens na fila, todas falhando com "canal não habilitado" | as três variáveis vazias com webhook apontado |
| Webhook devolve 401 | `EVOLUTION_WEBHOOK_SECRET` preenchida e header ausente ou errado |
| Lead parou de receber respostas | `agent_active=false` — humano respondeu pelo aparelho, ou etiqueta de pausa |
| Resposta duplicada | reentrega fora da janela de dedupe; conferir `message_id` na fila |
| Áudio vira "mídia não suportada" | payload sem `url` no nó de mídia — sem ela não há download |
| Boot em produção falha citando `EVOLUTION_WEBHOOK_SECRET` | canal configurado com secret vazia ou com menos de 32 caracteres |
| Webhook devolve 200 com `motivo: agente_desconhecido` | typo no `?agent=` da URL configurada na instância |
| `evolution_send_sem_id` no log | envio funcionou; o corpo da resposta traz um shape que o parser não conhece |
| Download de mídia devolve 401 | `EVOLUTION_API_KEY` errada — é ela que vai no `Authorization: Bearer` do GET |
