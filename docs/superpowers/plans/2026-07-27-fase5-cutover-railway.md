# Fase 5 — Cutover e deploy no Railway

> **Para agentes:** SUB-SKILL OBRIGATÓRIA: use `superpowers:subagent-driven-development`.

**Goal:** Levar a Renata do n8n para o harness em produção, no Railway, sem
perder lead, sem mandar mensagem indevida, e com caminho de volta.

---

## A divisão de responsabilidade desta fase

Esta fase é diferente das quatro anteriores. Ela **toca produção**: desliga um
sistema que está atendendo gente agora, migra dados reais uma única vez, e
reaponta webhooks de um número de WhatsApp comercial.

**Os agentes constroem e testam as ferramentas. Uma pessoa executa o cutover.**

Nenhuma task deste plano autoriza rodar migração contra a base real, desligar
workflow do n8n, ou reapontar webhook. As tasks entregam ferramenta, verificação
e roteiro; a execução é decisão humana, com o relatório na mão.

---

## Global Constraints

- Python 3.11+, `uv`. Português brasileiro. Conventional Commits.
- Banco de dev na porta **5440**. Não rodar `make up`.
- **Não usar `make check`.** `ruff check`, `ruff format --check`, `pyright` só
  nos arquivos tocados. **Não fazer `git push`.** Nunca `git add -A`.
- **Credenciais são segredo**: Supabase, Evolution, Google, Pipedrive, OpenRouter.
  Entram por variável de ambiente, nunca versionadas, **nunca impressas** — nem
  em log, nem em relatório, nem em mensagem de erro.
- Camada de banco testada contra o Postgres real, nunca monkeypatchada.
- Nos testes, **nunca timestamps idênticos** entre linhas do mesmo grupo.
- Suíte de partida: **883 passed, 13 skipped, 11 deselected** com
  `uv run pytest -m "not docker_demo"` (sem paths).

---

## O que já existe, e o que falta

`scripts/migrar_supabase.py` tem **1.617 linhas** de lógica testada — normalização,
fusão, importação, validações, histórico. E **não roda**: não tem `main()`, não lê
do Supabase, não escreve o relatório em disco. É biblioteca.

`Dockerfile.{api,db,worker,frontend}` existem. `docs/RAILWAY.md` tem 405 linhas.
Não há `railway.json`.

---

## Task 1: tornar o script executável

**Files:** `scripts/migrar_supabase.py`, `tests/integration/test_migrar_cli.py`

### A decisão que governa esta task

**`--dry-run` é o padrão.** Rodar o script sem argumento **não escreve nada** —
lê, funde, valida, gera o relatório e para. Escrever exige `--executar` explícito.

É migração de mão única contra dados de gente real, num momento em que o sistema
antigo já estará desligado. A direção segura de errar é não escrever.

### O que acrescentar

- Leitura REST do Supabase, **paginada** — são 3.373 leads e 8.920 mensagens, e
  o PostgREST corta em 1.000 por padrão. Uma leitura que silenciosamente traz
  1.000 linhas e diz que terminou é o pior desfecho possível. Valide a contagem
  contra `Content-Range` e **aborte** se não bater.
- `relatorio_migracao.md` escrito em disco (já está no `.gitignore` — ele carrega
  telefone e nome de gente real).
- Resumo no stdout: totais, descartes por motivo, grupos fundidos, e as
  pendências de decisão humana.
- `--dry-run` e `--executar` mutuamente exclusivos; sem nenhum, vale `--dry-run`.

- [ ] **Step 1: Testes** — `--dry-run` não escreve nada no banco (verifique
  contando linhas antes e depois); paginação traz as 3.373 e não 1.000; contagem
  divergente aborta; credencial ausente dá erro legível e **não** vaza valor;
  `--executar` escreve.

Use um servidor HTTP falso para a paginação, não a rede.

- [ ] **Step 2: Implementar. Step 3: mutação (mínimo 6), commit**

