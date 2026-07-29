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
- [ ] O TCP Proxy do serviço `db` está habilitado (`docs/RAILWAY.md`, seção
      "Como alcançar o Postgres de produção de fora do Railway") — **sem
      isto, os passos 2, 4 e 6 não conseguem se conectar ao Postgres de
      produção a partir do seu laptop.** `DATABASE_URL` (a reference
      variable interna) só resolve dentro da rede privada do Railway;
      `railway run` executa os scripts NA SUA MÁQUINA, não dentro do
      Railway, e injetaria essa mesma `DATABASE_URL` interna se você não
      seguir a seção acima
- [ ] Você tem `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` e as credenciais
      de Evolution/Google/Pipedrive do serviço `worker` disponíveis (via
      `railway variable list --service worker --kv` ou o dashboard) para
      rodar os scripts localmente
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
   qualquer escrita é rotina, não exceção. Essa seção já exige TCP Proxy
   habilitado (`docs/RAILWAY.md`, "Como alcançar o Postgres de produção de
   fora do Railway") e prova, com contagem de linhas, que o dump veio do
   banco certo — não basta o arquivo `.sql.gz` existir.
2. **Confira que o Supabase está intacto** — rode uma leitura qualquer
   contra as tabelas de origem (`leads_crm` e `n8n_chat_histories` — **não**
   `leads`/`messages`, esses nomes não existem no Supabase deste projeto; os
   dois nomes certos são os mesmos que `scripts/migrar_supabase.py` lê, ver
   `_TABELA_LEADS`/`_TABELA_HISTORICO`) e anote:
   - contagem de linhas das duas tabelas;
   - `max(last_interaction_at)` de `leads_crm` (ex.: `select
     max(last_interaction_at) from leads_crm;` direto no Supabase, via SQL
     Editor do painel ou `psql`).

   O próprio dry-run do passo 4, adiantado, também serve — ele já imprime
   `max(last_interaction_at) entre os leads migrados` no resumo. É a
   referência para o passo 7 comparar depois, e é a prova de que a origem —
   seu caminho de volta — está onde deveria.

**Como saber que deu certo:** você tem um arquivo `.sql.gz` de backup fora do
Railway **com a contagem de `leads_crm` do dump conferida contra o banco**
(não só o arquivo existindo — ver o critério da própria seção de backup em
`docs/RAILWAY.md`), e uma contagem de linhas **e** um `max(last_interaction_
at)` anotados do Supabase.

**Se der errado:** não prossiga. Backup falho não é motivo para pular —
resolva antes de continuar.

### Passo 2 — `preflight_cutover.py`

Roda contra o ambiente que vai virar produção. **`railway run --service
worker uv run python scripts/preflight_cutover.py`, sozinho, NÃO funciona**
— `railway run` executa o comando na SUA máquina, só injetando as variáveis
do serviço `worker`, e `DATABASE_URL` entre elas é a reference variable
interna (`...@db.railway.internal:5432/...`), que só resolve dentro da rede
privada do Railway. "Copiar o `.env` localmente" tem o mesmo defeito: o
valor copiado é esse mesmo host interno. Sem corrigir isso, este passo trava
até dar timeout de conexão — com o n8n já potencialmente desligado (se você
pulou a ordem e já fez o passo 3), essa é a pior hora para descobrir isso.

Siga `docs/RAILWAY.md`, seção "Como alcançar o Postgres de produção de fora
do Railway", **antes** de rodar este passo — ela monta a `DATABASE_URL`
pública via TCP Proxy. Com isso feito:

```bash
railway run --service worker env DATABASE_URL="$DATABASE_URL_PUBLICA" \
  uv run python scripts/preflight_cutover.py
```

Imprime uma tabela com as treze checagens e termina com código de saída `0`
só se todas passarem.

**Como saber que deu certo:** a última linha diz
`13/13 checagens passaram -- pré-voo ok.` e o processo sai com código `0`
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

Mesma ressalva de alcance do passo 2: rode com a `DATABASE_URL` pública já
montada (`docs/RAILWAY.md`, "Como alcançar o Postgres de produção de fora do
Railway") — `migrar_supabase.py` também abre um pool contra `DATABASE_URL`
para gravar em modo `--executar` (passo 6) e, mesmo em dry-run, precisa de
`SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` (do serviço `worker`) para ler a
origem:

```bash
railway run --service worker env DATABASE_URL="$DATABASE_URL_PUBLICA" \
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
salvo o que mudou desde a última vez que esse número foi medido). O resumo
(stdout e `relatorio_migracao.md`) também imprime `max(last_interaction_at)
entre os leads migrados` — anote esse valor junto com o que você já anotou
no passo 1: é o segundo lado da comparação do passo 7.

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

Mesma `DATABASE_URL` pública do passo 4/2 (ela precisa ser a mesma sessão de
terminal, ou re-monte com a receita de `docs/RAILWAY.md`):

```bash
railway run --service worker env DATABASE_URL="$DATABASE_URL_PUBLICA" \
  uv run python scripts/migrar_supabase.py --executar
```

Roda a migração de verdade: grava em `leads_crm` e `legacy_chat_history`. As
validações internas **abortam a escrita inteira** (nenhuma linha parcial) se
algo não fechar — ver `MigracaoAbortada` no script.

**Como saber que deu certo:** o processo termina e imprime
`--executar: N lead(s) gravado(s), ...` com `N` batendo com o total do
relatório do passo 4 (descontadas fusões — grupos fundidos viram uma linha
só). Código de saída `0`.

Logo em seguida, o processo imprime uma linha própria: `Linha de base para o
monitoramento da primeira hora (passo 11): M lead(s) com agent_active=false
agora.` **ANOTE esse número M** — é o argumento obrigatório
(`--linha-de-base-handover M`) que `scripts/monitorar_cutover.py` (passo 11)
precisa para medir quantos handovers novos acontecem depois do cutover (ver
"Monitoramento da primeira hora" abaixo, item 4 — a métrica não tem como
funcionar sem esse número, e não existe um default seguro para "esqueci de
anotar").

**Se der errado:** uma validação que aborta sai com `ERRO:` e **nenhuma
linha foi gravada** — o script foi desenhado para isso ser tudo-ou-nada.
Volte ao passo 5, entenda o que a validação apontou, ajuste e rode de novo.
Não force uma segunda tentativa sem entender por que a primeira abortou.

### Passo 7 — Conferir `max(last_interaction_at)`

Com a `DATABASE_URL` pública ainda ativa na sessão (passo 2), rode via
`psql`:

```bash
railway run --service db bash -c '
  psql -h "$RAILWAY_TCP_PROXY_DOMAIN" -p "$RAILWAY_TCP_PROXY_PORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -tAc "select max(last_interaction_at) from leads_crm;"
'
```

Compare o resultado com **os dois lados** que já existem neste ponto do
roteiro:

1. **O que você anotou no passo 1** — o `max(last_interaction_at)` que você
   leu direto do Supabase (`leads_crm`) antes de qualquer coisa, ou o do
   dry-run adiantado do passo 4.
2. **O que o passo 4/6 registrou** — o resumo de `migrar_supabase.py`
   (stdout e `relatorio_migracao.md`) imprime `max(last_interaction_at)
   entre os leads migrados`; é o mesmo valor calculado em memória, ANTES da
   escrita.

Os três (Postgres agora, passo 1, passo 4/6) precisam bater — é a evidência
de que a migração pegou o dado mais recente, e não uma foto velha por algum
problema de paginação ou cache.

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
não seja lead real) e confira em `message_queue` que uma linha nova
apareceu com `channel = 'evolution'` — via `/queue` do admin panel
(`https://<frontend>/queue`, não precisa de TCP Proxy nem `psql`) ou via
`psql` direto (TCP Proxy — ver `docs/RAILWAY.md`; **não pgweb** — este
projeto não roda pgweb no Railway, é exclusivo do deploy self-hosted em
VPS/Traefik, ver `deploy/README.md`). Ainda não precisa responder — isso é o
passo 10.

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
  direto no banco via `psql`, TCP Proxy — ver `docs/RAILWAY.md`; este
  projeto não roda pgweb no Railway — para um lead específico, até o
  webhook existir).
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
hora" abaixo. Exige `--linha-de-base-handover M`, o número que o passo 6
mandou anotar — mesma `DATABASE_URL` pública das etapas anteriores:

```bash
railway run --service worker env DATABASE_URL="$DATABASE_URL_PUBLICA" \
  uv run python scripts/monitorar_cutover.py --linha-de-base-handover M
```

Mantenha a fila à vista — literalmente, um terminal rodando o script em
intervalos, ou `psql` aberto contra a `DATABASE_URL` pública (TCP Proxy) —
durante pelo menos a primeira hora de tráfego real.

### Passo 11.5 — Esvaziar `ALLOWLIST_PHONES` — o passo que nenhum portão cobra

**Se você usou uma janela de teste, a Renata está atendendo SÓ os telefones
de `ALLOWLIST_PHONES`. Todo lead real está sendo descartado em silêncio.**

Este é o único passo do roteiro que nenhuma verificação automática pega, e
vale entender por quê antes de confiar nelas:

- **`preflight_cutover.py` fica verde.** A 13ª checagem avisa quando a
  allowlist está preenchida, mas ela é um **aviso**, não um bloqueio — o
  pré-voo precisa passar durante a janela de teste, senão ninguém consegue
  testar. Leia a linha, não só o `12/13 passaram`.
- **O teste de fumaça do passo 10 fica verde.** Ele manda usar "um número
  próprio, não um lead real" — e o número próprio do operador é exatamente
  o que está na allowlist.
- **O monitoramento do passo 11 fica verde.** As seis métricas contam
  coisa ruim (fila parada, falhas, handover, fallback de balões). O gate
  descarta antes de qualquer escrita: zero mensagem produz zero fila, zero
  falha, zero tudo. Nenhum limiar de reversão dispara.

Ou seja: n8n desligado, webhook repontado, todo lead real em silêncio
absoluto, e os três portões verdes.

```bash
railway variables --service api    --set 'ALLOWLIST_PHONES='
railway variables --service worker --set 'ALLOWLIST_PHONES='
```

**Como saber que deu certo:** o boot dos dois serviços loga
`allowlist_ativa=false`. Com a allowlist ligada ele loga
`allowlist_ativa=true allowlist_permitidos=N`.

```bash
railway logs --service worker -d --lines 50 | grep worker_ready
railway logs --service api    -d --lines 50 | grep server_ready
```

**Confira também `allowlist_descartadas` no mesmo log.** Se ele vier
não-vazio, alguma entrada não era telefone — e se TODAS forem recusadas
(separar por espaço em vez de vírgula faz isso), a lista fica vazia e a
trava **evapora**, liberando todo mundo sem nenhum outro sinal.

**Quem escreveu durante a janela não vira rajada.** O gate desliga
`followup_active` de quem descartou, justamente para o relógio andar sem
gerar cobrança sobre uma conversa que a empresa nunca respondeu. Esses leads
voltam à régua sozinhos na primeira mensagem que mandarem depois daqui.

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

Ver `scripts/monitorar_cutover.py` para as métricas automatizadas. Rode (com
a `DATABASE_URL` pública das etapas anteriores, e `M` = o número que o passo
6 mandou anotar):

```bash
railway run --service worker env DATABASE_URL="$DATABASE_URL_PUBLICA" \
  uv run python scripts/monitorar_cutover.py --linha-de-base-handover M
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

### 4. Handover acumulado desde a linha de base do passo 6

O que é: `count(*) where not agent_active` em `leads_crm` **agora**, menos
`--linha-de-base-handover M` (o mesmo `count(*)`, medido e impresso por
`migrar_supabase.py --executar` logo após gravar). O delta é a contagem de
handovers novos desde o cutover.

**Por que não é "entre os leads criados na última hora" (a versão original
desta métrica):** `leads_crm` não tem `updated_at` — nenhuma coluna diz
"quando `agent_active` virou `false`" — e `human_handover` deixa
`agent_reactivate_at` `NULL` de propósito (não é um timestamp substituto).
Escopar por `created_at`, como a primeira versão desta métrica fazia, mede
uma população **vazia por construção** depois de uma migração: os leads
importados vêm com `created_at` **preservado** do Supabase (o mais antigo
entre linhas fundidas, nunca `now()`), então nenhum lead migrado tem
`created_at` "na última hora" — a métrica ficava sempre `ok, 0/0` mesmo com
dezenas de leads pausados, cega bem na janela em que mais importa (a
primeira hora). Comparar dois `count(*)` absolutos, sem depender de nenhuma
coluna de tempo, é o que resolve isso.

**O que deveria ser:** delta próximo de zero. Um salto no número de leads
pausados logo depois do cutover é sinal de handover disparando demais — a
Renata travando em algo (erro de tool, resposta fora do SOP) e caindo no
caminho de desligamento como consequência. `FOLLOWUP_ENABLED=false` no dia
(ver "O que NÃO ligar no dia" acima) garante que nada religa `agent_active`
sozinho nessa janela, então o delta só deveria crescer — uma queda seria
sinal de alguém reativando um lead manualmente via `psql`, não um bug desta
métrica.

**Motivo para reverter:** delta de 5 ou mais handovers novos desde a linha
de base. 1 a 4 fica em atenção (um handover isolado — lead pedindo humano,
por exemplo — é esperado e não é sozinho motivo de reversão).

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
