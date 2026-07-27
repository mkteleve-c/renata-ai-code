# Fase 2 — O agente Renata: Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Colocar a Renata — a assistente de pré-vendas da EleveC — rodando no harness: prompt portado do n8n, resposta em múltiplos balões, e as tools de agenda, CRM e handover em Python nativo.

**Architecture:** Um agente no catálogo (`elevec_sdr`) construído com `create_agent`, com um middleware que injeta o contexto do lead a cada turno. A saída sai como JSON de balões, parseada do texto final e enviada em N mensagens. As tools falam direto com Google Calendar e Pipedrive — sem n8n.

**Tech Stack:** Python 3.11+, LangGraph 1.0 / LangChain 1.x (`create_agent`), OpenRouter (`x-ai/grok-4.3`), Google Calendar API v3, Pipedrive API v1, pytest (`asyncio_mode=auto`), `uv`.

## Global Constraints

- **Gerenciador de dependências é `uv`.** `uv add`, nunca `pip install`.
- **Migração já aplicada é IMUTÁVEL.** O runner pula pelo nome do arquivo e não guarda checksum. As migrações `001` a `012` já rodaram. Schema novo? Arquivo novo.
- **Banco local na porta 5440**, já de pé. Não rodar `make up`.
- **Nunca `make check` como gate** — há erros de lint pré-existentes em `stress/locustfile.py` e `config.py` que não são desta fase. Use `uv run ruff check`, `uv run ruff format --check` e `uv run pyright` escopados aos arquivos tocados.
- **Nenhum segredo em arquivo versionado.** Placeholders em `.env.example`, valores só no `.env`.
- **Duas representações de telefone**, nunca misturar: canônico (só dígitos, BR sem o 9) em `leads_crm`; E.164 com `+` em `message_queue.phone_number` e no `thread_id`. Conversão por `to_e164()` / `from_e164()`.
- Português brasileiro no código e nos commits; Conventional Commits.
- Todo commit termina com: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Nunca `git add -A` nem `git add .`
- **Paridade é o critério.** Onde o n8n e o harness divergirem, o n8n ganha — salvo decisão explícita registrada no relatório.

---

## Quem a Renata atende — e por que isso muda o portão de faturamento

Levantado depois da primeira versão deste plano, com o workflow `YAY FORMS` na
mão. **A Renata não é a porta de entrada do funil.**

O formulário classifica por faturamento declarado e roteia: faixa acima de R$ 8
mil vai direto para closer humano (Silvio ou Ivana, com rodízio), faixa abaixo
de R$ 5 mil é descartada, e **só a faixa de R$ 5 a 8 mil que não agendou sozinha
recebe o template e cai na conversa com a IA**.

Isso reinterpreta a Fase 7 do SOP. O prompt exige o faturamento antes de
agendar, mas **não define nenhum limiar** — e os critérios de desqualificação
(C1 recolocação, C2 fora de escopo) não mencionam faturamento. Um portão
obrigatório sem critério de aceite significa que o critério mora antes: no
formulário.

**Portanto o portão é confirmação, não qualificação.** O lead já declarou a
faixa; a Renata verifica na conversa. A implementação não muda — `calendar_agendar`
segue recusando sem `email` e sem `faturamento_mensal` —, mas a razão muda, e a
razão importa para quem for mexer nisso depois.

> **A entrada é `sendTemplate`, não `sendText`.** É o template
> `boas_vindas_renata_*` que abre a janela de 24h e puxa o lead para a conversa.
> Isso é **Fase 3**, junto com o follow-up, que sofre da mesma restrição — o
> degrau de 23 horas encosta no limite da janela. Esta fase testa com
> `OUTBOUND_MODE=mock` e não depende disso.

## Contexto herdado da Fase 1

O canal Evolution está pronto e revisado. O que já existe e esta fase consome:

| Peça | Onde |
|---|---|
| Gate de ingestão, consolidação de duplicatas | `shared/leads.py::aplicar_gate` |
| Canonicalização de telefone, recusa de `@lid` | `shared/phone.py` |
| `leads_crm` com `email`, `faturamento_mensal`, `google_event_id` | migração `007` |
| Fila com dedupe por `(canal, agente, telefone, id)` | `shared/queue.py`, migrações `008`–`012` |
| Cliente outbound (`sendText`) e download de mídia | `worker/evolution_client.py`, `worker/media.py` |

