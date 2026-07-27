# Fase 3 — Periféricos do `elevec_sdr`: follow-up, template e ChatWoot

> **Para agentes:** SUB-SKILL OBRIGATÓRIA: use `superpowers:subagent-driven-development`
> para implementar task a task. Os passos usam checkbox (`- [ ]`).

**Goal:** Dar à Renata as três peças que faltam para operar sozinha — a régua de
follow-up com reivindicação atômica, o `sendTemplate` que abre a janela de 24h, e
o webhook do ChatWoot que deixa um humano pausá-la.

**Architecture:** O follow-up é uma task asyncio no Worker, ao lado do loop de
consumo. Ela reivindica leads com um único `UPDATE ... RETURNING` e só então
envia, fora de qualquer transação. O ChatWoot entra como mais uma rota de webhook
na API. O `sendTemplate` é um método novo no `EvolutionClient`.

**Tech Stack:** psycopg async pool, asyncio, FastAPI, httpx.

> **Revisão:** este plano passou por uma rodada adversarial que encontrou 5
> Críticos e 9 Importantes. Todos estão incorporados. As seções "O relógio da
> escada" e "Task 5" mudaram de desenho por causa dela.

---

## Global Constraints

- Python 3.11+, deps por `uv` — nunca `pip install`.
- Português brasileiro no código, nos logs e nos commits (Conventional Commits).
- Banco de desenvolvimento na **porta 5440**. Não rodar `make up`.
- **Não usar `make check`** (37 `E501` pré-existentes em `prompts.py`). Rodar
  `ruff check`, `ruff format --check` e `pyright` só nos arquivos tocados.
- **Não fazer `git push`.** O orquestrador empurra ao fim da fase.
- Nunca `git add -A` nem `git add .` — sempre caminhos explícitos.
- Nenhum segredo versionado.
- Migrações **nunca são editadas depois de aplicadas** — o runner pula por nome
  de arquivo. Correção vira migração nova.
- Camada de banco **testada contra o Postgres real**, nunca monkeypatchada. Este
  projeto reprovou três tasks por isso.
- **Mock de HTTP no `EvolutionClient` é `_transport`, não `_client`.** O cliente
  guarda `self._transport` e cria um `AsyncClient` por chamada. O padrão é
  `monkeypatch.setattr(cliente, "_transport", httpx.MockTransport(handler))` —
  veja `tests/unit/test_evolution_client.py:35`. Usar `_client` faz o teste bater
  na rede de verdade e passar vacuamente.
- Suíte de partida: **666 passed, 13 skipped, 11 deselected** (Task 2 já
  concluída, commit `99c43da`).

---

## O relógio da escada — o que mudou e por quê

A especificação descreve três degraus (15 min, 1 hora, 23 horas) contados a partir
de `last_interaction_at`, que **o próprio envio atualiza**. Isso os torna
cumulativos.

**Medido no banco legado, 1.067 leads que entraram na régua:**

| `followup_count` | leads | mediana desde `created_at` | levaram ≥ 20h |
|---|---|---|---|
| 1 | 5 | 0h18 | 0 |
| 2 | 11 | 1h25 | 0 |
| 3 | **1.051** | **24h28** | **1.051 de 1.051** |

A escada é cumulativa em produção — confirmado, não presumido. E o terceiro
degrau cai em ~24h28 depois da criação do lead.

A integração é a **Cloud API oficial**: texto livre só alcança quem escreveu nas
últimas 24 horas, contadas do último inbound **do lead**. Mensagem de negócio não
abre nem estende a janela. Um degrau que dispara em 24h28 chega depois de a janela
ter fechado.

**A correção é o relógio, não o valor.** Ancorando os três degraus em
`last_inbound_at` — o instante em que o lead falou —, o cronograma vira absoluto:

| Degrau | Dispara em | Corte da janela (24h − 30 min) | |
|---|---|---|---|
| 1 | inbound + 0h15 | 23h30 | entrega |
| 2 | inbound + 1h15 | 23h30 | entrega |
| 3 | **inbound + 23h00** | 23h30 | entrega, com 55 min de folga real |

Sem acumulação, portanto **sem deriva do polling** — os até 5 minutos de atraso de
cada rodada não se somam, porque cada degrau tem hora marcada em vez de contar do
anterior. E **os 23 horas da especificação são preservados**: não há divergência de
valor. `followup_count` já diz em que degrau o lead está; `last_interaction_at`
deixa de participar da decisão e fica só como registro de "quando encostamos nele
pela última vez".

> Uma versão anterior deste plano propunha mudar o degrau 3 para 22 horas. Era
> remendo no sintoma: com o relógio certo, 23 horas funciona.

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `db/migrations/013_last_inbound_at.sql` | coluna `last_inbound_at` + backfill conservador + índice |
| `src/whatsapp_langchain/shared/leads.py` (modificar) | grava `last_inbound_at` em **todo** inbound real, inclusive o recusado |
| `src/whatsapp_langchain/worker/followup.py` (criar) | reivindicação, escada, revalidação, envio |
| `src/whatsapp_langchain/worker/main.py` (modificar) | sobe a task |
| `src/whatsapp_langchain/worker/processor.py` (modificar) | relê `agent_active` antes de enviar |
| `src/whatsapp_langchain/server/routes/webhook_chatwoot.py` (criar) | pausa/retoma |
| `src/whatsapp_langchain/shared/config.py` (modificar) | variáveis novas |
| `tests/integration/conftest_leads.py` (criar) | fixture `lead_factory` compartilhada |
| `tests/integration/test_followup.py` (criar) | escada, concorrência real, janela, revalidação |
| `tests/integration/test_webhook_chatwoot.py` (criar) | rótulo, secret, filtro, ordem |

