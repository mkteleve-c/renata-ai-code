# Agendar antes de perguntar faturamento, com as faixas do YAY FORMS

**Data:** 2026-07-30
**Estado:** aguardando aprovação

## O que muda, em uma frase

O faturamento deixa de ser **portão que bloqueia o agendamento** e passa a ser
**qualificação que acontece depois dele**, usando as mesmas faixas do
workflow `nXuIqeQ0tBialBsR` (YAY FORMS).

## Por que

O portão atual (Fase 7, marcada OBRIGATÓRIO) é a maior fricção do funil:
pergunta quanto a pessoa fatura logo depois de ela dar o e-mail, e recusa
agendar até ela responder. Um lead que trava ali é perdido inteiro — sem
reunião e sem dado.

Agendando primeiro, o compromisso já está firmado quando a pergunta chega, e
uma recusa custa a qualificação, não a reunião.

## As faixas, extraídas do n8n

Do nó `4. Qualifica por faturamento`:

| Resposta no formulário | Destino no n8n |
|---|---|
| Menos de R$ 3 mil/mês | `DESQUALIFICADO (<5k)` |
| R$ 3 mil a R$ 5 mil/mês | `DESQUALIFICADO (<5k)` |
| R$ 5 mil a R$ 8 mil/mês | `5-8k \| Rodizio` |
| R$ 8 mil a R$ 15 mil/mês | `8-25k \| Rodizio` |
| R$ 15 mil a R$ 25 mil/mês | `8-25k \| Rodizio` |
| Acima de R$ 25 mil/mês | `>25k \| Silvio` |

**O corte é R$ 5 mil/mês.**

> Nota para quem for auditar pelo n8n: o rótulo da primeira saída diz
> `"Menos de R$ 2 mil/mês"` mas a condição casa `"Menos de R$ 3 mil/mês"`.
> Bug cosmético do workflow, não afeta o roteamento.

## O escopo da Renata era mais estreito do que parecia

Rastreando as conexões do YAY FORMS, **um único nó manda mensagem para o
lead pela Renata**: `WA Template | 5-8k s/agenda`, template
`boas_vindas_renata_respondiapp_03`. Ele só é alcançado por
`5-8k` **que não agendou no Calendly**.

| Faixa | Agendou? | O que acontecia |
|---|---|---|
| < 5k | — | desqualificado, Renata não entra |
| **5–8k** | **não** | **template da Renata** |
| 5–8k | sim | closer assume |
| 8–25k | não | só e-mail para `ana.castro@`/`ivana.castro@` |
| 8–25k | sim | closer assume |
| > 25k | não | WhatsApp interno para o Silvio + e-mail |
| > 25k | sim | Silvio assume |

Confirmando: `followup_active = TRUE` aparece em **um único** `PG Lead` — o
`5-8k s/agenda`. Todos os outros gravam `FALSE`. A régua de follow-up nunca
foi para a base inteira.

**Isso não sobrevive ao cutover.** Com o webhook da Evolution repontado, toda
mensagem chega ao harness e a Renata responde qualquer um. O recorte por
faixa vinha do formulário, e o caminho `whatsapp_direct` nunca passou por
ele.

## Decisões

### Ela atende todos, e roteia por faixa depois de descobrir

- **< R$ 5 mil** → mantém a reunião, `update_crm(desqualificado)` e
  `human_handover`. Não cancela.
- **R$ 5–8 mil** → é a faixa dela. Segue normal, `update_crm(agendou_sessao)`.
- **≥ R$ 8 mil** → mantém a reunião e `human_handover`: lead grande merece
  closer, não robô. Preserva a intenção do funil sem exigir saber a faixa
  antes da conversa.

Por que nunca cancelar: marcar e desmarcar em dois turnos é constrangedor, e
o custo de uma reunião fora do perfil (o Silvio decide se vai) é menor que o
de uma pessoa qualificada perdendo o horário por um erro de classificação.
`human_handover` avisa o time por WhatsApp (`HANDOVER_NOTIFY_PHONE`) e
desliga o agente para aquele lead — uma pessoa decide o que fazer.

### Quando já sabe, confirma em vez de perguntar

Lead de formulário já declarou a faixa. Perguntar de novo soa a
desorganização. A Renata confirma:

> *"Vi aqui que você preencheu que fatura na faixa de R$ 5 a 8 mil — segue
> assim?"*