**Três coisas que a Fase 1 deixou explicitamente para cá:**

1. **O marcador `[figurinha]`.** O webhook converte sticker nesse texto. Precisa
   aparecer no prompt, senão o agente improvisa em cima de um símbolo que
   ninguém lhe explicou.
2. **A resposta ao template chega como `messageType: "buttonMessage"`**, com o
   texto em `message.conversation`. Verificado com payload de produção — o
   webhook trata certo, porque lê pela forma do conteúdo e não pelo tipo. Vale
   saber ao depurar: é o primeiro turno de toda conversa que vem do formulário.
3. **Mídia baixa por `GET` com `Bearer`**, não por endpoint de base64 — a
   integração `WHATSAPP-BUSINESS` não entrega mídia cifrada. Áudio chega como
   `audio/ogg; codecs=opus` com `ptt: true`, imagem como `image/jpeg`, e o MIME
   vem em `mime_type` (com underscore).

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `agents/catalog/elevec_sdr/prompts.py` | **Criar.** O SOP portado do n8n, verbatim. |
| `agents/catalog/elevec_sdr/agent.py` | **Criar.** `build_graph` com tools e middleware. |
| `agents/catalog/elevec_sdr/graph.py` | **Criar.** Factory para `langgraph dev`. |
| `agents/catalog/elevec_sdr/contexto.py` | **Criar.** Middleware que injeta os dados do lead por turno. |
| `agents/catalog/elevec_sdr/saida.py` | **Criar.** Parser dos balões, com fallback. |
| `agents/catalog/elevec_sdr/tools/agenda.py` | **Criar.** As 5 operações de calendário. |
| `agents/catalog/elevec_sdr/tools/crm.py` | **Criar.** `update_crm`: fase + Pipedrive. |
| `agents/catalog/elevec_sdr/tools/handover.py` | **Criar.** Desliga agente, notifica humano. |
| `shared/google_calendar.py` | **Criar.** Cliente HTTP: OAuth por refresh token, CRUD de evento. |
| `shared/pipedrive.py` | **Criar.** Cliente HTTP: mover card de estágio. |
| `worker/processor.py` | **Modificar.** Enviar N balões em vez de um. |
| `shared/config.py` | **Modificar.** Settings de Google, Pipedrive e handover. |
| `langgraph.json` | **Modificar.** Registrar `elevec_sdr`. |

**Fora desta fase:** follow-up e webhook do ChatWoot (Fase 3), migração dos 3.319 leads (Fase 4), cutover (Fase 5).

---

### Task 1: Prompt e agente base

Sem tools ainda. Ao final, a Renata conversa — responde texto puro seguindo o SOP.

**Files:**
- Create: `src/whatsapp_langchain/agents/catalog/elevec_sdr/{__init__,prompts,agent,graph}.py`
- Modify: `langgraph.json`
- Test: `tests/unit/test_elevec_prompt.py`

**Interfaces:**
- Produces: `build_graph(checkpointer=None, store=None)`; `SYSTEM_PROMPT: str`; grafo registrado como `elevec_sdr`.

- [ ] **Step 1: Extrair o prompt do n8n, verbatim**

O prompt vive no nó `AI Agent` do workflow `i5CHQ5VgzrA65kuK`. Extraia com:

```bash
SP="/private/tmp/.../scratchpad"   # o caminho é impresso pelo controlador
python3 -c "
import json
d=json.load(open('$SP/n8n/i5CHQ5VgzrA65kuK.json'))
n=next(x for x in d['nodes'] if x['name']=='AI Agent')
print(n['parameters']['options']['systemMessage'])
"
```

Se o arquivo não existir, o controlador tem o texto — peça. **Não reescreva o prompt.** Ele está em produção e é o ativo mais valioso desta migração.

- [ ] **Step 2: Escrever o teste que falha**

`tests/unit/test_elevec_prompt.py` — o prompt é dado, não código, então o teste protege o que não pode sumir numa edição futura:

