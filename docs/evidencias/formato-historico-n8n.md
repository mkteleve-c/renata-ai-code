# Formato real de `n8n_chat_histories` (Supabase legado)

Medido em 27/07/2026 contra a base de produção. **8.920 linhas, 736 sessões.**
`session_id` é o telefone; `message` é JSONB no formato serializado do LangChain.

## Distribuição por tipo

| `message->>'type'` | linhas | `content` começa com `{` |
|---|---|---|
| `ai` | 4.460 | 3.316 |
| `human` | 3.316 | 0 |
| `tool` | 1.144 | 0 |

`4460 − 3316 = 1144`, igual à contagem de `tool`.

## As duas formas de `ai`

**Resposta final — 3.316 linhas.** O envelope está **aninhado sob `output`**:

```json
{"output":{"messages":["Oi, Diego!","Recebi sua mensagem e vou te ajudar"]}}
```

**Pedido de ferramenta — 1.144 linhas.** É **texto puro**, não `tool_calls` em
`additional_kwargs` — nenhuma linha da base tem essa chave:

```
Calling update_crm1 with input: {"phone":"5511986335551","pha...
```

Logo, "o `content` faz `json.loads`" é o discriminador correto entre os dois.

## Consequência para a importação

`extrair_baloes` (`agents/catalog/elevec_sdr/saida.py`) procura `messages` no
**topo** do objeto. Contra a forma real ele não encontra, emite
`extrair_baloes_sem_lista_messages` e cai no fallback de balão único —
devolvendo a string JSON inteira:

```
forma REAL (aninhada em output):
   -> {"output":{"messages":["Oi, Diego!","Recebi sua mensagem e vou te ajud

forma do plano (messages no topo):
   -> Oi, Diego!
   -> Recebi sua mensagem e vou te ajudar
```

Sem desaninhar `output` antes, as 3.316 respostas da Renata entram no histórico
como blobs de JSON — e o modelo passa a ver as próprias falas passadas nesse
formato.
