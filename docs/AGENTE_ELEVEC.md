# Agente `elevec_sdr` — a Renata

Assistente de pré-vendas da **EleveC**. Qualifica leads por WhatsApp e agenda
a *Consultoria de Alavancagem de Carreira* na agenda de **Silvio Hirata**.

Porte do workflow n8n `#1 Agente SDR | 10/02/26 | V2.2` (id `i5CHQ5VgzrA65kuK`).
A fonte de verdade do prompt é `docs/evidencias/prompt-renata-n8n.md`, extraída
do sistema em produção — **não reescreva o SOP a partir deste documento**.

```
src/whatsapp_langchain/agents/catalog/elevec_sdr/
├── agent.py      build_graph() — create_agent, temperature=0.3
├── graph.py      export `graph` para o LangGraph Studio
├── prompts.py    SYSTEM_PROMPT (o SOP verbatim + 3 mudanças documentadas)
├── contexto.py   middleware que interpola {nome}/{origem}/{telefone}/{data_hoje}
├── saida.py      extrair_baloes() — JSON estruturado → balões de WhatsApp
└── tools/
    ├── agenda.py    5 tools de Google Calendar
    ├── crm.py       update_crm
    ├── handover.py  human_handover
    └── interno.py   o marcador [sistema]
```

Seleção pelo webhook: `POST /webhook/evolution?agent=elevec_sdr`.
`thread_id = "{phone}:elevec_sdr"`.

---

## Quem a Renata atende — e quem ela NÃO atende

Isto é escopo de produto, não detalhe de implementação, e determina quantos
leads a migração toca.

**Ela atende** o lead que preencheu o formulário, **não agendou sozinho** e
está na faixa de **R$ 5 a 8 mil** de faturamento mensal — o roteamento é feito
pelo formulário, antes de a conversa chegar aqui.

**Ela não atende:**

- quem já agendou pelo próprio formulário (esse vai direto para a agenda);
- as faixas de faturamento fora de R$ 5–8 mil, que o time comercial trata por
  outro caminho;
- suporte a cliente ativo, cobrança, ou qualquer assunto pós-venda.

Consequência prática registrada na Fase 4: os leads em `formulario_preenchido`
no banco de origem **não são todos dela**. Conferir a distribuição por faixa
antes de migrar, em vez de assumir que a fila inteira é da Renata.

---

## As 8 fases do SOP

O prompt numera de 0 a 8. Fase 0 não é conversa — é a regra que impede a
Renata de repetir a saudação quando o histórico já tem uma.

| # | Fase | O que acontece |
|---|---|---|
| 0 | Identificação de contexto | Lê o histórico. Se já houve saudação, pula direto para a Fase 2. |
| 1 | Acolhimento | Saudação com o primeiro nome + permissão para uma pergunta de diagnóstico. |
| 2 | Diagnóstico & Filtro | "Qual o principal desafio ou objetivo na sua carreira hoje?" |
| 3 | Aprofundamento | Avalia contra C1/C2. Resposta rasa → pede exemplos. Educa sobre o posicionamento e valida o entendimento. |
| 4 | Ponte e Transição | Validação empática → conexão → fechamento assumido: "qual turno fica melhor?" |
| 5 | Disponibilidade | `calendar_get_many` e oferta **apenas** de horários livres. |
| 6 | **Portão de e-mail** | "Para eu te enviar o convite oficial, qual seu melhor e-mail?" |
| 7 | **Portão de faturamento** | "Qual seu faturamento médio mensal hoje?" Não avança sem resposta clara. |
| 8 | Agendamento | Reconsulta a disponibilidade, `calendar_agendar`, confirma. |

### Critérios de desqualificação

Se qualquer um bater, **não agende**. Use disrupção e requalificação antes de
encerrar, e encerre de forma educada.

- **C1 — Recolocação / "arrumar emprego"**: o lead quer indicação, vaga,
  colocação rápida, ou que o Silvio consiga emprego por ele.
  *"quero uma vaga"*, *"preciso de indicação"*, *"recolocação urgente"*.