```python
"""O prompt da Renata carrega regras que não podem sumir numa edição."""

import pytest

from whatsapp_langchain.agents.catalog.elevec_sdr.prompts import SYSTEM_PROMPT


@pytest.mark.parametrize(
    "trecho",
    [
        "Renata",
        "EleveC",
        "Silvio Hirata",
        "Consultoria de Alavancagem de Carreira",
        "faturamento",
        "human_handover",
        "update_crm",
        "calendar_get_many",
        "calendar_agendar",
        "[figurinha]",
    ],
)
def test_prompt_mantem_ancoras(trecho):
    assert trecho in SYSTEM_PROMPT


def test_prompt_tem_as_oito_fases_do_sop():
    for n in range(1, 9):
        assert f"\n{n}." in SYSTEM_PROMPT, f"fase {n} sumiu do SOP"


def test_prompt_exige_email_e_faturamento_antes_de_agendar():
    assert "INVIOLÁVEL" in SYSTEM_PROMPT
    assert "TERMINANTEMENTE PROIBIDO" in SYSTEM_PROMPT


def test_prompt_tem_placeholders_de_contexto():
    for campo in ("{nome}", "{origem}", "{telefone}", "{data_hoje}"):
        assert campo in SYSTEM_PROMPT
```

- [ ] **Step 3: Rodar e confirmar a falha**

Run: `uv run pytest tests/unit/test_elevec_prompt.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Criar `prompts.py`**

Cole o prompt extraído como `SYSTEM_PROMPT`, com **duas** mudanças e nenhuma a mais:

1. As expressões n8n (`{{ $('Fields').item.json.pushName }}` etc.) viram placeholders `{nome}`, `{origem}`, `{telefone}`, `{data_hoje}`.
2. Acrescente ao bloco de regras de formatação:

```
- Quando a mensagem do lead vier como `[figurinha]`, ele mandou uma figurinha
  (sticker), não texto. Trate como uma reação positiva breve — reconheça e siga
  a conversa do ponto em que estava. Não peça para ele mandar texto.
```

- [ ] **Step 5: Criar `agent.py` e `graph.py`**

Espelhe `agents/catalog/illumi_assistant/agent.py`, que é o padrão do repositório. Diferenças desta fase:
- `tools=[]` por enquanto (as tools entram nas tasks 4–6)
- middleware de contexto do harness (`get_context_middleware()`) — o de lead entra na Task 3
- **não** habilite as tools de memória (`save_memory`/`read_memory`): a paridade com o n8n exige `MEMORY_ENABLED=false`

`graph.py` é cópia estrutural do `illumi_assistant/graph.py`, trocando o import.

- [ ] **Step 6: Registrar em `langgraph.json`**

```json
"elevec_sdr": "./src/whatsapp_langchain/agents/catalog/elevec_sdr/graph.py:graph"
```

- [ ] **Step 7: Rodar os testes**

Run: `uv run pytest tests/unit/test_elevec_prompt.py tests/unit/test_loader.py -v`
Expected: PASS. O `test_loader.py` prova que o agente novo é descoberto pelo catálogo.

- [ ] **Step 8: Commit**

```bash
git add src/whatsapp_langchain/agents/catalog/elevec_sdr/ langgraph.json tests/unit/test_elevec_prompt.py
git commit -m "feat: agente elevec_sdr com o prompt da Renata portado do n8n

O SOP de 8 fases, os critérios de desqualificação e os portões de e-mail e
faturamento vêm verbatim do nó AI Agent. As únicas mudanças são os
placeholders de contexto e a instrução sobre o marcador [figurinha], que a
Fase 1 introduziu e nenhum prompt explicava.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Saída em balões

O n8n devolve `{"messages": [...]}` e envia cada item como um balão separado. Hoje o processor manda uma mensagem só.

**Files:**
- Create: `src/whatsapp_langchain/agents/catalog/elevec_sdr/saida.py`
- Modify: `src/whatsapp_langchain/worker/processor.py`
- Test: `tests/unit/test_saida_baloes.py`

**Interfaces:**
- Produces: `extrair_baloes(texto: str) -> list[str]` — sempre devolve ao menos um item.

- [ ] **Step 1: Escrever os testes que falham**