Mutações: `--dry-run` deixando de ser o padrão; paginação parando na primeira
página; a validação de `Content-Range` virando aviso; a credencial aparecendo em
log de erro.

---

## Task 2: pré-voo

**Files:** `scripts/preflight_cutover.py`, `tests/integration/test_preflight.py`

Um comando que responde **"dá para virar a chave agora?"** com sim ou não, e
listando o que falta. Roda contra o ambiente de produção **sem alterá-lo**.

Checagens, todas bloqueantes:

| o quê | por quê |
|---|---|
| Migrações 001–015 aplicadas | a 014 instala o `CHECK` que sustenta a chave canônica |
| `CHECK` presente com as três cláusulas | lido de `pg_get_constraintdef`, não do arquivo |
| `leads_crm` vazia | o cutover é a primeira carga; linha preexistente muda o resultado da fusão |
| `OUTBOUND_MODE=real` e canal Evolution completo | |
| `EVOLUTION_WEBHOOK_SECRET` com 32+ caracteres | |
| Evolution alcançável e a instância conectada | |
| Google Calendar: refresh token válido | um `GET` que não cria evento |
| Pipedrive alcançável, estágios 12 e 13 existem | |
| `INTERNAL_SERVICE_TOKEN` forte | |
| **`FOLLOWUP_ENABLED=false`** | a régua só liga depois, deliberadamente |
| `HANDOVER_NOTIFY_PHONE` preenchido e canonicalizável | senão o handover é silencioso |
| `OPENROUTER_API_KEY` responde | não pode ser placeholder |

**Nenhuma checagem escreve.** A do Google lê a agenda; a do Pipedrive lê os
estágios; a do OpenRouter faz uma chamada mínima. Se alguma precisar escrever
para valer, ela não entra.

Saída: tabela do que passou e do que falhou, e código de saída diferente de zero
se qualquer uma falhar.

- [ ] **Steps: testes com dublês para cada falha possível, implementar, mutação
  (mínimo 5, incluindo "checagem que falha vira aviso"), commit**

---

## Task 3: Railway

**Files:** `railway.json`, `docs/RAILWAY.md`, `deploy/README.md`

Quatro serviços: `db` (privado, volume persistente), `api` (público), `worker`
(privado), `frontend`.

### Duas coisas que o `docs/RAILWAY.md` atual erra ou não cobre

**1. Uma réplica na API, não duas.** O documento sugere duas. O rate limit do
harness é **em memória**: com duas réplicas o limite por telefone dobra em
silêncio (30/h vira até 60/h). Escalar exige antes mover o rate limit para o
Postgres, que está fora desta entrega.

**2. Uma réplica no worker.** Duas réplicas processam a fila em paralelo — o que
o `FOR UPDATE SKIP LOCKED` suporta —, mas a régua de follow-up subiria nas duas,
e `iniciar_followup` não coordena entre processos. Duas rodadas simultâneas são
seguras para o claim, mas dobram a carga de escrita sem ganho.

### O que documentar sem ambiguidade

- As variáveis novas das Fases 1–4: `EVOLUTION_*`, `GOOGLE_*`, `PIPEDRIVE_*`,
  `HANDOVER_NOTIFY_PHONE`, `FOLLOWUP_*`, `CHATWOOT_*`.
- **`FOLLOWUP_ENABLED=false` no primeiro deploy**, com a explicação.
- Que o Postgres do Railway é o banco de verdade, e o Supabase é **origem de
  leitura única** que deixa de existir depois do cutover.
- Como fazer backup do volume antes do cutover.

- [ ] **Steps: escrever, validar o `railway.json` contra o schema, commit**

---

## Task 4: o roteiro do cutover

**Files:** `docs/CUTOVER.md`

Este é o entregável que uma **pessoa** executa. Ele precisa ser seguível por
alguém que não acompanhou esta migração.

### A ordem, com portão de decisão em cada passo