- **C2 — Fora do escopo**: o objetivo principal não é carreira corporativa,
  posicionamento ou liderança. *"quero passar em concurso"*, *"quero abrir um
  negócio do zero"*, *"quero terapia como foco"*.

---

## As 7 tools

| Tool | O que faz |
|---|---|
| `calendar_get_many(periodo, a_partir_de)` | Horários livres, já com escassez aplicada. `manha`/`tarde`/`noite`/`qualquer`. |
| `calendar_agendar(inicio, email, faturamento_mensal)` | Cria o evento e grava `google_event_id`. **Recusa sem e-mail e sem faturamento.** |
| `calendar_update(novo_inicio, event_id)` | Reagenda. Reconsulta o novo horário antes de mover. |
| `calendar_delete(event_id)` | Cancela e limpa o vínculo no cadastro. |
| `calendar_get_event(event_id)` | Detalhes da consultoria marcada. |
| `update_crm(phase, email, faturamento_mensal)` | Move o lead no funil e o card no Pipedrive. |
| `human_handover(motivo)` | Desliga o agente para o lead e avisa o responsável. |

O telefone **nunca** vem por argumento: todas resolvem o lead pelo
`configurable` do turno (`telefone_do_turno`). Um `phone` parametrizável
deixaria o modelo desqualificar o lead da conversa anterior.

Toda tool devolve **string** em qualquer desfecho, inclusive erro — exceção
que sobe derruba o turno; uma frase deixa a Renata seguir o SOP.

### Fases aceitas por `update_crm`

`qualificado`, `agendou_sessao`, `desqualificado`. `formulario_preenchido` e
`iniciou_conversa` são do gate de ingestão; `perdido` é julgamento do time
comercial. `agendou_sessao` e `desqualificado` desligam o follow-up;
`desqualificado` **não move card** (o funil não tem estágio de descarte).

---

## Política de agenda

Herdada do workflow `#3 Agenda` do n8n:

- segunda a sexta, **slots de 60 minutos em hora cheia**;
- manhã `8–11`, tarde `13–17`, noite `18–21`;
- janela de **4 dias corridos a partir de D+1** — hoje nunca é oferecido;
- escassez: no máximo **2 dias × 2 horários** (4 slots);
- quando o lead não pede período, a preferência é **tarde → noite → manhã**, e
  o segundo horário vem de outro período (`13, 18` é escolha de verdade;
  `13, 14` é a mesma resposta duas vezes).

**Ocupado é sobreposição de intervalo, nunca igualdade de hora** — a leitura
real da agenda trouxe eventos de duração quebrada e sobrepostos entre si.
Evento de dia inteiro bloqueia o dia inteiro; `status: "cancelled"` não ocupa
nada.

O `event_id` é **determinístico**, derivado de `(lead, slot)`: retry não
duplica consultoria, ele colide em 409. O único `event_id` que
`calendar_update`/`delete`/`get_event` aceitam é o gravado no lead — id
divergente é recusado, não obedecido.

---

## Os portões validados em código

O prompt chama a sequência de "INVIOLÁVEL" e "TERMINANTEMENTE PROIBIDO".
Parágrafo pede; `if` garante. O que está no código:

| Regra | Onde | O que acontece se violada |
|---|---|---|
| Não agendar sem e-mail | `calendar_agendar` | Recusa e manda voltar à Fase 6. |
| Não agendar sem faturamento | `calendar_agendar` | Recusa e manda voltar à Fase 7. |
| Não agendar hoje, fim de semana, meia hora ou fora da grade | `validar_slot` | Recusa com o motivo. |
| Não agendar horário ocupado | reconsulta em `calendar_agendar` | Recusa e pede outro horário. |
| Não marcar duas consultorias para o mesmo lead | `google_event_id` no cadastro | Manda usar `calendar_update`. |
| Nunca reescrever a mesma `phase` | `where phase is distinct from` em `gravar_fase` | Casa zero linhas e **não move card**. |
| `qualificado` nunca sobrescreve `agendou_sessao` | mesmo `where` | Recusa o retrocesso do funil. |
| Card só se move se a fase gravou | `update_crm` | Sem gravação, sem card. |
| Handover desliga o agente antes de dizer que desligou | `pausar_agente` | `rowcount == 0` reporta falha em vez de mentir. |