```python
"""Parser da saída em balões da Renata."""

from whatsapp_langchain.agents.catalog.elevec_sdr.saida import extrair_baloes


def test_json_valido_vira_lista():
    assert extrair_baloes('{"messages": ["oi", "tudo bem?"]}') == ["oi", "tudo bem?"]


def test_json_dentro_de_cerca_markdown():
    bruto = '```json\n{"messages": ["oi"]}\n```'
    assert extrair_baloes(bruto) == ["oi"]


def test_texto_solto_vira_balao_unico():
    assert extrair_baloes("desculpa, tive um problema") == ["desculpa, tive um problema"]


def test_json_sem_a_chave_messages_vira_balao_unico():
    bruto = '{"resposta": "oi"}'
    assert extrair_baloes(bruto) == [bruto]


def test_lista_vazia_nao_devolve_nada_vazio():
    assert extrair_baloes('{"messages": []}') == ['{"messages": []}']


def test_itens_nao_string_sao_descartados():
    assert extrair_baloes('{"messages": ["oi", 42, null, "tchau"]}') == ["oi", "tchau"]


def test_espaco_em_branco_nao_vira_balao():
    assert extrair_baloes('{"messages": ["oi", "   ", "tchau"]}') == ["oi", "tchau"]
```

- [ ] **Step 2: Rodar e confirmar a falha**

Run: `uv run pytest tests/unit/test_saida_baloes.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implementar `saida.py`**

```python
"""Converte a resposta final do agente em balões de WhatsApp.

O n8n usa um output parser estruturado sobre o TEXTO FINAL, depois que o ciclo
de tools terminou — não `response_format` nativo. Replicamos o mesmo mecanismo:
estruturar a resposta e chamar ferramentas no mesmo turno faz o modelo devolver
o JSON em vez de chamar a tool, ou quebrar o schema quando há tool call pendente.

Nunca devolve lista vazia: perder a resposta é pior que perder a formatação.
"""

import json
import re

CERCA = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def extrair_baloes(texto: str) -> list[str]:
    bruto = (texto or "").strip()
    if not bruto:
        return [""]

    candidato = bruto
    if cerca := CERCA.search(bruto):
        candidato = cerca.group(1).strip()

    try:
        dados = json.loads(candidato)
    except (ValueError, TypeError):
        return [bruto]

    if not isinstance(dados, dict):
        return [bruto]

    mensagens = dados.get("messages")
    if not isinstance(mensagens, list):
        return [bruto]

    baloes = [m.strip() for m in mensagens if isinstance(m, str) and m.strip()]
    return baloes or [bruto]
```

- [ ] **Step 4: Ligar no processor**

Em `worker/processor.py`, onde hoje está `response_text = result["messages"][-1].content` seguido de um `_send_message`:

- parseie com `extrair_baloes` **apenas quando `message.agent_id == "elevec_sdr"`** — os outros agentes do catálogo não devolvem JSON e não podem mudar de comportamento
- envie os balões em sequência, com um `delay_ms` entre eles
- `mark_done` só depois do último envio confirmado
- se um balão falhar no meio, **não** reenvie os já entregues: registre no log qual índice falhou e deixe a exceção subir para o retry existente

Documente essa decisão no relatório: o retry atual reenvia tudo, então uma falha no meio duplica os primeiros balões. É comportamento herdado, comum aos quatro canais, e a Fase 1 já o registrou como backlog.

- [ ] **Step 5: Rodar os testes**

Run: `uv run pytest tests/unit/test_saida_baloes.py tests/unit/test_processor_twilio.py tests/unit/test_processor_evolution.py -v`
Expected: PASS — nenhum agente existente pode mudar de comportamento.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/agents/catalog/elevec_sdr/saida.py \
        src/whatsapp_langchain/worker/processor.py tests/unit/test_saida_baloes.py
git commit -m "feat: resposta da Renata sai em múltiplos balões

Parseia o JSON do texto final, como o outputParserStructured do n8n faz —
não response_format nativo, que conflita com tool calling no mesmo turno.

Nunca devolve lista vazia: perder a resposta é pior que perder a formatação.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Contexto do lead por turno

O prompt tem `{nome}`, `{origem}`, `{telefone}` e `{data_hoje}`. `create_agent` recebe o `system_prompt` na construção, mas `load_graph` é chamado **por mensagem** (`worker/processor.py`), então o grafo é construído a cada turno — dá para interpolar.

**Files:**
- Create: `src/whatsapp_langchain/agents/catalog/elevec_sdr/contexto.py`
- Modify: `agents/catalog/elevec_sdr/agent.py`
- Test: `tests/integration/test_elevec_contexto.py`

**Interfaces:**
- Consumes: `shared/leads.py`, `shared/phone.py::from_e164`
- Produces: `async carregar_contexto(pool, phone_e164) -> dict[str, str]`; middleware `@before_model` que injeta o contexto.

- [ ] **Step 1: Decidir o mecanismo — leia antes de escrever**

Duas rotas possíveis, e a escolha muda o desenho:

| Rota | A favor | Contra |
|---|---|---|
| Interpolar no `system_prompt` na construção do grafo | idêntico ao n8n; o modelo vê o contexto como instrução | `build_graph` precisa receber o telefone, mudando a assinatura que o `loader` usa para todos os agentes |
| Middleware `@before_model` que injeta uma mensagem de contexto | não mexe na assinatura do loader; segue o padrão de `middleware/trim.py` | o contexto vira mensagem, não instrução — o modelo pode tratá-la como fala do usuário |

**Escolha a segunda** e mitigue o contra: injete como `SystemMessage` efêmera (não persistida no checkpointer), não como `HumanMessage`. Confirme lendo `agents/middleware/trim.py` como o `@before_model` devolve estado.

Se ao implementar descobrir que a segunda rota não sustenta o comportamento, **pare e reporte** — mudar a assinatura do loader afeta os outros dois agentes do catálogo e é decisão do controlador.

- [ ] **Step 2: Escrever o teste que falha**

```python
"""O contexto do lead chega ao agente a cada turno."""