**Já concluído:** `send_template` no `EvolutionClient` (Task 2, `99c43da`).

---

## Task 0 (BLOQUEANTE): capturar um payload real do ChatWoot

A Fase 1 estabeleceu esta doutrina depois de o plano presumir um campo
(`remoteJidAlt`) que não existia em nenhuma das 50 mensagens reais. A Task 5
depende de três suposições não verificadas, e cada uma faz o recurso falhar **em
silêncio**:

1. **O ChatWoot consegue mandar header customizado?** A integração "Webhooks" do
   produto recebe URL + lista de eventos. Se não houver campo de header, um secret
   em header nunca chega, todo POST vira 401, e a etiqueta para de funcionar sem
   ninguém perceber — exatamente o risco que a especificação lista no cutover.
2. **O telefone vem em `meta.sender.identifier`?** No ChatWoot o campo canônico do
   contato é `phone_number` (E.164 com `+`). `identifier` é o *external id*
   opcional, muitas vezes `null`.
3. **`conversation_updated` traz `changed_attributes`?** É o que distingue "os
   rótulos mudaram" de "mudou outra coisa e `labels` veio junto por acaso".

**Entregável:** `docs/evidencias/payload-chatwoot-real.json`, com pelo menos três
eventos capturados da instância do cliente — um `conversation_updated` com a
etiqueta `pausar_agente` sendo adicionada, um com ela sendo removida, e um
`message_created` qualquer. Redija os dados pessoais (nome, telefone real,
conteúdo), preservando **a forma** de cada campo.

**Como capturar:** aponte o webhook do ChatWoot para um coletor temporário
(`webhook.site`, ou um endpoint de log no ambiente de dev) e faça as três ações na
interface.

**As Tasks 1, 3 e 4 não dependem disto e podem rodar antes.** A Task 5 não começa
sem o arquivo.

---

## Task 1: `last_inbound_at` — o relógio da janela

**Files:**
- Create: `db/migrations/013_last_inbound_at.sql`
- Modify: `src/whatsapp_langchain/shared/leads.py`
- Test: `tests/integration/test_gate_ingestao.py`

**Interfaces:**
- Produces: `leads_crm.last_inbound_at TIMESTAMPTZ` (nullable, **sem `DEFAULT`**),
  escrita apenas pelo gate. Consumida pela Task 3.

### O que o implementador precisa entender

**Por que a coluna é nullable e sem `DEFAULT now()`.** É decisão, não
esquecimento. `NULL` significa "nunca vi este lead falar" e é **seguro por
construção**: `NULL > now() - interval` avalia para `NULL`, que não é `TRUE`, logo
o lead nunca é reivindicado. Com `DEFAULT now()`, toda linha importada nasceria
com janela aberta e receberia follow-up que a Meta rejeita. Escreva isso no
comentário da migração — alguém vai querer "consertar" o default.

**Por que o backfill é conservador.** Para quem tem `followup_count > 0`,
`last_interaction_at` é **provadamente** o nosso envio, não o inbound dele.
Copiar esse valor superestimaria a janela em até 24 horas. Esses ficam `NULL`.

**Por que o gate grava mesmo quando recusa por `agente_desligado`.**
`last_inbound_at` é fato sobre a Meta, não sobre o nosso funil. Hoje o gate
retorna em `leads.py:361-363` **antes** do `UPDATE`; com isso, tudo que o lead
escreve durante um handover humano não move o relógio — e na retomada ele pode
estar inalcançável pela régua embora a janela real esteja aberta. Grave em todo
inbound real do lead. **Não** grave em `fromMe` (somos nós), nem em blocklist, nem
em telefone inválido.

- [ ] **Step 1: Criar a fixture compartilhada**

Os testes de integração deste repo usam `await get_pool()` direto e uma fixture
`limpar` por arquivo (`tests/integration/test_gate_ingestao.py:17`). As Tasks 1, 3
e 5 precisam do mesmo factory de lead — crie uma vez, em
`tests/integration/conftest_leads.py`:

```python
import pytest_asyncio

from whatsapp_langchain.shared.db import get_pool


@pytest_asyncio.fixture
async def lead_factory():
    """Cria leads com relógios controlados. Limpa o que criou no teardown."""
    criados: list[str] = []

    async def _criar(
        phone: str,
        *,
        phase: str = "iniciou_conversa",
        followup_count: int = 0,
        followup_active: bool = True,
        agent_active: bool = True,
        name: str | None = "Fulano",
        minutos_desde_interacao: float = 0,
        minutos_desde_inbound: float | None = None,
    ) -> str:
        """`minutos_desde_inbound=None` deixa last_inbound_at NULL de propósito."""
        pool = await get_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "insert into leads_crm "
                "  (phone, name, phase, followup_count, followup_active, "
                "   agent_active, last_interaction_at, last_inbound_at) "
                "values (%s, %s, %s, %s, %s, %s, "
                "        now() - make_interval(mins => %s), "
                "        case when %s::float is null then null "
                "             else now() - make_interval(mins => %s) end)",
                (phone, name, phase, followup_count, followup_active,
                 agent_active, minutos_desde_interacao,
                 minutos_desde_inbound, minutos_desde_inbound),
            )
            await conn.commit()
        criados.append(phone)
        return phone

    yield _criar

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("delete from leads_crm where phone = any(%s)", (criados,))
        await conn.commit()
```