Cobertura: `tests/unit/test_tool_agenda.py`, `tests/integration/test_tool_agenda_db.py`,
`tests/integration/test_tool_crm.py`, `tests/integration/test_roteiro_sop.py`.

---

## O marcador `[sistema]`

As tools devolvem ao modelo texto operacional — *"o card no Pipedrive não foi
movido"*, *"acione o human_handover"*, *"cadastro do lead"*. Esse texto entra
na conversa como resultado de tool, e nada impede o modelo de repassá-lo ao
lead. No n8n o problema não existia: lá as tools eram nós do workflow.

O contrato tem duas metades, e as duas precisam existir:

1. **`tools/interno.py`** marca a string com o prefixo `[sistema] `.
   `crm.py` e `handover.py` marcam **100%** dos retornos. `agenda.py` marca
   **seletivamente** — as saídas de `calendar_get_many` e a confirmação de
   agendamento carregam fato que o lead precisa, e marcar tudo diluiria o
   sinal até ele não distinguir nada.
2. **O bloco `### Resultado de tool (texto interno)` do prompt** diz o que
   fazer com o marcador: agir sobre o texto e responder ao lead com
   linguagem própria, nunca repetir, citar, traduzir ou resumir.

Sem a metade 2, o prefixo é só mais um pedaço de texto que o modelo repete.

---

## Saída em balões

A Renata é o único agente do catálogo que responde **JSON estruturado**:

```json
{"messages": ["Oi, Marcos!", "Posso te fazer uma pergunta rápida?"]}
```

Cada item vira um balão separado no WhatsApp, espaçados por `BALAO_DELAY_MS`.
`extrair_baloes` (em `saida.py`) faz o parse do **texto final**, depois que o
ciclo de tools terminou — é o mesmo mecanismo do `outputParserStructured` do
n8n, e não `response_format` nativo (que quebra o schema quando há tool call
pendente no mesmo turno).

Desvio de schema cai para o **texto bruto inteiro**, nunca para uma lista
mutilada: perder a formatação é melhor que entregar metade da resposta sem
ninguém perceber. Todo fallback loga `warning`.

---

## Variáveis de ambiente

### Google Calendar (obrigatórias para agendar)

```
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
GOOGLE_CALENDAR_ID=       # o e-mail do calendário
```

OAuth 2.0 de aplicação instalada. **Rotacionar o OAuth Client no Google Cloud
Console invalida o refresh token** e derruba o agendamento até alguém
reautorizar.

### Pipedrive (funil comercial)

```
PIPEDRIVE_API_TOKEN=
PIPEDRIVE_STAGE_QUALIFICADO=12
PIPEDRIVE_STAGE_AGENDADO=13
```

Sem token, a fase ainda é gravada no banco e só o card não se move — deploy
sem Pipedrive é escolha válida, e vira `crm_pipedrive_nao_configurado` no log,
não incidente.

### Handover

```
HANDOVER_NOTIFY_PHONE=    # E.164 ou só dígitos
```

Quem recebe o aviso quando `human_handover` desliga o agente. **Vazio significa
handover silencioso**: o agente desliga, o lead fica esperando e ninguém é
acionado.

### Balões

```
BALAO_DELAY_MS=700        # espaçamento entre balões
BALAO_MAX_COUNT=10        # teto; acima disso o resto concatena no último
```

O teto protege o `lease_seconds`: sem ele, uma resposta com dezenas de itens
soma sleep suficiente para estourar o lease e, com mais de um worker,
duplicar o envio.

### Fail-fast no boot

As **seis** variáveis de Google + Pipedrive + handover formam um grupo só em
`Settings._sdr_credentials_status()`, com a mesma doutrina de "toque parcial"
dos canais de mensagem:

- **nenhuma preenchida** → o SDR fica desabilitado e o boot passa. É o que
  permite a deploys de `illumi_assistant`/`rhawk_assistant` subirem normalmente.
- **alguma preenchida e outra faltando, em `OUTBOUND_MODE=real`** →
  `ValueError` no boot da API e do Worker.
