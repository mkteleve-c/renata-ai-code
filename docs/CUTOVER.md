# Cutover: n8n → harness (agente `elevec_sdr`, a Renata)

Este documento é o roteiro que **uma pessoa executa** para desligar o sistema
atual (n8n + Supabase) e ligar o harness (Railway) como o único sistema
respondendo pelo número comercial de WhatsApp da EleveC.

Não é uma referência de arquitetura — para isso, `docs/AGENTE_ELEVEC.md` (o
agente), `docs/RAILWAY.md` (a infraestrutura) e `docs/EVOLUTION.md` (o
canal). Este documento presume que a infraestrutura Railway **já está no ar**
(checklist de `docs/RAILWAY.md` concluído) e que você está prestes a virar a
chave — possivelmente à noite, possivelmente sozinho, possivelmente sem ter
acompanhado o resto desta migração.

**Leia o documento inteiro antes de rodar o primeiro comando**, em especial a
seção "Plano de volta" abaixo. Você precisa saber como desfazer antes de
começar a fazer.

## Antes de começar

- [ ] Você tem acesso ao painel do n8n (para desligar os 6 workflows)
- [ ] Você tem acesso ao painel da Evolution API (para repontar o webhook)
- [ ] Você tem acesso ao Railway (dashboard e/ou `railway` CLI logada)
- [ ] Você tem o `.env` da API/Worker do Railway disponível para rodar os
      scripts localmente contra o banco de produção (`DATABASE_URL`,
      `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, credenciais de Evolution/
      Google/Pipedrive) — ou terminal via `railway run --service worker`
- [ ] Você sabe qual horário isto está acontecendo e por quê (fora do horário
      comercial reduz o número de leads em trânsito durante a janela)
- [ ] Você leu a seção "Plano de volta" deste documento

Nenhum destes scripts escreve por padrão — `migrar_supabase.py` roda em
`--dry-run` sem argumento, `preflight_cutover.py` só lê. O único momento em
que este roteiro grava dado novo em produção é o passo 6
(`migrar_supabase.py --executar`), depois de um portão humano explícito no
passo 5.

---

## Plano de volta — leia isto antes do passo 1

**O Supabase não é alterado por nada neste roteiro.** `migrar_supabase.py`
só lê dele, via REST, e nunca escreve. Isso significa que, a qualquer momento
até o passo 6 (inclusive), voltar atrás é trivial: o n8n ainda aponta pro
Supabase inalterado, e religar os workflows restaura o estado exato de antes.

**Depois do passo 6**, o Postgres do Railway passa a ter os dados
importados, mas o Supabase **continua intacto e continua sendo o que o n8n
usa** — os dois sistemas não competem pelo mesmo dado depois do cutover
porque o n8n, religado, volta a escrever nele como sempre escreveu. O
harness fica com uma cópia congelada no momento da migração, que simplesmente
para de ser usada se você reverter.

### Como reverter, em qualquer ponto até o passo 10

1. **Religue os 6 workflows do n8n** (mesmo painel do passo 3, toggle
   "Active" de volta).
2. **Reaponte o webhook da instância Evolution** (`instancia-apioficial`)
   para a URL que o n8n usava antes do passo 8 — confira o valor anterior no
   histórico de configuração da instância, ou no workflow `#1 Agente SDR`
   antes de desligá-lo (passo 3), se ainda não tiver essa URL anotada.
3. Se o passo 9 (ChatWoot) chegou a ser executado — hoje não deveria, está
   bloqueado — reaponte o webhook do ChatWoot de volta também.
4. **Não delete nem restaure nada no Railway.** O Postgres do Railway com os
   dados importados pode ficar como está; ele simplesmente para de receber
   tráfego assim que o webhook da Evolution volta a apontar para o n8n. Uma
   nova tentativa de cutover no futuro roda `migrar_supabase.py` de novo — o
   `ON CONFLICT` da importação já lida com reimportação sobre uma
   `leads_crm` não-vazia (mas leia o preflight: ele **bloqueia** rodar
   `--executar` com `leads_crm` não-vazia como proteção contra isso acontecer
   sem intenção — para uma segunda tentativa deliberada, essa checagem
   específica precisa ser conscientemente ignorada, não contornada por
   acidente).
5. **Anote o motivo da reversão** antes de sair da tela — quem for tentar de
   novo precisa saber o que já foi tentado.

O ponto sem volta simples é **depois do passo 10** (teste de fumaça passou,
sistema novo está recebendo tráfego real): a partir daí, leads podem ter
conversado com a Renata e não com o n8n, e reverter significa que essas
conversas ficam "presas" no harness enquanto o n8n (religado) não sabe que
elas aconteceram. Reverter ainda é possível, mas exige decidir manualmente o
que fazer com esses leads — não é mais um toggle limpo.

---

## O roteiro

### Passo 1 — Backup

Duas coisas, nesta ordem:

1. **Backup do volume do Postgres do Railway** (`docs/RAILWAY.md`, seção
   "Backup do volume do Postgres antes do cutover") — mesmo ele estando
   praticamente vazio agora (o cutover é a primeira carga), backup antes de
   qualquer escrita é rotina, não exceção.
2. **Confira que o Supabase está intacto** — rode uma leitura qualquer
   (contagem de linhas em `leads` e `messages`, ou o próprio dry-run do passo
   4 adiantado) e anote os números. É a referência para comparar depois, e é
   a prova de que a origem — seu caminho de volta — está onde deveria.

**Como saber que deu certo:** você tem um arquivo `.sql.gz` de backup fora do
Railway, e uma contagem anotada do Supabase.

**Se der errado:** não prossiga. Backup falho não é motivo para pular —
resolva antes de continuar.

### Passo 2 — `preflight_cutover.py`

```bash
uv run python scripts/preflight_cutover.py
```

Roda contra o ambiente que vai virar produção (aponte `DATABASE_URL` e as
demais variáveis para o Railway — via `railway run --service worker uv run
python scripts/preflight_cutover.py`, ou copiando o `.env` localmente).
Imprime uma tabela com as doze checagens e termina com código de saída `0`
só se todas passarem.

**Como saber que deu certo:** a última linha diz
`12/12 checagens passaram -- pré-voo ok.` e o processo sai com código `0`
(`echo $?` depois de rodar).

**Se der errado:** **pare aqui.** A tabela lista exatamente o que falhou e
por quê — nenhum detalhe vaza credencial, só nomes de variável ausente,
status HTTP e contagens. Corrija o item, rode de novo. Não avance para o
passo 3 com qualquer checagem em `FALHOU`.

### Passo 3 — Desligar os 6 workflows do n8n

No painel do n8n, desligue (toggle "Inactive") os seis workflows do sistema
atual:

1. `#0 Form & Primeira Abordagem`
2. `#1 Agente SDR | 10/02/26 | V2.2`
3. `#1.1 Handover via ChatWoot`
4. `#2 Follow Up | 16/01/2026 | v3`
5. `#3 Agenda MCP | 12 Fev 26 | v2`
6. `#4 CRM Control | 16/01/2026 | v2`

(O `#5 Handover | 10/02/26 | v2` é acionado pelo `#1`, mas desligue-o também
se aparecer como ativo separadamente.)

**A partir daqui o sistema antigo não escreve mais em lugar nenhum, e a
janela do cutover começa.** Quanto mais rápido os passos seguintes, menor a
janela em que ninguém está atendendo leads.

**Como saber que deu certo:** os seis (ou sete) workflows aparecem como
"Inactive" no painel do n8n. Mande uma mensagem de teste de um número que não
seja lead real e confirme que nada responde.

**Se der errado:** se um workflow não desligar (erro do n8n, permissão),
**não prossiga para o passo 4** — rodar a migração com o n8n ainda
potencialmente escrevendo no Supabase quebra a premissa de "leitura única
contra dado parado". Resolva o desligamento primeiro.

### Passo 4 — `migrar_supabase.py` (dry-run)

```bash
uv run python scripts/migrar_supabase.py
```

Sem argumento = `--dry-run`, o padrão. Lê o Supabase inteiro (paginado,
validado contra `Content-Range`), funde duplicatas, valida, e escreve
`relatorio_migracao.md` na raiz do repo (gitignored — carrega telefone e
nome de gente real). **Não escreve nada no Postgres.**

**Leia o relatório inteiro** — não só o resumo do stdout. Ele lista totais,
descartes por motivo, grupos fundidos, e as pendências de decisão humana que
o passo 5 resolve.

**Como saber que deu certo:** o processo termina e imprime
`--dry-run (padrão): nada foi gravado. Revise o relatório e rode de novo com
--executar quando estiver pronto.` Os totais no relatório fazem sentido
(ordem de grandeza esperada: ~3.373 leads, ~8.920 mensagens de histórico,
salvo o que mudou desde a última vez que esse número foi medido).

**Se der errado:** erro de rede/paginação sai com `ERRO:` no stderr e nunca
vaza a credencial do Supabase — corrija e rode de novo, é só leitura. Se os
totais do relatório destoarem muito do esperado (muito menos ou muito mais
linhas), **pare e investigue antes do passo 5** — pode ser paginação
truncada silenciosamente em algum outro lugar, ou o Supabase mudou de estado
entre medições.

### Passo 5 — Portão humano (não pule)

**Pare. Leia as pendências de decisão do relatório antes de escrever
qualquer coisa.** Este passo existe porque `--executar` no passo 6 é
irreversível na prática (o caminho de volta depois dele exige decisão
manual, não um toggle) — decidir depois de já ter gravado é tarde.

Pendências conhecidas hoje, que precisam de resposta explícita antes de
prosseguir:

- **Lead de Moçambique** (`258864038352`), hoje em fase `qualificado` e com
  o agente ativo. Decida: ele entra na migração como está (o agente
  `elevec_sdr` continua a conversa com ele normalmente), ou é tratado como
  exceção (ex: `agent_active=false` manual depois da importação, se a EleveC
  não atende esse mercado)?
- **Números de EUA e Portugal** — mesma pergunta: a Renata deveria continuar
  atendendo esses leads pelo canal novo, ou eles ficam fora de escopo?
- **Qualquer grupo cuja fusão mudou `phase` ou `agent_active`** — o
  relatório lista esses grupos explicitamente (regra determinística de
  fusão: o maior rank de fase vence, `agent_active` só continua `true` se as
  duas linhas de origem estavam `true`). Releia cada um: a fusão escolheu a
  fase e o estado de ativação corretos para aquele lead específico?

Se qualquer resposta implicar em mudar algo **antes** de gravar, ajuste os
dados na origem (Supabase) e rode o passo 4 de novo — não corrija depois do
`--executar` torcendo para o `ON CONFLICT` absorver a correção; ele foi
desenhado para reimportação seguindo a mesma regra determinística, não para
overrides pontuais.

**Como saber que deu certo:** você tem uma resposta explícita, por escrito
(neste documento, num ticket, onde for), para cada pendência que o relatório
listou — não só as três conhecidas acima, que podem não ser as únicas na
sua execução.

**Se der errado:** se você não consegue decidir sozinho (ex: decisão de
escopo de mercado que não é sua para tomar), **pare o roteiro aqui** e volte
depois de ter a resposta. Não existe valor default seguro para "prosseguir
sem decidir" — é exatamente o tipo de omissão que este passo existe para
não deixar passar batido.

### Passo 6 — `migrar_supabase.py --executar`

```bash
uv run python scripts/migrar_supabase.py --executar
```

Roda a migração de verdade: grava em `leads_crm` e `legacy_chat_history`. As
validações internas **abortam a escrita inteira** (nenhuma linha parcial) se
algo não fechar — ver `MigracaoAbortada` no script.

**Como saber que deu certo:** o processo termina e imprime
`--executar: N lead(s) gravado(s), ...` com `N` batendo com o total do
relatório do passo 4 (descontadas fusões — grupos fundidos viram uma linha
só). Código de saída `0`.

**Se der errado:** uma validação que aborta sai com `ERRO:` e **nenhuma
linha foi gravada** — o script foi desenhado para isso ser tudo-ou-nada.
Volte ao passo 5, entenda o que a validação apontou, ajuste e rode de novo.
Não force uma segunda tentativa sem entender por que a primeira abortou.

### Passo 7 — Conferir `max(last_interaction_at)`

```sql
select max(last_interaction_at) from leads_crm;
```

Compare com o `max(last_interaction_at)` que você tinha anotado do Supabase
(passo 1) e com o que o relatório do passo 4 registrou. Os dois precisam
bater — é a evidência de que a migração pegou o dado mais recente, e não uma
foto velha por algum problema de paginação ou cache.

**Como saber que deu certo:** o valor bate (mesmo timestamp, ou diferença
explicável pelo tempo que passou entre a medição do passo 1 e agora).

**Se der errado:** se o valor no Postgres é **mais velho** que o esperado, a
migração pode ter lido uma página incompleta apesar da validação de
`Content-Range` — não prossiga; investigue antes do passo 8. Não é
recuperável só rodando `--executar` de novo por cima (a constraint de
`leads_crm` não-vazia do preflight bloquearia; contornar essa proteção sem
entender a causa é o erro que ela existe para evitar).

### Passo 8 — Repontar o webhook da Evolution

No painel da instância `instancia-apioficial` (Evolution API), configure o
webhook para:

```
https://<domínio-da-api>/webhook/evolution?agent=elevec_sdr
```

E, se `EVOLUTION_WEBHOOK_SECRET` estiver configurada (obrigatório em
produção — ver `docs/EVOLUTION.md`), configure o header customizado
`X-Evolution-Webhook-Secret` com o mesmo valor da variável de ambiente do
Worker/API no Railway. **Não reutilize o valor de `EVOLUTION_API_KEY`** como
secret — são credenciais diferentes por design.

Confirme os eventos habilitados incluem o de mensagem recebida (`messages.
upsert` ou variação do nome — ver `docs/EVOLUTION.md`).

**Como saber que deu certo:** mande uma mensagem de teste (de um número que
não seja lead real) e confira em `message_queue` (via pgweb ou psql) que uma
linha nova apareceu com `channel = 'evolution'`. Ainda não precisa
responder — isso é o passo 10.

**Se der errado:** se a mensagem não aparece na fila, confira primeiro se o
webhook está mesmo salvo (alguns painéis da Evolution não persistem a
mudança sem um "Save" explícito) e se a URL não tem erro de digitação no
`?agent=`. Um 401 nos logs da API é secret errado/ausente; um 200 com
`motivo: agente_desconhecido` é erro no valor de `agent`.

### Passo 9 — Repontar o webhook do ChatWoot

**Este passo está bloqueado hoje.** A rota `POST /webhook/chatwoot` depende
da Task 5 da Fase 3, que não pode começar sem um payload real do ChatWoot
capturado (Task 0 daquela fase) — ver
`docs/superpowers/plans/2026-07-27-fase3-perifericos.md`. Não existe rota
para receber esse webhook ainda.

**A consequência de pular este passo permanentemente não é neutra.** A
etiqueta `pausar_agente` no ChatWoot é o mecanismo pelo qual um humano
desliga a Renata para uma conversa específica (ex: o Silvio assume o
atendimento manualmente). Sem o webhook cadastrado — porque a rota não
existe —, aplicar a etiqueta no ChatWoot **não faz nada**: a Renata continua
respondendo normalmente. **O único sinal de que isso está acontecendo é o
agente respondendo por cima de um atendimento humano em andamento** — não há
erro, não há log de falha, porque do ponto de vista do harness nada de
errado aconteceu; ele nunca soube que devia parar.

Até esta task ser desbloqueada e implementada:

- **Combine com quem atende pelo ChatWoot** que a etiqueta `pausar_agente`
  não tem efeito nenhum hoje — o desligamento manual do agente, se
  necessário, precisa ser feito por outro caminho (ex: `agent_active=false`
  direto no banco via pgweb, para um lead específico, até o webhook existir).
- Não anuncie a etiqueta como funcional para a equipe de atendimento antes
  da Task 5 da Fase 3 estar concluída e testada.

**Como saber que deu certo:** não aplicável — o passo não pode ser
concluído hoje. O "sucesso" deste passo, por ora, é a equipe de atendimento
ciente da limitação acima.

**Se der errado:** não é possível errar um passo que não existe ainda. O
erro real seria pular esta seção e deixar a equipe de atendimento acreditar
que a etiqueta funciona.

### Passo 10 — Teste de fumaça

De um número próprio (não um lead real), mande uma mensagem simulando o
início de uma conversa (ex: "Oi, vim pelo formulário do LinkedIn").

Acompanhe o ciclo inteiro:

1. Mensagem aparece em `message_queue` com `channel='evolution'`,
   `status='queued'`.
2. Status muda para `processing` e depois `done` (worker pegou e processou).
3. A resposta chega no WhatsApp do número de teste, **em múltiplos balões**
   (a Renata responde em `{"messages": [...]}`, um `send_message` por item —
   ver `docs/AGENTE_ELEVEC.md`, "Saída em balões"). Uma única mensagem longa
   em vez de vários balões curtos é sinal de fallback de schema (ver
   `scripts/monitorar_cutover.py`, taxa de fallback).
4. `leads_crm` tem uma linha nova (ou atualizada) para o número de teste,
   com `phase` condizente com o que você disse na conversa.

**Como saber que deu certo:** os quatro itens acima acontecem, na ordem, em
menos de ~1 minuto (`LEASE_SECONDS` default é 60s; se passar muito disso sem
resposta, algo está preso).

**Se der errado:** use a skill `debug-queue` (`.claude/skills/debug-queue/
SKILL.md`) para investigar mensagens presas em `queued`/`processing`. Se o
teste de fumaça falhar de um jeito que pareça sistêmico (não um caso de
borda), **volte ao plano de reversão** no topo deste documento — não avance
para o passo 11 com o teste de fumaça vermelho.

### Passo 11 — Monitorar a primeira hora

Ver `scripts/monitorar_cutover.py` e a seção "Monitoramento da primeira
hora" abaixo. Mantenha a fila à vista — literalmente, um terminal rodando o
script em intervalos, ou a aba SQL do pgweb aberta — durante pelo menos a
primeira hora de tráfego real.

### Passo 12 — `FOLLOWUP_ENABLED` — decisão separada, não hoje

**Não ligue `FOLLOWUP_ENABLED=true` no dia do cutover.** Ver a seção
"O que NÃO ligar no dia" abaixo antes de considerar isso, e só depois que os
passos 1–11 estiverem estáveis por um período razoável (não definido aqui em
horas exatas — é julgamento operacional, não um número mágico).

---

## O que NÃO ligar no dia

`FOLLOWUP_ENABLED` fica `false` no primeiro deploy e continua `false` depois
do cutover, como decisão deliberada e separada. Duas dívidas concretas da
Fase 3 sustentam isso:

1. **O wiring de `main()` que liga a régua tem cobertura zero.** Em
   `src/whatsapp_langchain/worker/main.py:276`, a linha
   `followup_task = iniciar_followup(pool, outbounds)` não tem nenhum teste
   que falhe se ela virar `followup_task = None`. A lógica interna da régua
   (`worker/followup.py`) está bem testada; o fio que a liga dentro do
   processo do worker, não.
2. **Trocar `except Exception` por `except BaseException`** no loop
   (`_loop_followup`, `worker/main.py:165`) faria a task engolir
   `asyncio.CancelledError` — o sinal de shutdown gracioso — e o worker
   pararia de desligar limpo sob essa mudança. O código está correto hoje; a
   dívida é a fragilidade de alguém alterar isso sem perceber a consequência,
   sem um teste que pegue o erro.

Nenhuma das duas dívidas manda mensagem indevida para um lead — as duas
falham fechadas (régua desligada continua desligada; shutdown mal feito no
pior caso atrasa, não duplica envio). Mas ligar a régua sem cobrir a dívida
1 é ligar no escuro: se `iniciar_followup` parar de ser chamado num refactor
futuro, nada no CI apontaria, e a régua simplesmente pararia de rodar sem
nenhum sinal — o oposto do que se espera de uma régua de reengajamento
comercial.

Quando alguém decidir ligar `FOLLOWUP_ENABLED=true`:

- Cubra a dívida 1 primeiro (um teste que falhe se `iniciar_followup` não
  for chamado em `main()`).
- Ligue como mudança isolada, fora da janela do cutover, com a mesma
  disciplina de monitoramento da primeira hora.

---

## Monitoramento da primeira hora

Ver `scripts/monitorar_cutover.py` para as métricas automatizadas. Rode:

```bash
uv run python scripts/monitorar_cutover.py
```

Ele imprime uma tabela com seis medições e, ao final, se algum limiar de
reversão foi cruzado, uma seção **"MOTIVO PARA REVERTER"** destacada — não
"monitore com atenção", um limiar explícito por métrica (documentados nas
seções abaixo e no docstring do próprio script). Rode em intervalos durante
a primeira hora (ex: a cada 5–10 minutos) — cada chamada é uma fotografia,
não um processo contínuo.

### 1. Fila por status e por canal

O que é: contagem de `message_queue` agrupada por `(channel, status)`, mais
uma checagem separada de mensagens em `queued`/`processing` há mais de 5
minutos (`FILA_PARADA_MINUTOS` no script — 5x o `LEASE_SECONDS` default de
60s, tempo de sobra para qualquer retry legítimo).

**O que deveria ser:** a maioria das linhas em `done`; `queued`/`processing`
baixando a zero rapidamente à medida que o worker processa; nada parado há
mais de 5 minutos.

**Motivo para reverter:** qualquer mensagem em `queued`/`processing` há mais
de 5 minutos. Isso é fila real de leads reais esperando resposta com o
sistema antigo já desligado — o worker parou, travou, ou não está rodando.

### 2. Mensagens falhadas com `attempts >= MAX_ATTEMPTS`

O que é: contagem de `message_queue` com `status='failed'` e
`attempts >= max_attempts`, na última hora.

**O que deveria ser:** 0. Uma falha isolada pode acontecer (timeout de rede
pontual); mais que isso na primeira hora é padrão, não acaso.

**Motivo para reverter:** 5 ou mais mensagens falhadas com tentativas
esgotadas na primeira hora. Cada uma é um lead real que não recebeu
resposta nenhuma depois de `MAX_ATTEMPTS` tentativas.

### 3. Leads criados na última hora

O que é: contagem de `leads_crm` com `created_at` na última hora.

**O que deveria ser:** próximo de zero logo após a migração — os leads que
já existiam vieram com `created_at` **preservado** do Supabase (o mais
antigo entre linhas fundidas), não `now()`. Um número alto aqui, logo depois
do passo 6, é sinal de que a migração duplicou linhas em vez de fundir, ou
de que o gate de ingestão está criando leads novos para telefones que já
deveriam existir (canonicalização divergente entre `phone.py` e o que a
migração gravou).

**Não é, sozinho, motivo de reversão** — um número alto de leads novos de
verdade (uma campanha ativa, por exemplo) é bom sinal de negócio, não de
sistema quebrado. Tratar como alerta para investigar, cruzando com a métrica
5 (taxa de fallback) e a 4 (handover) antes de decidir.

### 4. `agent_active = false` entre os leads criados na última hora

O que é: dos leads com `created_at` na última hora (métrica 3), quantos já
têm `agent_active = false`.

**O que deveria ser:** próximo de zero. Um lead recém-chegado sendo
desligado do agente quase imediatamente é sinal de handover disparando
demais — a Renata travando em algo (erro de tool, resposta fora do SOP) e
caindo no caminho de desligamento como consequência.

**Motivo para reverter:** taxa de 50% ou mais entre os leads novos da
janela, com pelo menos 3 leads novos na amostra (amostra menor que isso é
ruído, não padrão).

### 5. Taxa de fallback de balão único

O que é: entre as respostas do `elevec_sdr` concluídas (`status='done'`) na
última hora, a fração cujo `response` **não** é um JSON válido no formato
`{"messages": [...]}` com só strings não-vazias — o schema que
`extrair_baloes` (`agents/catalog/elevec_sdr/saida.py`) espera. Fora desse
formato, a resposta inteira vira um único balão (fallback), o que a Renata
nunca faz quando está seguindo o SOP.

**O que deveria ser:** baixo (algum ruído de modelo é esperado — não é 0%
numa integração real com LLM). Um número crescente indica que o contrato de
saída quebrou (mudança de modelo, mudança de prompt, ou o modelo
alucinando fora do formato sistematicamente).

**Motivo para reverter:** 30% ou mais de fallback, com pelo menos 3
respostas na amostra. Acima disso, a Renata está estruturalmente fora do
schema esperado, não é caso isolado.

### 6. Eventos criados no Google Calendar na última hora

O que é: consulta direta à API do Google Calendar (não ao Postgres — a
tabela `leads_crm` não guarda quando um `google_event_id` foi escrito,
só o valor atual), contando eventos cujo campo `created` (nativo da API do
Google, distinto do horário de início do compromisso) cai na última hora.

**O que deveria ser:** informativo — não tem "deveria ser zero" nem limiar
de reversão. Zero eventos na primeira hora é normal (poucos leads chegam a
agendar tão rápido); um número existir é evidência de que `agendar_
consultoria` está funcionando ponta a ponta.

**Se a checagem falhar** (Google Calendar inalcançável, refresh token
inválido): o script reporta a falha nesta métrica isoladamente e continua —
não é motivo de reversão por si só, mas se combinado com falhas nas métricas
1–5, considere que o agendamento pode estar quebrado silenciosamente para
quem chegou a esse ponto do SOP.

### Lendo o "MOTIVO PARA REVERTER" do script

Se **qualquer uma** das métricas 1, 2, 4 ou 5 cruzar o limiar documentado
acima, o script imprime a seção destacada com o(s) motivo(s) — pode ser mais
de um ao mesmo tempo. Trate como o gatilho para voltar à seção "Plano de
volta" deste documento, não como sugestão para "monitorar com mais atenção".