Registre em `tests/integration/conftest.py` com
`pytest_plugins = ["tests.integration.conftest_leads"]` — ou mova o corpo para o
`conftest.py` existente, se preferir. **Não** deixe cada task inventar o seu.

- [ ] **Step 2: Escrever os testes que falham**

```python
async def test_gate_grava_last_inbound_at(lead_factory):
    await lead_factory("5511987654321", minutos_desde_inbound=180)

    resultado = await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511987654321@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )
    assert resultado.aceito
    assert await _minutos_desde_inbound("5511987654321") < 1


async def test_inbound_de_lead_pausado_ainda_move_a_janela(lead_factory):
    """A janela é fato sobre a Meta, não sobre o nosso funil.

    Sem isto, tudo que o lead escreve durante o handover humano não conta, e na
    retomada ele pode estar inalcançável pela régua com a janela real aberta.
    """
    await lead_factory("5511987654322", agent_active=False, minutos_desde_inbound=180)

    resultado = await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511987654322@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )
    assert not resultado.aceito
    assert resultado.motivo == "agente_desligado"
    assert await _minutos_desde_inbound("5511987654322") < 1


async def test_mensagem_nossa_nao_move_a_janela(lead_factory):
    """fromMe somos nós — não abre janela nenhuma."""
    await lead_factory("5511987654323", minutos_desde_inbound=180)

    await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511987654323@s.whatsapp.net", "fromMe": True},
        push_name="Fulano",
    )
    assert await _minutos_desde_inbound("5511987654323") > 170
```

- [ ] **Step 3: Rodar e ver falhar** — `column "last_inbound_at" does not exist`.

- [ ] **Step 4: Criar a migração**

```sql
-- 013_last_inbound_at.sql
-- Relógio da janela de 24h da Cloud API.
--
-- last_interaction_at mistura "o lead falou" com "nós falamos" — o follow-up
-- também o atualiza, o que torna a escada cumulativa (medido: o degrau 3 caía
-- em ~24h28 desde a criação do lead, fora da janela). Esta coluna registra só
-- o inbound do lead, e é o que ancora os três degraus.
--
-- SEM DEFAULT, e nullable, DE PROPÓSITO. NULL = "nunca vi este lead falar", e
-- é seguro por construção: `NULL > now() - interval` não é TRUE, então o lead
-- não é reivindicado. Com DEFAULT now(), toda linha importada nasceria com
-- janela aberta e receberia follow-up que a Meta rejeita.

ALTER TABLE leads_crm ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;

-- Backfill conservador: só para quem nunca recebeu follow-up, onde
-- last_interaction_at ainda é o inbound do lead. Para followup_count > 0 o
-- valor é provadamente o NOSSO envio — copiá-lo superestimaria a janela em até
-- 24h. Esses ficam NULL e voltam à régua quando falarem de novo.
UPDATE leads_crm SET last_inbound_at = last_interaction_at
WHERE last_inbound_at IS NULL AND followup_count = 0;

DROP INDEX IF EXISTS idx_leads_followup;
CREATE INDEX idx_leads_followup
    ON leads_crm (last_inbound_at)
    INCLUDE (phase, followup_count)
    WHERE followup_active AND agent_active;
```

O índice lidera por `last_inbound_at` porque a reivindicação filtra e ordena por
ele. `phase` e `followup_count` são `INCLUDE`: avaliadas dentro do índice, sem ir
ao heap, mas sem fingir que restringem faixa.

- [ ] **Step 5: Fazer o gate gravar a coluna**

Duas escritas em `leads.py`:
- no upsert do caminho aceito, ao lado de `last_interaction_at = now()`;
- **antes** do `return` de `agente_desligado`, um `UPDATE` só da coluna.

- [ ] **Step 6: Rodar e ver passar**

- [ ] **Step 7: Provar por mutação — mínimo 4**

(a) tirar `last_inbound_at = now()` do upsert; (b) tirar a gravação do caminho
`agente_desligado`; (c) gravar também em `fromMe`; (d) trocar o backfill por
`WHERE last_inbound_at IS NULL` sem a cláusula de `followup_count`.

- [ ] **Step 8: Commit**

```bash
git add db/migrations/013_last_inbound_at.sql \
        src/whatsapp_langchain/shared/leads.py \
        tests/integration/conftest_leads.py tests/integration/conftest.py \
        tests/integration/test_gate_ingestao.py
git commit -m "feat: last_inbound_at, o relogio da janela de 24h"
```

---

## Task 3: Reivindicação atômica e a escada

**Um envio indevido aqui é WhatsApp para gente de verdade.**

**Files:**
- Create: `src/whatsapp_langchain/worker/followup.py`
- Modify: `src/whatsapp_langchain/shared/config.py`
- Test: `tests/integration/test_followup.py`

**Interfaces:**
```python
@dataclass(frozen=True)
class LeadReivindicado:
    phone: str
    name: str | None
    nivel: int          # 1, 2 ou 3 — já é o novo followup_count

async def reivindicar(pool, *, limite: int, n1_min: int, n2_min: int,
                      n3_min: int, janela_min: int) -> list[LeadReivindicado]
async def contar_bloqueados_por_janela(pool, *, n1_min, n2_min, n3_min) -> int
async def ainda_vale_enviar(pool, phone: str, nivel: int) -> bool
def primeiro_nome(name: str | None) -> str | None
def montar_mensagem(nivel: int, name: str | None) -> str
async def reativar_agentes(pool) -> int
async def rodada(pool, cliente, **kwargs) -> dict[str, int]
```