import pytest

from whatsapp_langchain.agents.catalog.elevec_sdr.contexto import carregar_contexto
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "551155554444"


@pytest.fixture
async def lead():
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute(
            "insert into leads_crm (phone, name, source, phase) "
            "values (%s, 'Fulano de Tal', 'linkedin_form', 'iniciou_conversa')",
            (TELEFONE,),
        )
    yield
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))


async def test_contexto_traz_nome_e_origem(lead):
    pool = await get_pool()
    ctx = await carregar_contexto(pool, f"+{TELEFONE}")

    assert ctx["nome"] == "Fulano de Tal"
    assert ctx["origem"] == "linkedin_form"
    assert ctx["telefone"] == TELEFONE


async def test_data_de_hoje_vem_no_fuso_de_sao_paulo(lead):
    pool = await get_pool()
    ctx = await carregar_contexto(pool, f"+{TELEFONE}")

    # dd/MM/yyyy HH:mm:ss — mesmo formato do n8n
    assert len(ctx["data_hoje"].split("/")) == 3


async def test_lead_inexistente_nao_quebra():
    pool = await get_pool()
    ctx = await carregar_contexto(pool, "+551100000000")

    assert ctx["nome"] == ""
    assert ctx["telefone"] == "551100000000"
```

- [ ] **Step 3: Rodar e confirmar a falha**

Run: `uv run pytest tests/integration/test_elevec_contexto.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Implementar e ligar no `agent.py`**

`carregar_contexto` faz uma consulta por turno em `leads_crm`. O fuso é `America/Sao_Paulo` e o formato de data é `dd/MM/yyyy HH:mm:ss`, igual ao `$now.setZone(...)` do n8n. Lead inexistente devolve strings vazias — nunca levanta.

- [ ] **Step 5: Rodar os testes e commitar**