1. **Backup** do volume do Postgres do Railway e conferência de que o Supabase
   está intacto (é a origem; se algo der errado, é para onde se volta).
2. **`preflight_cutover.py`** — se falhar, para aqui.
3. **Desligar os 6 workflows do n8n.** A partir daqui o sistema antigo não
   escreve mais, e a janela começa.
4. **`migrar_supabase.py`** (sem argumento: dry-run). Ler o relatório inteiro.
5. **Portão humano** — as pendências de decisão precisam de resposta antes de
   escrever. As conhecidas hoje: o lead de **Moçambique** (`258864038352`) em
   `qualificado` e ativo; os números de **EUA** e **Portugal**; e qualquer grupo
   cuja fusão mudou `phase` ou `agent_active`.
6. **`migrar_supabase.py --executar`.** As validações abortam se algo não fechar.
7. **Conferir `max(last_interaction_at)`** antes e depois — tem que bater com o
   que o relatório disse.
8. **Reapontar o webhook da Evolution** (`instancia-apioficial`) para
   `https://<api>/webhook/evolution?agent=elevec_sdr`.
9. **Reapontar o webhook do ChatWoot.** *Sem este passo a etiqueta `pausar_agente`
   para de funcionar em silêncio, e o único sinal seria o agente respondendo por
   cima de um humano.* (Depende da Task 5 da Fase 3, hoje bloqueada.)
10. **Teste de fumaça**: mandar uma mensagem de um número próprio e ver o ciclo
    inteiro — fila, agente, resposta em balões.
11. **Monitorar a primeira hora** com a fila à vista (Task 5).
12. **Só então**, e como decisão separada, considerar ligar `FOLLOWUP_ENABLED`.

### O plano de volta

O Supabase **não é alterado** pela migração — é leitura. Então voltar é: religar
os workflows do n8n e reapontar os webhooks. Documente isso explicitamente, com
os passos, porque quem estiver executando às 22h de uma sexta precisa saber que
existe caminho de volta antes de começar.

### O que NÃO ligar no dia

`FOLLOWUP_ENABLED` fica `false`. A régua tem duas dívidas registradas na Fase 3:
o wiring de `main()` que a liga **tem cobertura zero** (trocar a chamada por
`None` passa na suíte), e trocar `except Exception` por `BaseException` no laço
faz a task ignorar o shutdown. Nenhuma das duas manda mensagem indevida — as duas
falham fechadas —, mas ligar a régua sem cobrir a primeira é ligar no escuro.

---

## Task 5: monitoramento da primeira hora

**Files:** `docs/CUTOVER.md` (seção), `scripts/monitorar_cutover.py`

Queries prontas e o que cada número deveria ser:

- fila por status e por canal — pendentes crescendo é sinal de worker parado
- mensagens falhadas com `attempts >= MAX_ATTEMPTS`
- leads criados na última hora — deveria ser ~0 logo após a migração
- `agent_active = false` criados na última hora — handover disparando muito é
  sinal de que a Renata está travando
- taxa de fallback de balão único, que indica saída fora do schema
- eventos criados no Google Calendar na última hora

E, explicitamente, **qual número é motivo para reverter**.

- [ ] **Steps: escrever as queries, testar contra o banco de dev com dados
  semeados, commit**

---

## Pendências que atravessam esta fase

| o quê | quem resolve |
|---|---|
| Os 28 números da blocklist (nó `Filtro e Permissao v002`) | **usuário** — bloqueia a Task 6 da Fase 4 |
| Payload real do ChatWoot | **usuário** — bloqueia a Task 5 da Fase 3 e o passo 9 |
| `OPENROUTER_API_KEY` real | **usuário** — a Renata nunca rodou com LLM de verdade |
| Decisão sobre o lead de Moçambique | **usuário**, no passo 5 |
| Cobrir o wiring de `main()` antes de ligar a régua | agente, antes do passo 12 |
| Rotacionar o PAT do GitHub e o token do n8n | **usuário** |