Os degraus entram como **parâmetros com default vindo de `settings`**, não lidos
lá dentro — senão testar o degrau 3 exige mexer em env global.

**Config nova:**

| Variável | Default | Papel |
|---|---|---|
| `FOLLOWUP_ENABLED` | `false` | **desligado por padrão** |
| `FOLLOWUP_INTERVAL_SECONDS` | `300` | intervalo da task |
| `FOLLOWUP_BATCH_SIZE` | `10` | `LIMIT` |
| `FOLLOWUP_NIVEL1_MINUTOS` | `15` | degrau 1 |
| `FOLLOWUP_NIVEL2_MINUTOS` | `75` | degrau 2 — 1h15 **desde o inbound** |
| `FOLLOWUP_NIVEL3_MINUTOS` | `1380` | degrau 3 — 23h desde o inbound |
| `FOLLOWUP_JANELA_MARGEM_MINUTOS` | `30` | folga antes das 24h |

- [ ] **Step 1: Escrever os testes que falham**

```python
async def test_os_tres_degraus_ancoram_no_inbound(lead_factory):
    await lead_factory("5511900000001", followup_count=0, minutos_desde_inbound=10)
    await lead_factory("5511900000002", followup_count=0, minutos_desde_inbound=20)
    await lead_factory("5511900000003", followup_count=1, minutos_desde_inbound=80)
    await lead_factory("5511900000004", followup_count=2, minutos_desde_inbound=23 * 60 + 5)

    por_telefone = {r.phone: r.nivel for r in await _reivindicar()}

    assert "5511900000001" not in por_telefone, "10 min não vence o degrau 1"
    assert por_telefone["5511900000002"] == 1
    assert por_telefone["5511900000003"] == 2
    assert por_telefone["5511900000004"] == 3


async def test_degrau_2_nao_acumula_sobre_o_degrau_1(lead_factory):
    """A âncora é o inbound, não o envio anterior.

    Lead que recebeu o degrau 1 há muito tempo mas falou há 30 min ainda não
    venceu o degrau 2 (75 min desde o inbound).
    """
    await lead_factory("5511900000005", followup_count=1,
                       minutos_desde_interacao=600, minutos_desde_inbound=30)
    assert await _reivindicar() == []


async def test_lead_sem_last_inbound_at_nunca_e_reivindicado(lead_factory):
    """NULL é seguro por construção — leads importados não recebem nada."""
    await lead_factory("5511900000006", followup_count=0,
                       minutos_desde_interacao=600, minutos_desde_inbound=None)
    assert await _reivindicar() == []


async def test_janela_fechada_nao_reivindica_e_nao_queima_nivel(lead_factory):
    await lead_factory("5511900000020", followup_count=2,
                       minutos_desde_inbound=25 * 60)          # janela fechou
    irmao = await lead_factory("5511900000021", followup_count=2,
                               minutos_desde_inbound=23 * 60 + 5)  # dentro

    reivindicados = {r.phone for r in await _reivindicar()}

    assert reivindicados == {irmao}, (
        "o de fora da janela não pode entrar, e o de dentro TEM que entrar — "
        "sem o irmão, este teste passaria com uma implementação que não "
        "reivindica ninguém"
    )
    assert await _followup_count("5511900000020") == 2


async def test_fase_terminal_nunca_e_perseguida(lead_factory):
    for i, fase in enumerate(
        ["agendou_sessao", "qualificado", "desqualificado", "perdido"]
    ):
        await lead_factory(f"551190000003{i}", phase=fase,
                           followup_count=0, minutos_desde_inbound=60)
    assert await _reivindicar() == []


async def test_lead_que_voltou_de_uma_reuniao_cancelada_nao_e_perseguido(lead_factory):
    """Passa pela função de verdade da Fase 2, não por estado montado à mão."""
    await lead_factory("5511900000040", phase="agendou_sessao",
                       followup_count=2, minutos_desde_inbound=23 * 60 + 5)
    async with (await get_pool()).connection() as conn:
        await reverter_fase_apos_cancelamento(conn, "5511900000040")
        await conn.commit()

    assert await _reivindicar() == [], (
        "reverter_fase_apos_cancelamento religa followup_active e devolve o "
        "lead a 'qualificado', que o filtro exclui — se este teste quebrar, "
        "alguém tirou 'qualificado' do filtro e leads reais vão receber "
        "mensagem indevida"
    )


async def test_duas_transacoes_realmente_sobrepostas_nao_pegam_o_mesmo_lead(lead_factory):
    """Sem barreira, asyncio.gather não garante sobreposição: a primeira pode
    commitar antes de a segunda abrir, e o teste passaria sem SKIP LOCKED."""
    for i in range(6):
        await lead_factory(f"55119000005{i:02d}", followup_count=0,
                           minutos_desde_inbound=30)

    segurando = asyncio.Event()
    pode_soltar = asyncio.Event()

    async def primeira():
        pool = await get_pool()
        async with pool.connection() as conn:
            resultado = await _reivindicar_na_conexao(conn, limite=3)
            segurando.set()
            await pode_soltar.wait()   # segura a transação aberta
            await conn.commit()
        return resultado

    async def segunda():
        await segurando.wait()          # só entra com a primeira ainda aberta
        try:
            return await _reivindicar(limite=3)
        finally:
            pode_soltar.set()

    a, b = await asyncio.gather(primeira(), segunda())
    telefones = [r.phone for r in a] + [r.phone for r in b]
    assert len(telefones) == len(set(telefones)), "o mesmo lead saiu duas vezes"
    assert len(telefones) == 6


async def test_lead_que_falou_entre_o_claim_e_o_envio_nao_recebe(lead_factory):
    """O claim commita e só então o HTTP acontece. Nesse intervalo o lead pode
    ter escrito — e mandar "Fulano?" em cima da mensagem dele é o pior caso."""
    await lead_factory("5511900000060", followup_count=0, minutos_desde_inbound=30)

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    reivindicados = await _reivindicar()
    assert len(reivindicados) == 1

    # o lead escreve agora, entre o claim e o envio
    await aplicar_gate(
        await get_pool(),
        key={"remoteJid": "5511900000060@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )

    await _enviar_reivindicados(reivindicados, ClienteQueRegistra())
    assert enviados == [], "não pode enviar por cima de quem acabou de falar"


async def test_contador_sobe_antes_do_envio(lead_factory):
    """Divergência consciente do n8n: falha de envio pula um nível, e é o certo.

    Mandar a mesma mensagem duas vezes é pior que perder um follow-up.
    """
    await lead_factory("5511900000070", followup_count=0, minutos_desde_inbound=30)

    class ClienteQueFalha:
        async def send_message(self, to, body, **kwargs):
            raise EvolutionSendError(500, "boom")

    resumo = await rodada(await get_pool(), ClienteQueFalha())
    assert resumo["falhas"] == 1
    assert await _followup_count("5511900000070") == 1


async def test_rodada_conta_os_bloqueados_por_janela(lead_factory):
    """Sem esta métrica, 'a régua morreu' e 'não havia ninguém' são idênticos."""
    await lead_factory("5511900000080", followup_count=2, minutos_desde_inbound=25 * 60)

    resumo = await rodada(await get_pool(), _ClienteMudo())
    assert resumo["bloqueados_por_janela"] == 1
```

