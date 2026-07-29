# Comando `/r` — reiniciar a conversa de um contato

**Data:** 2026-07-29
**Estado:** aprovado, aguardando plano de implementação

## O problema

Durante a janela de teste do cutover, quem testa a Renata precisa repetir o
roteiro desde o "Oi" — várias vezes, com o mesmo telefone. Hoje não existe
como fazer isso: a conversa fica no checkpointer do LangGraph e o funil fica
em `leads_crm`, e nenhum dos dois se apaga sozinho. Na prática, testar o
primeiro contato exige um telefone novo a cada rodada.

Vale para os números da allowlist agora, e para todos quando `ALLOWLIST_PHONES`
for esvaziada.

## O que "começar do zero" significa aqui

O estado de um contato vive em quatro lugares. A decisão do que apagar não é
óbvia, então está fixada explicitamente:

| Onde | O que guarda | `/r` faz |
|---|---|---|
| `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` | a conversa (`thread_id`) | **apaga** |
| `leads_crm` | fase do funil, e-mail, faturamento, `google_event_id`, follow-up, `agent_active` | **zera os campos, mantém a linha** |
| `legacy_chat_history` | até 12 turnos importados do Supabase | **não toca** |
| `message_queue` | trilha de auditoria | **não toca** — o próprio `/r` fica registrado |

### Por que a linha do lead sobrevive

Apagar a linha inteira deixaria o contato literalmente desconhecido, o que é
mais limpo para teste. Foi descartado porque o mesmo comando vai valer em
produção: um lead real digitando `/r` perderia o registro de que existe, e o
`created_at` — que é a única âncora de "desde quando essa pessoa fala com a
gente" — sumiria junto. Zerar os campos entrega o mesmo comportamento
observável (a Renata age como no primeiro contato) sem destruir a identidade.

### Por que o histórico legado NÃO é reinjetado

O middleware `historico_legado` decide reinjetar olhando **duas** condições:
não haver checkpoint para a thread **e** `leads_crm.metadata.historico_injetado`
não ser `true` (ver `agents/middleware/historico_legado.py::_ja_injetado`).

Apagar o checkpoint satisfaz a primeira. Se o flag também fosse limpo, o
próximo "oi" recarregaria os turnos do Supabase e a conversa **não** começaria
do zero — começaria de onde o n8n parou. Como o objetivo é testar o roteiro
desde o início, o flag permanece `true`.

Consequência aceita: não há como, por `/r`, voltar um lead ao estado
"migrado, com histórico". Se isso for preciso um dia, é um comando diferente.

### O que explicitamente NÃO acontece

**Um evento já criado no Google Calendar não é cancelado.** O `/r` limpa
`google_event_id` do lead, mas a reunião continua na agenda do Silvio, agora
órfã — nada no sistema aponta mais para ela. É a consequência menos óbvia
desta escolha e está registrada aqui de propósito: quem resetar um lead que
já agendou precisa cancelar na agenda à mão.

## Onde interceptar

**No worker, em `processor.py`, antes de carregar o agente.**

Alternativas descartadas:

- **Na API, no `webhook_evolution.py`, antes de enfileirar.** Pegaria mais
  cedo e nem ocuparia a fila, mas a API não tem cliente outbound — quem fala
  com a Evolution é o worker. Mandar a confirmação exigiria dar um cliente de
  envio à API, furando a separação que o projeto mantém (credenciais outbound
  só no worker, ver `docs/RAILWAY.md`).
- **Como tool do agente (`reset_conversa`).** O modelo decidiria quando
  resetar: não-determinístico para um comando que precisa ser exato, gasta
  token, e o grafo apagando o próprio checkpoint no meio da execução é uma
  corrida sem motivo.

No worker o custo é zero: a mensagem entra na fila normalmente, e o guard
roda antes de qualquer instanciação do grafo. Nenhuma chamada de LLM.

## Componentes

### `shared/reset.py` — novo

```
async def resetar_contato(pool, thread_id: str, phone_e164: str) -> bool
```

Recebe o `thread_id` pronto em vez de remontá-lo a partir de telefone +
agente: `MessageQueue` já o carrega (`shared/models.py:56`), gravado quando a
mensagem foi enfileirada. Remontar aqui criaria uma segunda fonte de verdade
para a mesma chave, e as duas divergiriam no dia em que o formato mudasse.

Uma transação, dois passos:

```sql
DELETE FROM checkpoint_writes WHERE thread_id = %(thread)s;
DELETE FROM checkpoint_blobs  WHERE thread_id = %(thread)s;
DELETE FROM checkpoints       WHERE thread_id = %(thread)s;

UPDATE leads_crm SET
  phase               = 'iniciou_conversa',
  email               = NULL,
  faturamento_mensal  = NULL,
  qualificacao_notas  = NULL,
  google_event_id     = NULL,
  followup_count      = 0,
  followup_active     = true,
  agent_active        = true,
  agent_reactivate_at = NULL
WHERE phone = %(canonico)s;
```