```bash
uv run pytest tests/integration/test_elevec_contexto.py tests/unit -q
git add src/whatsapp_langchain/agents/catalog/elevec_sdr/ tests/integration/test_elevec_contexto.py
git commit -m "feat: contexto do lead injetado a cada turno da Renata

Nome, origem, telefone e data de hoje chegam ao agente por middleware
before_model, sem mudar a assinatura do loader — que é compartilhada com os
outros agentes do catálogo.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Cliente do Google Calendar

**Files:**
- Create: `src/whatsapp_langchain/shared/google_calendar.py`
- Modify: `src/whatsapp_langchain/shared/config.py`
- Test: `tests/unit/test_google_calendar.py`

**Interfaces:**
- Produces: `GoogleCalendarClient(client_id, client_secret, refresh_token, calendar_id)` com `listar_eventos(inicio, fim)`, `criar_evento(...)`, `atualizar_evento(...)`, `deletar_evento(...)`, `obter_evento(...)`. `GoogleCalendarError`.

- [ ] **Step 1: Settings**

Em `shared/config.py`, junto aos outros blocos de canal:

```python
    # --- Google Calendar (agenda de agendamento do SDR) ---
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = ""
```

- [ ] **Step 2: Escrever os testes que falham**

Use `httpx.MockTransport`, como `tests/unit/test_evolution_client.py` faz. Cubra:
- renovação de access token a partir do refresh token, e reuso enquanto válido
- `listar_eventos` montando `timeMin`/`timeMax` em ISO 8601 com offset `-03:00`
- `criar_evento` enviando `attendees` e `summary`
- erro HTTP virando `GoogleCalendarError` com status e detalhe
- resposta 200 com corpo inesperado **não** levantando exceção crua

> A última é a lição que a Fase 1 pagou caro: `response.json()["campo"]` sem
> blindagem transforma um 200 estranho em `KeyError` sem log.

- [ ] **Step 3: Implementar**

Autenticação: `POST https://oauth2.googleapis.com/token` com `grant_type=refresh_token`. O access token dura ~3600s — guarde com margem e renove sozinho.

Operações sobre `https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events`.

**As credenciais já foram validadas** pelo controlador: o refresh token renova e tem `owner` em `silvio.hirata@eleve-c.co`.

- [ ] **Step 4: Verificar contra a API real**

Um `listar_eventos` numa janela curta é leitura pura e seguro de rodar. **Não** crie, atualize nem delete evento na agenda real — ela é de trabalho de uma pessoa. Cole a saída no relatório.

- [ ] **Step 5: Rodar os testes e commitar**

---

### Task 5: Tools de agenda

**Files:**
- Create: `src/whatsapp_langchain/agents/catalog/elevec_sdr/tools/{__init__,agenda}.py`
- Modify: `agents/catalog/elevec_sdr/agent.py`
- Test: `tests/unit/test_tool_agenda.py`

**A política de disponibilidade, extraída do código do workflow `#3 Agenda`:**

| Regra | Valor |
|---|---|
| Agenda | `silvio.hirata@eleve-c.co` |
| Timezone | `America/Sao_Paulo` (-03:00) |
| Dias permitidos | segunda a sexta |
| Manhã / Tarde / Noite | `8,9,10,11` / `13,14,15,16,17` / `18,19,20,21` |
| Duração | 60 min, horas cheias |
| Janela | 4 dias a partir de **D+1** — nunca hoje |
| Título | `Consultoria de Alavancagem de Carreira - {Nome}` |
| Saída de disponibilidade | `quinta 12/02: 13, 14, 16, 17` |

> O código original calcula 4 dias mas busca eventos de 7. Padronizamos em **4** — a busca maior era desperdício, não regra.

- [ ] **Step 1: Escrever os testes que falham**

Cubra, com o cliente mockado:
- `calendar_get_many` devolvendo só horas livres, no formato exato
- fim de semana nunca aparecendo
- **hoje nunca aparecendo** — a janela começa em D+1
- período inválido devolvendo mensagem de erro, não exceção
- no máximo 2 dias e 2 horários por dia (regra de escassez do prompt)
- **`calendar_agendar` recusando quando o lead não tem `email` ou `faturamento_mensal`**

- [ ] **Step 2: Implementar as tools**

Cinco tools com `@tool`, descrições em português derivadas das do n8n.

**A validação dos portões vai em código, não só no prompt.** `calendar_agendar` lê o lead e recusa se `email` ou `faturamento_mensal` estiverem vazios, devolvendo ao agente uma mensagem que o oriente a voltar à fase 6 ou 7. O prompt chama isso de "sequência INVIOLÁVEL" — um `if` garante o que três parágrafos pedem.

Ao agendar com sucesso, grave `google_event_id` no lead: torna reagendamento e cancelamento determinísticos, sem varrer o calendário.

- [ ] **Step 3: Rodar os testes e commitar**

---

### Task 6: Tools de CRM e handover