E os da mensagem, sem banco:

```python
def test_nivel_1_usa_so_o_primeiro_nome():
    assert montar_mensagem(1, "Fulano de Tal") == "Fulano?"


def test_nivel_1_sem_nome_vira_oi():
    assert montar_mensagem(1, None) == "Oi?"


def test_nome_ausente_nunca_vaza_o_marcador_do_contexto():
    """sanitizar_nome devolve a string 'não informado' quando não há nome.

    Reaproveitá-la aqui sem cuidado manda 'não informado?' para uma pessoa.
    """
    for entrada in (None, "", "   ", "não informado"):
        for nivel in (1, 3):
            texto = montar_mensagem(nivel, entrada)
            assert "não informado" not in texto.lower(), (nivel, entrada, texto)
            assert "None" not in texto


def test_nivel_3_sem_nome_nao_deixa_virgula_orfa():
    texto = montar_mensagem(3, None)
    assert not texto.startswith(","), texto
    assert texto == "Tudo bem? Ainda faz sentido falarmos sobre o seu momento de carreira?"
```

- [ ] **Step 2: Rodar e ver falhar**

- [ ] **Step 3: Implementar `reivindicar`**

```python
_SQL_REIVINDICAR = """
update leads_crm
set followup_count = followup_count + 1,
    last_interaction_at = now()
where phone in (
    select phone from leads_crm
    where followup_active
      and agent_active
      and phase not in ('agendou_sessao','desqualificado','perdido','qualificado')
      and last_inbound_at is not null
      and (
            (followup_count = 0
             and last_inbound_at < now() - make_interval(mins => %(n1)s))
         or (followup_count = 1
             and last_inbound_at < now() - make_interval(mins => %(n2)s))
         or (followup_count = 2
             and last_inbound_at < now() - make_interval(mins => %(n3)s))
      )
      and last_inbound_at > now() - make_interval(mins => %(janela)s)
    order by last_inbound_at
    limit %(limite)s
    for update skip locked
)
returning phone, name, followup_count
"""
```

`janela` = `24 * 60 - FOLLOWUP_JANELA_MARGEM_MINUTOS`, calculado em Python.

Três coisas que o docstring deve dizer:

- **Nenhuma transação fica aberta durante HTTP.** A reivindicação commita, e só
  então o envio acontece. Segurar `FOR UPDATE` esperando a Evolution responder é
  o defeito que este desenho evita.
- **Sem advisory lock.** O lock do Postgres é por sessão; com pool, a conexão
  volta segurando o lock e ele evapora sem aviso se ela for reciclada.
- **`followup_count` do `RETURNING` já é o novo valor** (1, 2 ou 3) — é o nível.

- [ ] **Step 4: Implementar `ainda_vale_enviar`**

Um `SELECT` por lead, imediatamente antes do envio. Aborta se, desde o claim:
`agent_active` virou `false`, `followup_active` virou `false`, `followup_count`
não é mais o nível reivindicado, ou `last_inbound_at` ficou mais recente que o
instante do claim. Cada aborto sai no log como `followup_abortado` com o motivo.

Sem isso, `BATCH_SIZE=10` com envio serial faz o último do lote sair minutos
depois do claim — e o degrau 1 dispara 15 minutos depois de o lead falar, que é
justamente quando ele tende a voltar.

- [ ] **Step 5: Implementar `primeiro_nome` e `montar_mensagem`**