**Duas chaves em formatos diferentes, e confundi-las é o erro mais provável
desta implementação:**

- `thread_id` — E.164 **com** `+` mais o agente
  (`+558191013614:elevec_sdr`). Vem pronto na mensagem.
- `leads_crm.phone` — forma **canônica**, sem `+` e sem o 9º dígito
  (`558191013614`), derivada de `phone_e164` por `phone.canonico_do_lead`.

Os dois formatos foram verificados contra a produção em 2026-07-29: o
`/api/chats` devolve `thread_id="+558191013614:elevec_sdr"` para o lead cujo
`leads_crm.phone` é `558191013614`.

Devolve `True` se o lead foi encontrado, `False` se não. `name`, `username`,
`source`, `pipedriveid`, `created_at` e `metadata` **não** entram no `UPDATE` —
são identidade, não estado de conversa.

### `worker/processor.py` — guard

Antes do bloco que carrega o agente:

```python
if (message.normalized_input or "").strip() == COMANDO_RESET:
    await resetar_contato(pool, message.thread_id, message.phone_number)
    await enviar(RESPOSTA_RESET)          # cliente do canal de origem
    marcar done com response=RESPOSTA_RESET
    return
```

Usa `normalized_input`, não `incoming_message`: é o campo que já passou pelo
pré-processamento de mídia, então um áudio transcrito como "/r" e uma imagem
descrita como "/r" seguem o mesmo caminho que o texto — sem regra separada.

### Constantes

- `COMANDO_RESET = "/r"` — comparação **exata** após `.strip()`.
  `/R`, `/reset`, `/rr` e `"manda /r pra ela"` **não** disparam. A superfície
  mínima é deliberada: o comando vai valer em produção para leads reais, e
  cada forma extra aceita é uma forma extra de perder um funil por acidente.
- `RESPOSTA_RESET = "Pronto, conversa reiniciada."` — texto fixo, um balão,
  sem LLM.

## Erros

| Situação | Comportamento |
|---|---|
| Lead não existe em `leads_crm` | Loga `reset_lead_ausente` e **segue** apagando o checkpoint. O reset da conversa é o que importa e não depende do CRM. A mensagem ainda é marcada `done`. |
| Falha ao apagar o checkpoint | A transação inteira reverte; a mensagem segue o retry normal da fila (`MAX_ATTEMPTS`). Não pode ficar meio-resetado. |
| Falha ao enviar a confirmação | O reset **já aconteceu** e não é desfeito. Loga o erro de envio. Reverter um `DELETE` de checkpoint por causa de um envio falho seria pior: o retry reprocessaria `/r` e resetaria de novo, agora sobre um estado já limpo. |

## Testes

**Unit — casamento do gatilho.** `"/r"` e `" /r "` disparam. `"/R"`,
`"/reset"`, `"/rr"`, `"r"`, `"manda /r pra ela"` e `""` não. É o teste que
protege contra o gatilho acidental, que é o risco real em produção.

**Integração — o efeito.** Depois de `/r` num contato com conversa e funil
preenchidos:

- `select 1 from checkpoints where thread_id = ...` não devolve nada, idem
  `checkpoint_blobs` e `checkpoint_writes`;
- a linha em `leads_crm` **continua existindo**, com `phase='iniciou_conversa'`
  e `email`/`faturamento_mensal`/`google_event_id` nulos;
- `metadata->>'historico_injetado'` continua `'true'`;
- `created_at` e `name` inalterados.

**Integração — o LLM não é chamado.** Um duble do modelo que falha o teste se
for invocado. É o que prova que o guard roda antes do grafo, e não depois.

**Integração — o histórico legado não volta.** Contato com linhas em
`legacy_chat_history` e `historico_injetado=true`: depois do `/r` e de um novo
turno, as mensagens do state não contêm o conteúdo legado.

## Fora de escopo

- **Confirmação em dois passos** ("tem certeza?"). O risco de um lead real
  digitar `/r` por acidente é conhecido e aceito por ora; a mitigação foi
  adiada deliberadamente, não esquecida.
- **Restringir `/r` a uma lista de administradores.** Decisão explícita: vale
  para quem a allowlist já deixa falar, e para todos quando ela for esvaziada.
- **Cancelar o evento no Google Calendar.** Ver acima.
- **Resetar todos os agentes de um telefone de uma vez.** O `thread_id` é por
  agente; este deploy roda um só.