**Files:**
- Create: `agents/catalog/elevec_sdr/tools/{crm,handover}.py`, `shared/pipedrive.py`
- Modify: `shared/config.py`, `agents/catalog/elevec_sdr/agent.py`
- Test: `tests/unit/test_pipedrive.py`, `tests/integration/test_tool_crm.py`

**Valores extraídos do workflow `#4 CRM Control`:**

| Fase | `stage_id` no Pipedrive |
|---|---|
| `qualificado` | **12** |
| `agendou_sessao` | **13** |
| `desqualificado` | não move card, só atualiza o banco |

- [ ] **Step 1: Settings**

```python
    # --- Pipedrive (funil comercial) ---
    pipedrive_api_token: str = ""
    pipedrive_stage_qualificado: int = 12
    pipedrive_stage_agendado: int = 13

    # --- Handover ---
    handover_notify_phone: str = ""
```

- [ ] **Step 2: Escrever os testes que falham**

`update_crm`:
- muda a fase em `leads_crm`
- `agendou_sessao` e `desqualificado` desligam `followup_active`
- move o card só quando há `pipedriveid`
- **não** reescreve a mesma fase (regra explícita do prompt)

`human_handover`:
- zera `agent_active` e `followup_active`
- envia WhatsApp para `handover_notify_phone` com o motivo e `wa.me/{telefone}`
- **não** levanta se o envio falhar — o desligamento do agente é o que importa

- [ ] **Step 3: Implementar e commitar**

---

### Task 7: Ponta a ponta e documentação

**Files:**
- Create: `docs/AGENTE_ELEVEC.md`
- Modify: `.env.example`, `deploy/.env.prod.example`, `CLAUDE.md`

- [ ] **Step 1: Teste ponta a ponta com o LLM real**

Suba a API local, mande uma mensagem pelo webhook da Evolution com `?agent=elevec_sdr` e acompanhe: o lead é criado, a fila recebe, o worker invoca a Renata, e a resposta sai em balões.

**Use `OUTBOUND_MODE=mock`** — o envio fica no log, sem entregar WhatsApp a ninguém. Confira no log que os balões saíram separados.

- [ ] **Step 2: Roteiro de conversa**

Percorra o SOP inteiro simulando um lead: saudação → diagnóstico → aprofundamento → turno → horário → e-mail → faturamento → agendamento. Verifique que o agente **não agenda** antes de ter e-mail e faturamento, e cole o transcrito no relatório.

Faça também o caminho de desqualificação (C1: "quero uma vaga") e confirme que ele encerra educadamente sem agendar.

- [ ] **Step 3: Documentar**

`docs/AGENTE_ELEVEC.md`: as 8 fases, os critérios de desqualificação, as tools, a política de agenda, os portões validados em código, e as variáveis novas.

Em `CLAUDE.md`, acrescente `elevec_sdr` à lista do catálogo na convenção 12.

- [ ] **Step 4: Commit final da fase**

---

## Definição de pronto da Fase 2

- [ ] A Renata responde seguindo o SOP, em múltiplos balões
- [ ] O contexto do lead (nome, origem, data) chega a cada turno
- [ ] `calendar_get_many` devolve só horários livres, dentro da política, nunca hoje
- [ ] `calendar_agendar` **recusa** sem e-mail ou faturamento — verificado por teste
- [ ] `google_event_id` gravado ao agendar
- [ ] `update_crm` muda a fase e move o card nos estágios 12 e 13
- [ ] `human_handover` desliga o agente e avisa o humano
- [ ] Nenhum teste da Fase 1 quebrou

## O que vem depois

| Fase | Conteúdo | Ajuste desde o desenho original |
|---|---|---|
| 3 | Follow-up com reivindicação atômica, webhook do ChatWoot | **+ `sendTemplate` no `EvolutionClient`** e consciência da janela de 24h: o degrau de 23 horas encosta no limite, e texto livre é rejeitado pela Meta se a janela fechou |
| 4 | `migrar_supabase.py`: normalização, fusão de duplicatas, histórico | **escopo possivelmente menor**: se a IA só atende a faixa de R$ 5 a 8 mil sem agendamento, os 2.559 leads em `formulario_preenchido` não são todos dela — conferir a distribuição antes de migrar |
| 5 | Cutover e deploy no Railway | — |