`sanitizar_nome` (`agents/catalog/elevec_sdr/contexto.py`) **não serve sozinha**:
ela devolve o nome inteiro colapsado e a string literal `"não informado"` quando
vazio. Use-a como pré-passo e escreva `primeiro_nome`, que devolve `None` — nunca
um texto de preenchimento — quando não há nome utilizável.

```python
_NIVEL_2 = (
    "Opa, imagino que esteja corrido ai! Só para não perdermos o timing da "
    "sua aplicação, consegue falar agora?"
)
```

Nível 1: `f"{nome}?"` ou `"Oi?"`.
Nível 3: `f"{nome}, tudo bem? Ainda faz sentido..."` ou `"Tudo bem? Ainda faz sentido..."`.

- [ ] **Step 6: Implementar `reativar_agentes`, `contar_bloqueados_por_janela` e `rodada`**

`reativar_agentes` roda o `UPDATE` de `agent_reactivate_at` da especificação.
**Hoje é inerte** — nada em `src/` escreve a coluna, e `handover.py:30` documenta
que ela fica `NULL` de propósito porque o handover é permanente. Mantida por
paridade; diga isso no docstring para ninguém achar que está quebrada.

`rodada` devolve `{"enviados", "falhas", "abortados", "bloqueados_por_janela"}`.

- [ ] **Step 7: Rodar e ver passar**

- [ ] **Step 8: Provar por mutação — mínimo 10**

(a) tirar `for update skip locked`; (b) tirar o predicado de janela; (c) tirar
`last_inbound_at is not null`; (d) tirar `agent_active`; (e) tirar `qualificado`
das fases excluídas; (f) trocar a âncora dos degraus para `last_interaction_at`;
(g) `followup_count + 1` → `followup_count`; (h) mover o incremento para depois do
envio; (i) `ainda_vale_enviar` sempre `True`; (j) `primeiro_nome` devolvendo o
nome inteiro.

- [ ] **Step 9: Commit**

```bash
git add src/whatsapp_langchain/worker/followup.py \
        src/whatsapp_langchain/shared/config.py \
        tests/integration/test_followup.py
git commit -m "feat: regua de follow-up ancorada no inbound, com janela de 24h"
```

---

## Task 4: A task no Worker

**Files:**
- Modify: `src/whatsapp_langchain/worker/main.py`
- Test: `tests/integration/test_followup.py`

### A contradição que este plano tinha, e como fica resolvida

Uma versão anterior mandava a Task 4 não subir a task sem cliente Evolution **e**
a Task 6 derrubar o boot no mesmo caso. As duas não coexistem — com fail-fast, o
outro ramo é código morto.

**Decisão: sem fail-fast. O worker decide em runtime.** Motivo, e é o mesmo que a
Fase 2 aprendeu com o `PIPEDRIVE_API_TOKEN`: `validate_runtime_settings` roda
também no lifespan da **API**, que não roda follow-up. Um fail-fast ali derrubaria
a API por causa de uma flag do worker — inclusive impedindo `run_migrations`, que
vem depois da validação.

**Note também:** em `OUTBOUND_MODE=mock`, `channel_status()` marca todo canal como
completo e todos os clientes são instanciados. Logo, em dev com
`FOLLOWUP_ENABLED=true` a régua **sobe e roda de verdade** contra o cliente mock.
É aceitável, mas é surpresa — fixe com teste e documente.

- [ ] **Step 1: Escrever os testes que falham**

```python
async def test_task_desligada_por_padrao_nao_sobe(monkeypatch):
    monkeypatch.setattr(settings, "followup_enabled", False)
    assert iniciar_followup(pool, outbounds={MessagingChannel.EVOLUTION: _mudo()}) is None


async def test_sem_cliente_evolution_nao_sobe(monkeypatch, caplog):
    monkeypatch.setattr(settings, "followup_enabled", True)
    assert iniciar_followup(pool, outbounds={MessagingChannel.META: _mudo()}) is None
    assert "followup_sem_canal" in caplog.text


async def test_em_modo_mock_a_regua_sobe_e_roda(monkeypatch):
    """Documenta a surpresa: mock instancia todos os canais, então a régua roda."""
    monkeypatch.setattr(settings, "followup_enabled", True)
    assert iniciar_followup(pool, outbounds={MessagingChannel.EVOLUTION: _mudo()}) is not None


async def test_excecao_numa_rodada_nao_derruba_o_loop(monkeypatch):
    """Sem isto, um erro de banco mata a régua até o próximo deploy."""
    chamadas = []
    terceira = asyncio.Event()

    async def rodada_que_explode(*args, **kwargs):
        chamadas.append(1)
        if len(chamadas) >= 3:
            terceira.set()
        if len(chamadas) == 1:
            raise RuntimeError("banco caiu")
        return {"enviados": 0, "falhas": 0, "abortados": 0, "bloqueados_por_janela": 0}

    monkeypatch.setattr("whatsapp_langchain.worker.main.rodada", rodada_que_explode)
    monkeypatch.setattr(settings, "followup_enabled", True)
    monkeypatch.setattr(settings, "followup_interval_seconds", 0.01)

    tarefa = iniciar_followup(pool, outbounds={MessagingChannel.EVOLUTION: _mudo()})
    await asyncio.wait_for(terceira.wait(), timeout=5)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa

    assert len(chamadas) >= 3, "o loop tem que sobreviver à primeira exceção"
```

Note que o teste espera um `asyncio.Event`, não um `sleep` — `sleep` fixo em teste
de loop é frágil e some em CI lenta.

- [ ] **Step 2: Rodar e ver falhar**

- [ ] **Step 3: Implementar**