- **`HANDOVER_NOTIFY_PHONE` preenchido mas irreconhecível** (`"ramal 42"`) →
  também derruba. Era o caso pior: passa em qualquer checagem de "está
  configurado" e só morre dentro do `except` do envio.
- **`OUTBOUND_MODE=mock`** → isento, igual aos canais.

---

## Divergências de paridade com o n8n — decisão de deploy

Duas configurações do harness **não** batem com o que a evidência registra do
n8n. As duas são variáveis de ambiente, e as duas precisam de decisão
explícita antes do cutover.

| Item | n8n (evidência) | Default do harness | Onde decidir |
|---|---|---|---|
| Modelo | `x-ai/grok-4.3` | `x-ai/grok-4.1-fast` | `OPENROUTER_MODEL` |
| Janela de contexto | 12 **mensagens** | `TRIM_KEEP_TURNS=5` (dev) / `SUMMARIZE_KEEP_MESSAGES=10` (prod) | `CONTEXT_STRATEGY` + a variável da estratégia |

**Modelo.** O default do harness é compartilhado com `illumi_assistant` e
`rhawk_assistant` — por isso não foi trocado no `config.py`. Um deploy que
atende leads da EleveC precisa setar `OPENROUTER_MODEL=x-ai/grok-4.3`
explicitamente; sem isso a Renata roda num modelo diferente daquele em que o
SOP foi validado em produção.

**Janela.** As unidades nem são as mesmas: `TRIM_KEEP_TURNS` conta turnos e
`SUMMARIZE_KEEP_MESSAGES` conta mensagens. Os dois defaults guardam menos
histórico que as 12 mensagens do n8n, e num SOP de 8 fases em que e-mail
(Fase 6) e faturamento (Fase 7) chegam em turnos distintos, isso é a
diferença entre lembrar e reperguntar.

Os dois `.env.*.example` carregam o mesmo aviso, no bloco da variável.

---

## Rodar o SOP inteiro (roteiro de conversa)

`tests/integration/test_roteiro_sop.py` percorre saudação → diagnóstico →
aprofundamento → turno → horário → e-mail → faturamento → agendamento, e
também o caminho de desqualificação C1.

```bash
# Encanamento, sem chave: tools reais, portões, banco, balões.
uv run pytest tests/integration/test_roteiro_sop.py -v -s

# O modelo de verdade — o único que responde se a Renata segue o SOP.
OPENROUTER_LIVE_TESTS=1 uv run pytest tests/integration/test_roteiro_sop.py -v -s
# ou: make test-roteiro
```

Nos dois modos, **Google Calendar, Pipedrive e o canal de saída são dublês**.
O único recurso real é o Postgres local, e o lead de teste é apagado no
teardown. Nenhuma escrita chega a serviço externo.

---

## Invariante de telefone (contrato para o importador da Fase 4)

Desde a migração `db/migrations/014_uma_linha_por_pessoa.sql`,
`leads_crm.phone` é **sempre** a forma canônica (só dígitos, brasileiro sem
o 9º dígito) — garantido pelo banco, não por convenção de quem escreve. O
`CHECK leads_crm_phone_canonico_check` proíbe as duas formas físicas que
causavam duplicata de identidade (mesma pessoa, duas linhas): o 9º dígito
do celular (`^55[0-9]{2}9[0-9]{8}$`) e o zero de tronco (`^550`).

**Consequência para o importador do Supabase:** ele precisa canonicalizar
(`shared/phone.py::canonicalizar`) **antes** de inserir, não depois. Uma
violação do CHECK numa carga em massa falha alto (a linha inteira é
recusada pelo Postgres) em vez de criar silenciosamente uma segunda linha
para o mesmo lead — esse é o comportamento desejado. Não trate uma
`CheckViolation` aqui como bug do importador para contornar; é o banco
recusando um telefone que chegou sem canonicalizar.

---

## Ver também

- `docs/evidencias/prompt-renata-n8n.md` — o prompt de produção, verbatim
- `docs/DATABASE.md` — `leads_crm`, o enum `lead_phase`, o gate de ingestão
- `docs/EVOLUTION.md` — o canal por onde a Renata fala
- `docs/ADDING_AGENTS.md` — criar outro agente no catálogo