Para quem não veio de formulário (`whatsapp_direct`), pergunta do zero.

### Só Silvio

O rodízio Silvio/Ivana do n8n **não é regra de negócio** — o nó
`Define closer` lê `event_memberships[0].user_name` do **Calendly**, ou seja,
quem já está no invite. O rodízio acontece dentro do Calendly.

O harness agenda direto no Google Calendar do Silvio
(`GOOGLE_CALENDAR_ID=silvio.hirata@eleve-c.co`) e não tem Calendly no
caminho. Rodízio fica **fora de escopo** — exigiria a agenda da Ivana
configurada e uma regra de alternância nova.

## Mudanças

### 1. `leads_crm.faturamento_mensal` precisa ser preenchido na origem

A coluna existe e a tool `update_crm` já a escreve. O que falta é o dado do
formulário chegar: o `PG Lead | 5-8k s/agenda` do n8n insere `phone`, `name`,
`source`, `pipedriveID`, `created_at`, `last_interaction_at`,
`followup_count`, `followup_active`, `phase` — **faturamento não está lá**.
Ele vai só para o campo customizado do Pipedrive
(`77899370154d31dc7a7dbf4805c0285362ddb9e6`).

**Ação no n8n (fora deste repositório):** acrescentar `faturamento_mensal` ao
INSERT e apontar a credencial Postgres para o banco do Railway.

Sem isso, a Renata simplesmente não confirma — cai no caminho de perguntar
do zero, que é o comportamento correto quando não se sabe.

### 2. `contexto.py` — novo placeholder `{faturamento}`

`carregar_contexto` passa a ler `faturamento_mensal` junto com `name` e
`source`, e `contexto_vazio` ganha a chave. Vazio quando não há dado — o
prompt trata os dois casos.

### 3. `prompts.py` — Fases 7 e 8 trocam de lugar

- **Fase 7 (era 8): Agendamento.** Depois do e-mail, consulta a
  disponibilidade e agenda. Sem exigir faturamento.
- **Fase 8 (era 7): Qualificação por faturamento.** Depois de confirmado o
  agendamento, confirma ou pergunta a faixa, e roteia conforme a tabela de
  decisões acima.

O bloco `### Sequência de Agendamento (INVIOLÁVEL)` em RULES precisa ser
revisto junto — ele hoje amarra a ordem antiga.

### 4. Golden test — mudança documentada nº 6

`test_elevec_prompt.py` trava o prompt contra a evidência do n8n. A inversão
entra como a sexta transformação registrada, com o motivo. Sem isso o teste
quebra, e é assim que ele deve se comportar.

## Testes

**Unit — o prompt.** Golden atualizado; asserções de que a Fase 7 é
agendamento e a Fase 8 é faturamento, e de que sumiu o "ANTES de qualquer
agendamento".

**Unit — contexto.** `{faturamento}` presente quando a coluna tem valor,
vazio quando não tem, e o placeholder nunca vaza cru para o prompt.

**Integração — `make test-roteiro` com LLM real**, três roteiros novos:

| Roteiro | Faturamento | Esperado |
|---|---|---|
| D | "uns 6 mil" | agenda, `agendou_sessao`, **sem** handover |
| E | "uns 3 mil" | **agenda**, `desqualificado`, **com** handover, evento **não** cancelado |
| F | "uns 40 mil" | agenda, **com** handover |

O roteiro E é o que prova a decisão central: o evento continua no Calendar
depois da desqualificação.

**Integração — confirmação.** Lead com `faturamento_mensal` preenchido: a
resposta da Renata cita a faixa. Lead sem: ela pergunta aberto.

## Fora de escopo

- **Rodízio Silvio/Ivana** — ver acima.
- **Mover card no Pipedrive para o stage 65 (desqualificado).** O n8n move; o
  harness deliberadamente não (`config.py`: "desqualificado não move card —
  só atualiza o banco"). Manter a decisão atual; mudá-la é outra conversa.
- **Escrever o campo customizado de faturamento no Pipedrive.** O harness
  grava em `leads_crm`; o campo do Pipedrive continua sendo escrito pelo
  formulário.
- **Reativar o YAY FORMS e os workflows de LinkedIn no n8n.** Necessário para
  a captação por formulário voltar a existir, mas é ação no n8n, não aqui.