`iniciar_followup(pool, outbounds) -> asyncio.Task | None`, chamada em `main()`
depois de `_build_outbound_clients`. Devolve `None` (e loga o motivo) quando a
flag está desligada ou não há cliente Evolution. `try/except` por rodada, logando
`followup_rodada_falhou`. Cancelamento no `finally` que já fecha o pool.

- [ ] **Step 4: Rodar e ver passar**

- [ ] **Step 5: Provar por mutação — mínimo 3**

(a) subir a task com a flag desligada; (b) tirar o `try/except`; (c) subir sem
cliente Evolution.

- [ ] **Step 6: Commit**

```bash
git add src/whatsapp_langchain/worker/main.py tests/integration/test_followup.py
git commit -m "feat: task de follow-up no worker, desligada por padrao"
```

---

## Task 5: Webhook do ChatWoot

> **Não comece sem `docs/evidencias/payload-chatwoot-real.json` (Task 0).**

A especificação descreve esta rota em duas linhas, e a revisão adversarial achou
**cinco** defeitos nela. Todos estão corrigidos abaixo; o implementador precisa
entender cada um, porque são o motivo de o código não ser as duas linhas.

### Defeito 1 — a rota não tem autenticação

Como especificada, quem descobrir a URL desliga a Renata para qualquer telefone, ou
a religa por cima de um atendimento humano.

**Correção:** secret aceito **no path** (`POST /webhook/chatwoot/{secret}`) ou no
header `x-chatwoot-secret`, o que chegar. O path existe porque a integração
"Webhooks" do ChatWoot pode não ter campo de header customizado — confirme no
payload da Task 0. Comparação com `secrets.compare_digest`, sempre. Documente que
o secret no path aparece em log de acesso, e que o header é preferível quando
disponível.

### Defeito 2 — retomar pela ausência da etiqueta religa o agente sozinho

Este é o mais perigoso, e é a metade que a versão anterior deste plano **não**
corrigia. `conversation_updated` dispara para mudança de status, de responsável,
de atributo — todas com `labels: []` num lead que nunca teve a etiqueta.

Cenário concreto: o Silvio responde pelo WhatsApp (`fromMe` → `agent_active=false`,
`leads.py:339`), abre o ChatWoot e atribui a conversa a si → `conversation_updated`
com `labels: []` → **a Renata volta a responder por cima dele**. O mesmo desfaz o
`pausar_agente` escrito por `human_handover`.

A autoridade é assimétrica: **pausar por etiqueta é seguro; retomar pela ausência
dela não é**, porque a ausência é o estado default de toda conversa do ChatWoot.

**Correção:** só agir com sinal positivo de que os rótulos mudaram —
`changed_attributes` contendo `labels` (confirme a forma no payload da Task 0). Sem
esse sinal, ignorar o evento inteiro, inclusive quando `labels` vem presente e
vazia.

### Defeito 3 — pausar não cancela o que já está na fila

`agent_active` só é lido no gate, na ingestão. O `processor` **não relê nada**. Uma
mensagem já em `message_queue` é respondida sem consultar o estado — durante
`MESSAGE_BUFFER_SECONDS` + fila + turno do LLM, dezenas de segundos. Se o humano
pausou *porque* a Renata falou besteira, a próxima já está a caminho.

**Correção, as duas:** a rota marca como canceladas as linhas pendentes daquele
telefone; e o `processor` relê `agent_active` antes de enviar, descartando se
mudou. O furo já existe para o `fromMe` — a diferença é que a Task 5 vende a pausa
como recurso.

### Defeito 4 — retomar não reseta a escada

O lead volta com `followup_count` e relógios de antes da pausa. Vencidos, a próxima
rodada manda "Fulano?" **em cima da conversa que o humano acabou de encerrar**. O
gate reseta a escada em todo inbound justamente por isso (`leads.py:376-378`).

**Correção:** retomar faz `followup_count = 0`, `followup_active = true`,
`last_interaction_at = now()`. **Não** mexe em `last_inbound_at` — esse é fato
sobre a Meta e só o gate escreve.

### Defeito 5 — reentrega fora de ordem

O handler é idempotente por evento, mas não ordenado. Uma reentrega atrasada de um
`conversation_updated` antigo, aplicada depois do evento de pausa, religa a Renata.

**Correção:** guardar `conversation.updated_at` em `metadata` e recusar retrocesso.

**Files:**
- Create: `src/whatsapp_langchain/server/routes/webhook_chatwoot.py`
- Modify: `src/whatsapp_langchain/server/app.py`, `shared/config.py`,
  `worker/processor.py`
- Test: `tests/integration/test_webhook_chatwoot.py`

- [ ] **Step 1: Escrever os testes que falham**

Cobrindo, no mínimo: pausa pela etiqueta; retomada com `changed_attributes`
provando a mudança; **`labels: []` sem `changed_attributes` não religa**;
`message_created` não religa; 401 sem secret; 401 com secret errado; secret no
path e no header; telefone com e sem o nono dígito atingindo o mesmo lead; lead
inexistente não é criado; retomada zera a escada; evento com `updated_at` mais
velho que o guardado é recusado; e a fila do telefone é cancelada na pausa.

Extraia o telefone com a cadeia de fallback que o payload da Task 0 mostrar —
`phone_number` primeiro, `identifier` depois. Quando nenhum serve, responda 200
(**nunca 500**, o ChatWoot repetiria) e logue **as chaves** do payload, não o
conteúdo.

- [ ] **Step 2..6: falhar, implementar, passar, mutar (mínimo 8), commitar**

Mutações obrigatórias: (a) tratar `labels` ausente como vazia; (b) religar sem
`changed_attributes`; (c) remover a dependência do secret; (d) `==` no lugar de
`compare_digest`; (e) casar só a variação exata do telefone; (f) trocar `UPDATE`
por upsert; (g) retomar sem zerar a escada; (h) aceitar evento com `updated_at`
retrógrado.

---

## Task 6: Configuração e documentação

**Files:** `.env.example`, `deploy/.env.prod.example`, `docs/AGENTE_ELEVEC.md`,
`CLAUDE.md`, `agents/catalog/elevec_sdr/tools/crm.py`, `tests/unit/test_config_sdr.py`

- [ ] **Step 1: Corrigir a promessa da Fase 2**

`crm.py:197` diz que religar `followup_active` no cancelamento serve para que *"o
lead precisa ser perseguido para remarcar"*. **Isso não acontece**: o cancelamento
devolve o lead a `qualificado`, e a régua exclui `qualificado`.

**Decisão: manter o filtro da especificação e corrigir o texto.** Numa migração, a
direção segura de errar é não mandar mensagem que o sistema atual não manda.
Reescreva o docstring para dizer o que a coluna realmente faz — restaurar a
coerência do estado, já que `agendou_sessao` a desligou como consequência de haver
reunião — e registre em `docs/AGENTE_ELEVEC.md` que perseguir quem cancelou é uma
melhoria a discutir com o cliente **depois** do cutover, não uma promessa em
aberto.

- [ ] **Step 2: `CHATWOOT_WEBHOOK_SECRET` com fail-fast**

Mesma regra do `EVOLUTION_WEBHOOK_SECRET`: obrigatório em
`ENVIRONMENT=production` quando a rota está em uso, mínimo 32 caracteres. **Este**
fail-fast pode ficar em `validate_runtime_settings` — é da API, e a rota é da API.
O do follow-up não (ver Task 4).

- [ ] **Step 3: Documentar as variáveis**

Nos dois `.env.example`. O comentário dos degraus tem que explicar a âncora:

```
# Os três degraus contam a partir de last_inbound_at — a última mensagem DO
# LEAD —, não do envio anterior. Isso os torna absolutos: os até 5 minutos de
# atraso de cada rodada não se acumulam. Ancorar em last_interaction_at (como
# fazia o n8n) empurrava o degrau 3 para ~24h28 desde a criação do lead, fora
# da janela de 24h da Cloud API, e a Meta rejeita em vez de atrasar.
FOLLOWUP_NIVEL1_MINUTOS=15
FOLLOWUP_NIVEL2_MINUTOS=75
FOLLOWUP_NIVEL3_MINUTOS=1380
```

- [ ] **Step 4: `docs/AGENTE_ELEVEC.md`**

Os três degraus e a âncora; a divergência do contador (sobe antes do envio, e por
quê); o que acontece com quem sai da janela e como enxergar isso
(`bloqueados_por_janela`); a rota do ChatWoot com os cinco defeitos corrigidos e
o que o operador precisa configurar; e a nota de que `send_template` existe mas
não é usado pelo follow-up, com o motivo.

- [ ] **Step 5: `CLAUDE.md`**

`/webhook/chatwoot` na lista de rotas. Na convenção 11, deixar claro que o
ChatWoot **não** é canal de mensageria — não grava `message_queue.channel`, só
mexe em `leads_crm`.

- [ ] **Step 6: Suíte inteira, e commit**

---

## Auto-revisão

**Cobertura da especificação:** todos os requisitos das seções "Follow-up",
"Handover pelo ChatWoot" e "Impacto nas fases" têm task. A revisão adversarial
não achou requisito fora do plano.

**Divergências conscientes, todas registradas no código e na documentação:**

| O que | Por quê |
|---|---|
| Degraus ancorados em `last_inbound_at` | o desenho cumulativo empurra o degrau 3 para fora da janela (medido: 24h28) |
| Contador sobe antes do envio | mandar a mesma mensagem duas vezes é pior que perder um follow-up |
| Backfill deixa `NULL` para `followup_count > 0` | o valor disponível é o nosso envio, não o inbound |
| Retomada no ChatWoot exige sinal positivo | ausência de etiqueta é o estado default de toda conversa |
| Sem fail-fast do follow-up no boot | `validate_runtime_settings` roda na API, que não roda follow-up |

**Contrato para a Fase 4 (`migrar_supabase.py`), a não perder:**

- O importador **nunca escreve `last_inbound_at`**. Ele nasce `NULL` e só o gate o
  preenche. Copiar `last_interaction_at` do Supabase ressuscita em escala o
  problema que a 013 evita.
- **Backfill de `google_event_id`** (herdado da Fase 2): a coluna é nova, e leads
  legados têm reunião real com ela nula. O código lê nulo como "não tem reunião",
  o que deixa `update_crm('qualificado')` puxar o card de volta.

**Dívida registrada para a Fase 5:** o mapa `lead_source → template`
(`linkedin_form` → `boas_vindas_renata_linkedin_02`, `respondiapp_form` →
`boas_vindas_renata_respondiapp_03`) não é portado nesta fase, porque nada aqui
chama `send_template`. O cutover precisa dele.

**Fora de escopo, com motivo:** reabrir janela fechada (exige template de retomada
aprovado pela Meta — decisão do cliente); ingestão de leads por formulário (a
especificação já a coloca fora); métricas da régua em `/api/metrics`.
