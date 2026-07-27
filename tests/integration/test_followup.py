"""Régua de follow-up: reivindicação atômica, escada ancorada no inbound e janela de
24h.

**Um envio indevido aqui é WhatsApp para gente de verdade** — por isso os
testes de risco mais alto (concorrência real e janela fechada) são
propositalmente difíceis de escrever de forma honesta. Ver os comentários em
cada um.

Duplicata (mesma pessoa, duas linhas físicas) não é mais testável AQUI: a
migração `014_uma_linha_por_pessoa.sql` consolidou a base legada e o CHECK
`leads_crm_phone_canonico_check` proíbe o banco de aceitar de volta as duas
formas físicas que causavam isso (o 9º dígito do celular e o zero de
tronco) — `lead_factory` insere direto em `leads_crm`, então tentar recriar
um par duplicado aqui levantaria `CheckViolation`, não reproduziria o bug.
Essa prova agora mora em `tests/integration/test_migracao_014.py`
(consolidação) e no teste de invariante abaixo (o CHECK em si). Isso também
significa que TODO telefone usado neste arquivo precisa estar na forma
CANÔNICA (sem o 9º dígito) — um literal com 13 dígitos que sobreviver de uma
versão anterior deste arquivo quebra no `INSERT` do `lead_factory`.
"""

import asyncio

import psycopg
import pytest
import pytest_asyncio
import structlog

from whatsapp_langchain.agents.catalog.elevec_sdr.tools.crm import (
    reverter_fase_apos_cancelamento,
)
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.worker import followup
from whatsapp_langchain.worker.evolution_client import EvolutionSendError
from whatsapp_langchain.worker.followup import (
    LeadReivindicado,
    _enviar_reivindicados,
    _reivindicar_na_conexao,
    ainda_vale_enviar,
    contar_bloqueados_por_janela,
    montar_mensagem,
    primeiro_nome,
    reivindicar,
    rodada,
)
from whatsapp_langchain.worker.main import _parar_followup, iniciar_followup

# Round-trip por `canonicalizar`: "55" + DDD(11) + 8 dígitos, sem inserir o
# 9º dígito — permanece igual a si mesmo depois de canonicalizado. Usado nos
# testes que chamam uma tool real (`reverter_fase_apos_cancelamento`), que
# resolve o telefone com `canonico_do_lead` em vez de casar a string crua.
TELEFONE_CANONICO_ESTAVEL = "551190000040"


async def _reivindicar(**kwargs) -> list[LeadReivindicado]:
    return await reivindicar(await get_pool(), **kwargs)


async def _followup_count(phone: str) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select followup_count from leads_crm where phone = %s", (phone,)
        )
        linha = await cur.fetchone()
    assert linha is not None
    return linha[0]


class _ClienteMudo:
    async def send_message(self, to, body, **kwargs):
        raise AssertionError("cliente mudo não deveria ser chamado neste teste")


@pytest_asyncio.fixture
async def bloquear():
    """Insere na `blocklist` e limpa no teardown — `lead_factory` não toca
    essa tabela, então os testes de opt-out precisam da própria limpeza."""
    pool = await get_pool()
    inseridos: list[str] = []

    async def _bloquear(phone: str, motivo: str = "opt-out") -> None:
        async with pool.connection() as conn:
            await conn.execute(
                "insert into blocklist (phone, motivo) values (%s, %s)",
                (phone, motivo),
            )
            await conn.commit()
        inseridos.append(phone)

    yield _bloquear

    async with pool.connection() as conn:
        await conn.execute("delete from blocklist where phone = any(%s)", (inseridos,))
        await conn.commit()


async def test_os_tres_degraus_ancoram_no_inbound(lead_factory):
    await lead_factory("551100000001", followup_count=0, minutos_desde_inbound=10)
    await lead_factory("551100000002", followup_count=0, minutos_desde_inbound=20)
    await lead_factory("551100000003", followup_count=1, minutos_desde_inbound=80)
    await lead_factory(
        "551100000004", followup_count=2, minutos_desde_inbound=23 * 60 + 5
    )

    por_telefone = {r.phone: r.nivel for r in await _reivindicar()}

    assert "551100000001" not in por_telefone, "10 min não vence o degrau 1"
    assert por_telefone["551100000002"] == 1
    assert por_telefone["551100000003"] == 2
    assert por_telefone["551100000004"] == 3


async def test_limite_prioriza_o_last_inbound_at_mais_antigo(lead_factory):
    """Justiça de fila: com mais elegíveis do que `limite`, quem espera há
    mais tempo (`last_inbound_at` mais antigo) tem que ser servido primeiro
    — não o mais recente. `order by last_inbound_at` sem `desc` é o que
    garante isso; um `desc` acidental inverteria a prioridade sem quebrar
    nenhum outro teste desta suíte (nenhum indevido sai, a régua só atende
    fora de ordem)."""
    mais_urgente = await lead_factory(
        "551100000210", followup_count=0, minutos_desde_inbound=90
    )
    await lead_factory("551100000211", followup_count=0, minutos_desde_inbound=60)
    await lead_factory("551100000212", followup_count=0, minutos_desde_inbound=30)

    reivindicados = await _reivindicar(limite=1)

    assert [r.phone for r in reivindicados] == [mais_urgente], (
        "com limite=1 e três elegíveis, o mais urgente (90 min de espera) "
        "tem que ser o único reivindicado"
    )


async def test_degrau_2_nao_acumula_sobre_o_degrau_1(lead_factory):
    """A âncora é o inbound, não o envio anterior.

    Lead que recebeu o degrau 1 há muito tempo mas falou há 30 min ainda não
    venceu o degrau 2 (75 min desde o inbound).
    """
    await lead_factory(
        "551100000005",
        followup_count=1,
        minutos_desde_interacao=600,
        minutos_desde_inbound=30,
    )
    assert await _reivindicar() == []


async def test_lead_sem_last_inbound_at_nunca_e_reivindicado(lead_factory):
    """NULL é seguro por construção — leads importados não recebem nada."""
    await lead_factory(
        "551100000006",
        followup_count=0,
        minutos_desde_interacao=600,
        minutos_desde_inbound=None,
    )
    assert await _reivindicar() == []


async def test_janela_fechada_nao_reivindica_e_nao_queima_nivel(lead_factory):
    fora_da_janela = await lead_factory(
        "551100000020", followup_count=2, minutos_desde_inbound=25 * 60
    )  # janela fechou
    dentro = await lead_factory(
        "551100000021", followup_count=2, minutos_desde_inbound=23 * 60 + 5
    )  # dentro

    reivindicados = {r.phone for r in await _reivindicar()}

    assert reivindicados == {dentro}, (
        "o de fora da janela não pode entrar, e o de dentro TEM que entrar — "
        "sem o segundo lead, este teste passaria com uma implementação que "
        "não reivindica ninguém"
    )
    assert await _followup_count(fora_da_janela) == 2


async def test_fase_terminal_nunca_e_perseguida(lead_factory):
    """As quatro fases do filtro, uma a uma — inclusive `qualificado`, que
    não pode sair da lista: é a fase para onde
    `reverter_fase_apos_cancelamento` (Fase 2) devolve o lead quando a
    reunião é cancelada. Com o claim reduzido a uma consulta única (Task 7),
    este é o ÚNICO lugar que barra `qualificado` — sem um irmão para
    alcançar por uma segunda consulta sem filtro, não existe mais caminho
    que contorne este `phase not in (...)`."""
    for i, fase in enumerate(
        ["agendou_sessao", "qualificado", "desqualificado", "perdido"]
    ):
        await lead_factory(
            f"55110000003{i}",
            phase=fase,
            followup_count=0,
            minutos_desde_inbound=60,
        )
    assert await _reivindicar() == []


async def test_lead_que_ja_recebeu_os_tres_degraus_nunca_e_reivindicado(lead_factory):
    """Três degraus é o fim — nenhum quarto envio existe. Um lead em
    `followup_count = 3` (já passou pelos três), ativo, fase elegível e
    dentro da janela não pode voltar a ser candidato: nenhuma das três
    condições do degrau (`= 0`, `= 1`, `= 2`) casa `followup_count = 3`,
    mas essa proteção não pode depender só disso — um mutante que trocasse
    `= 2` por `>= 2` no braço do degrau 3 reabriria a régua para este lead,
    e `montar_mensagem(4)` levantaria a cada rodada (capturado como falha,
    nenhum WhatsApp indevido sai, mas o contador sobe e o log enche por até
    23h sem que ninguém perceba). `followup_count <= 2` no predicado é a
    barreira estrutural contra essa classe de mutação.

    `minutos_desde_inbound` tem que passar do limiar do DEGRAU 3 (23h,
    `n3_min`) — não um valor pequeno qualquer. Com um valor abaixo do
    limiar, a condição de tempo do braço `>= 2` (mutado) já falha sozinha,
    e o teste passaria mesmo SEM a barreira `followup_count <= 2` — não
    provaria nada sobre ela."""
    phone = await lead_factory(
        "551100000201", followup_count=3, minutos_desde_inbound=23 * 60 + 5
    )
    assert await _reivindicar() == []
    assert await _followup_count(phone) == 3, "não pode ter avançado nem sido tocado"


async def test_agente_pausado_nao_e_reivindicado(lead_factory):
    """`agent_active=False` (handover humano em curso) não pode ser perseguido,
    mesmo com fase e relógio elegíveis para o degrau."""
    await lead_factory(
        "551100000035",
        agent_active=False,
        followup_count=0,
        minutos_desde_inbound=60,
    )
    assert await _reivindicar() == []


async def test_lead_com_followup_active_false_nao_e_reivindicado(lead_factory):
    """Mesma classe da checagem de `agent_active` acima, mas para
    `followup_active` — caminho independente de desligamento da régua."""
    await lead_factory(
        "551100000036",
        followup_active=False,
        followup_count=0,
        minutos_desde_inbound=60,
    )
    assert await _reivindicar() == []


async def test_lead_que_voltou_de_uma_reuniao_cancelada_nao_e_perseguido(lead_factory):
    """Passa pela função de verdade da Fase 2, não por estado montado à mão."""
    await lead_factory(
        TELEFONE_CANONICO_ESTAVEL,
        phase="agendou_sessao",
        followup_count=2,
        minutos_desde_inbound=23 * 60 + 5,
    )

    await reverter_fase_apos_cancelamento(TELEFONE_CANONICO_ESTAVEL)

    assert await _reivindicar() == [], (
        "reverter_fase_apos_cancelamento religa followup_active e devolve o "
        "lead a 'qualificado', que o filtro exclui — se este teste quebrar, "
        "alguém tirou 'qualificado' do filtro e leads reais vão receber "
        "mensagem indevida"
    )


async def test_duas_transacoes_realmente_sobrepostas_nao_pegam_o_mesmo_lead(
    lead_factory,
):
    """Sem barreira, asyncio.gather não garante sobreposição: a primeira pode
    commitar antes de a segunda abrir, e o teste passaria sem SKIP LOCKED.

    Com a Task 7, `_SQL_ELEGIVEIS_TRAVADOS` trava exatamente `limite` leads
    por chamada (não há mais oversampling para capturar grupo de duplicata,
    porque duplicata deixou de existir) — por isso o lote aqui só precisa
    ser maior que `limite`, não maior que `limite * um fator`.
    """
    limite = 3
    total_leads = limite + 5
    for i in range(total_leads):
        await lead_factory(
            f"551100020{i:03d}", followup_count=0, minutos_desde_inbound=30
        )

    segurando = asyncio.Event()
    pode_soltar = asyncio.Event()

    async def primeira():
        pool = await get_pool()
        async with pool.connection() as conn:
            resultado = await _reivindicar_na_conexao(conn, limite=limite)
            segurando.set()
            await pode_soltar.wait()  # segura a transação aberta
            await conn.commit()
        return resultado

    async def segunda():
        await segurando.wait()  # só entra com a primeira ainda aberta
        try:
            return await _reivindicar(limite=limite)
        finally:
            pode_soltar.set()

    a, b = await asyncio.gather(primeira(), segunda())
    telefones = [r.phone for r in a] + [r.phone for r in b]
    assert len(telefones) == len(set(telefones)), "o mesmo lead saiu duas vezes"
    assert len(telefones) == limite * 2


async def test_invariante_de_telefone_canonico_e_garantida_pelo_banco():
    """A prova de que a inversão da Task 7 realmente aconteceu: não é mais
    o claim que se defende de duplicata, é o banco que a torna
    IRREPRESENTÁVEL. Inserir a forma com o 9º dígito — a mais comum das
    duas que causavam os três Críticos das rodadas anteriores — tem que
    levantar `CheckViolation`, não silenciosamente criar uma segunda linha
    para a mesma pessoa.

    A migração real (`014_uma_linha_por_pessoa.sql`) é testada com mais
    profundidade em `test_migracao_014.py` (consolidação, as três formas
    proibidas); este teste prova só que o CHECK está de fato em vigor nesta
    tabela, do ponto de vista de quem usa `leads_crm` pela aplicação.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = conn.cursor()
            with pytest.raises(psycopg.errors.CheckViolation):
                await cur.execute(
                    "insert into leads_crm (phone) values (%s)", ("5511987654321",)
                )
            raise psycopg.Rollback()


async def test_starvation_por_blocklist_nao_ocorre_mais(lead_factory, bloquear):
    """Reprodução do segundo Importante da Task 3: o corte `[:limite]`
    acontecia ANTES do filtro de blocklist — bloqueados nunca avançam
    `followup_count`, então ocupavam os mesmos slots rodada após rodada.
    Reproduzido ao vivo: 10 bloqueados mais antigos + 1 lead real, com
    `limite=1` → zero enviados, por até 23h.

    A Task 7 move o filtro de blocklist para DENTRO do predicado de
    `_SQL_ELEGIVEIS_TRAVADOS`, antes do `LIMIT` — um bloqueado nunca ocupa
    slot nenhum, porque nunca é candidato. Os 10 bloqueados abaixo são
    deliberadamente MAIS urgentes (`last_inbound_at` mais antigo) que o
    lead real — se o filtro ainda rodasse depois do corte, `limite=1`
    devolveria só o mais urgente dos 11 (um bloqueado) e o lead real nunca
    seria alcançado.
    """
    bloqueados = [
        await lead_factory(
            f"551100030{i:03d}", followup_count=0, minutos_desde_inbound=90 + i
        )
        for i in range(10)
    ]
    for phone in bloqueados:
        await bloquear(phone)

    lead_real = await lead_factory(
        "551100031000", followup_count=0, minutos_desde_inbound=30
    )

    reivindicados = await _reivindicar(limite=1)

    assert [r.phone for r in reivindicados] == [lead_real], (
        "com limite=1 e 10 bloqueados mais urgentes na fila, o lead real "
        "tem que ser o único reivindicado — se o filtro de blocklist "
        "rodasse depois do corte, o resultado seria a lista vazia"
    )
    for phone in bloqueados:
        assert await _followup_count(phone) == 0, (
            "bloqueado nunca avança o contador — não é isso que resolve a "
            "starvation, é nunca ocupar o slot"
        )


async def test_telefone_na_blocklist_nao_e_reivindicado(lead_factory, bloquear):
    """A régua é o único caminho do sistema que fala sem o lead ter
    falado — é onde o opt-out mais importa. Reproduzido: telefone
    bloqueado com linha elegível em `leads_crm` → `rodada` → mensagem
    entregue a quem pediu para parar."""
    phone = await lead_factory(
        "551100000140", followup_count=0, minutos_desde_inbound=30
    )
    await bloquear(phone)

    assert await _reivindicar() == []

    resumo = await rodada(await get_pool(), _ClienteMudo())
    assert resumo["enviados"] == 0


async def test_telefone_bloqueado_por_variacao_nao_e_reivindicado(
    lead_factory, bloquear
):
    """A blocklist pode ter a OUTRA forma (com o 9º dígito) do mesmo
    telefone — o `CHECK` da blocklist aceita as duas, igual ao gate.
    `leads_crm.phone` em si é sempre canônico (sem o 9º); é só a
    `blocklist` que ainda aceita as duas formas."""
    await lead_factory("551190000141", followup_count=0, minutos_desde_inbound=30)
    await bloquear("5511990000141")  # forma com o 9º dígito

    assert await _reivindicar() == []


async def test_ainda_vale_enviar_falso_para_telefone_bloqueado(lead_factory, bloquear):
    phone = await lead_factory(
        "551100000142",
        followup_count=1,
        minutos_desde_inbound=5,
        minutos_desde_interacao=0,
    )
    await bloquear(phone)

    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_lead_que_falou_entre_o_claim_e_o_envio_nao_recebe(lead_factory):
    """O claim commita e só então o HTTP acontece. Nesse intervalo o lead pode
    ter escrito — e mandar "Fulano?" em cima da mensagem dele é o pior caso.
    """
    phone = "551190000060"
    await lead_factory(phone, followup_count=0, minutos_desde_inbound=30)

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
        key={"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )
    assert await _followup_count(phone) == 0, (
        "o gate tem que ter zerado o contador aqui — é esse zeramento, não "
        "'lead sumiu', que este teste precisa provar que barra o envio"
    )

    await _enviar_reivindicados(reivindicados, ClienteQueRegistra())
    assert enviados == [], "não pode enviar por cima de quem acabou de falar"


async def test_contador_sobe_antes_do_envio(lead_factory):
    """Divergência consciente do n8n: falha de envio pula um nível, e é o certo.

    Mandar a mesma mensagem duas vezes é pior que perder um follow-up.
    """
    await lead_factory("551100000070", followup_count=0, minutos_desde_inbound=30)

    class ClienteQueFalha:
        async def send_message(self, to, body, **kwargs):
            raise EvolutionSendError(500, "boom")

    resumo = await rodada(await get_pool(), ClienteQueFalha())
    assert resumo["falhas"] == 1
    assert await _followup_count("551100000070") == 1


async def test_rodada_conta_os_bloqueados_por_janela(lead_factory):
    """Sem esta métrica, 'a régua morreu' e 'não havia ninguém' são idênticos."""
    await lead_factory("551100000080", followup_count=2, minutos_desde_inbound=25 * 60)

    resumo = await rodada(await get_pool(), _ClienteMudo())
    assert resumo["bloqueados_por_janela"] == 1


async def test_ainda_vale_enviar_falso_para_lead_inexistente():
    assert await ainda_vale_enviar(await get_pool(), "551100000099", 1) is False


async def test_ainda_vale_enviar_falso_quando_nivel_nao_bate_mais(lead_factory):
    phone = await lead_factory(
        "551100000090",
        followup_count=2,
        minutos_desde_inbound=5,
        minutos_desde_interacao=0,
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_ainda_vale_enviar_falso_quando_followup_active_false(lead_factory):
    phone = await lead_factory(
        "551100000091",
        followup_count=1,
        followup_active=False,
        minutos_desde_inbound=5,
        minutos_desde_interacao=0,
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_ainda_vale_enviar_falso_quando_agent_active_false(lead_factory):
    """A checagem que o docstring vende como a proteção real contra um
    humano pausando pelo ChatWoot no meio do lote — sem teste direto até
    aqui, sobrevivia a mutação."""
    phone = await lead_factory(
        "551100000092",
        followup_count=1,
        agent_active=False,
        minutos_desde_inbound=5,
        minutos_desde_interacao=0,
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_ainda_vale_enviar_falso_quando_inbound_passa_do_instante_do_claim(
    lead_factory,
):
    """Caminho real: o gate zera `followup_count` no mesmo `now()` em que
    grava `last_inbound_at` — então, na prática, é o zeramento de
    `followup_count` (checagem anterior) que costuma pegar primeiro um
    lead que falou de novo. Construído direto no banco para testar este
    ramo isoladamente, sem depender de qual checagem chega primeiro em
    algum caminho real."""
    phone = await lead_factory(
        "551100000094",
        followup_count=1,
        minutos_desde_interacao=10,  # "instante do claim", 10 min atrás
        minutos_desde_inbound=1,  # last_inbound_at mais recente que isso
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_ainda_vale_enviar_verdadeiro_quando_nada_mudou(lead_factory):
    phone = await lead_factory(
        "551100000093",
        followup_count=1,
        minutos_desde_inbound=20,
        minutos_desde_interacao=0,
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is True


async def test_excecao_no_meio_do_lote_nao_trava_o_resto_nem_perde_o_log(
    lead_factory, monkeypatch
):
    """`ainda_vale_enviar` e `montar_mensagem` viviam FORA do try em
    `_enviar_reivindicados` — uma exceção no lead do meio interrompia o
    loop inteiro, silenciosamente: os leads seguintes ficavam com
    `followup_count` já incrementado (pelo claim) mas sem mensagem e sem
    log de falha nenhum.

    O monkeypatch aqui é sobre controle de fluxo do Python, não sobre a
    camada de banco: as três chamadas de `ainda_vale_enviar` continuam
    batendo no Postgres real (delegam para a função original); só a do
    segundo lead é interceptada para simular uma falha imprevista no meio
    do lote.
    """
    p1 = await lead_factory("551100000101", followup_count=0, minutos_desde_inbound=30)
    p2 = await lead_factory("551100000102", followup_count=0, minutos_desde_inbound=30)
    p3 = await lead_factory("551100000103", followup_count=0, minutos_desde_inbound=30)

    reivindicados = await _reivindicar()
    assert {r.phone for r in reivindicados} == {p1, p2, p3}

    original = followup.ainda_vale_enviar

    async def _quebra_no_segundo(pool, phone, nivel):
        if phone == p2:
            raise RuntimeError("falha simulada, não é bug de banco")
        return await original(pool, phone, nivel)

    monkeypatch.setattr(followup, "ainda_vale_enviar", _quebra_no_segundo)

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    resumo = await _enviar_reivindicados(reivindicados, ClienteQueRegistra())

    assert resumo["falhas"] == 1
    assert resumo["enviados"] == 2
    assert len(enviados) == 2


async def test_envio_usa_e164_no_to(lead_factory):
    phone = await lead_factory(
        "551100000110", followup_count=0, minutos_desde_inbound=30
    )
    reivindicados = await _reivindicar()
    alvo = next(r for r in reivindicados if r.phone == phone)

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    await _enviar_reivindicados([alvo], ClienteQueRegistra())
    assert enviados == [f"+{phone}"]


async def test_rodada_usa_default_corrente_de_settings_via_monkeypatch(
    lead_factory, monkeypatch
):
    """Prova o mecanismo do sentinel `None`: sem passar kwarg nenhum de
    degrau/limite para `rodada`, o valor efetivo é o de `settings` NO
    MOMENTO DA CHAMADA — não uma cópia congelada na importação do módulo.
    Um default `= settings.followup_x` no cabeçalho da função não seria
    alcançado por `monkeypatch.setattr(settings, ...)`; isso ia morder as
    Tasks 4 e 6, que precisam variar `FOLLOWUP_*` em teste sem mexer em
    env global.
    """
    await lead_factory("551190000130", followup_count=0, minutos_desde_inbound=10)
    monkeypatch.setattr(followup.settings, "followup_nivel1_minutos", 5)
    monkeypatch.setattr(followup.settings, "followup_batch_size", 1)

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    resumo = await rodada(await get_pool(), ClienteQueRegistra())
    assert resumo["enviados"] == 1, (
        "10 min não vence o degrau 1 com o default de 15 min — só vence "
        "com o valor monkeypatchado (5 min) sendo lido de verdade"
    )


async def test_reivindicar_chamado_direto_usa_default_corrente_de_settings(
    lead_factory, monkeypatch
):
    """`rodada` sempre encaminha `None` explícito para `reivindicar`
    (nunca lê `settings` sozinha), então o teste acima não prova nada sobre
    o default da PRÓPRIA `reivindicar` — só sobre o de
    `_reivindicar_na_conexao`. Quem chama `reivindicar` direto (como
    `_reivindicar()` neste arquivo, sem passar `n1_min`) usa o default do
    cabeçalho de `reivindicar` mesmo, e é esse caminho que este teste
    tranca."""
    await lead_factory("551190000132", followup_count=0, minutos_desde_inbound=10)
    monkeypatch.setattr(followup.settings, "followup_nivel1_minutos", 5)

    reivindicados = await _reivindicar()
    assert len(reivindicados) == 1, (
        "10 min não vence o degrau 1 com o default de 15 min — só vence "
        "com o valor monkeypatchado (5 min) sendo lido de verdade por "
        "reivindicar() diretamente, sem passar por rodada()"
    )


async def test_contar_bloqueados_por_janela_usa_default_corrente_de_settings(
    lead_factory, monkeypatch
):
    await lead_factory("551190000131", followup_count=0, minutos_desde_inbound=20)
    # degrau 1 (default 15 min) já vencido aos 20 min; a janela default
    # (23h30 de folga) não fechou aos 20 min — não bloqueado ainda. Encurtar
    # a margem para 1439 min (janela de 1 min) fecha a janela e o lead passa
    # a contar como bloqueado — só se o default for lido em tempo de chamada.
    monkeypatch.setattr(followup.settings, "followup_janela_margem_minutos", 1439)
    assert await contar_bloqueados_por_janela(await get_pool()) == 1


async def test_rodada_reativa_agentes_com_reativacao_agendada_vencida(lead_factory):
    """`reativar_agentes` existia sem chamador. Decisão: chamar no início de
    `rodada`, por paridade com o ciclo da especificação — é seguro porque
    hoje nada escreve `agent_reactivate_at` (o `UPDATE` afeta zero linhas
    em produção), e este teste prova o comportamento para quando algo
    passar a escrever."""
    phone = await lead_factory(
        "551100000111",
        agent_active=False,
        followup_active=False,
        followup_count=0,
        minutos_desde_inbound=None,
    )
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "update leads_crm set agent_reactivate_at = now() - interval '1 minute' "
            "where phone = %s",
            (phone,),
        )
        await conn.commit()

    await rodada(pool, _ClienteMudo())

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active, agent_reactivate_at from leads_crm where phone = %s",
            (phone,),
        )
        linha = await cur.fetchone()
    assert linha is not None
    agent_active, agent_reactivate_at = linha
    assert agent_active is True
    assert agent_reactivate_at is None


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
    assert (
        texto == "Tudo bem? Ainda faz sentido falarmos sobre o seu momento de carreira?"
    )


def test_primeiro_nome_de_nome_composto():
    assert primeiro_nome("Fulano de Tal") == "Fulano"


def test_primeiro_nome_de_none_e_none():
    assert primeiro_nome(None) is None


def test_nivel_2_usa_o_texto_fixo_da_especificacao_e_nao_vaza_nome():
    texto_fixo = (
        "Opa, imagino que esteja corrido ai! Só para não perdermos o timing da "
        "sua aplicação, consegue falar agora?"
    )
    assert montar_mensagem(2, "Fulano") == texto_fixo
    assert montar_mensagem(2, None) == texto_fixo, "nível 2 não pode vazar o nome"


def test_pushname_sem_espaco_nao_vaza_para_o_whatsapp():
    """`pushName` é atributo do WhatsApp escolhido pelo REMETENTE — texto de
    um estranho indo para uma mensagem que sai do número comercial da
    empresa. Sem espaço, `.split(" ")[0]` sozinho não segura nada."""
    texto = montar_mensagem(1, "Ganhe1000reais>>http://evil.tld")
    assert texto == "Oi?", texto


def test_pushname_so_pontuacao_vira_saudacao_generica():
    assert montar_mensagem(1, "?") == "Oi?"


def test_pushname_com_digito_nao_e_primeiro_nome():
    assert primeiro_nome("Jo4o") is None


def test_pushname_com_hifen_ou_apostrofo_e_nome_de_verdade():
    """Nome de verdade com hífen/apóstrofo entre partes não pode ser
    penalizado pela blindagem contra pushName malicioso."""
    assert primeiro_nome("Ana-Maria Silva") == "Ana-Maria"
    assert primeiro_nome("O'Brien") == "O'Brien"


def test_pushname_muito_longo_nao_vaza():
    assert primeiro_nome("a" * 50) is None


# --- `iniciar_followup`: a task no worker (Fase 3, Task 4) ----------------
#
# `FOLLOWUP_ENABLED=false` por padrão é sagrado: subir o worker não pode,
# por si só, começar a mandar WhatsApp para lead nenhum. Estes testes usam
# `capture_logs` (não `caplog`) porque `structlog.get_logger()` sem
# `setup_logging()` (nunca chamada nos testes) usa `PrintLoggerFactory` —
# escreve direto em stdout, fora do módulo `logging` padrão que `caplog`
# intercepta. `structlog.testing.capture_logs()` funciona independente da
# configuração global.


async def test_followup_desligado_por_padrao_nao_sobe(monkeypatch):
    monkeypatch.setattr(settings, "followup_enabled", False)
    pool = await get_pool()
    tarefa = iniciar_followup(
        pool, outbounds={MessagingChannel.EVOLUTION: _ClienteMudo()}
    )
    assert tarefa is None


async def test_sem_cliente_evolution_nao_sobe(monkeypatch):
    monkeypatch.setattr(settings, "followup_enabled", True)
    pool = await get_pool()
    with structlog.testing.capture_logs() as logs:
        tarefa = iniciar_followup(
            pool, outbounds={MessagingChannel.META: _ClienteMudo()}
        )
    assert tarefa is None
    assert any(evento["event"] == "followup_sem_canal" for evento in logs)


async def test_em_modo_mock_a_regua_sobe_e_roda(monkeypatch):
    """Documenta a surpresa: em `OUTBOUND_MODE=mock`, `channel_status()`
    marca todo canal como completo e `_build_outbound_clients` instancia
    todos — então "tem cliente Evolution" é vacuamente verdadeiro, e com
    `FOLLOWUP_ENABLED=true` a régua SOBE E RODA DE VERDADE contra o cliente
    mock em dev. Inofensivo (o mock não manda WhatsApp nenhum), mas é
    surpresa.
    """
    chamou = asyncio.Event()

    async def _rodada_stub(pool, cliente):
        chamou.set()
        return {"enviados": 0, "falhas": 0, "abortados": 0, "bloqueados_por_janela": 0}

    monkeypatch.setattr("whatsapp_langchain.worker.main.rodada", _rodada_stub)
    monkeypatch.setattr(settings, "followup_enabled", True)
    monkeypatch.setattr(settings, "followup_interval_seconds", 60)

    pool = await get_pool()
    tarefa = iniciar_followup(
        pool, outbounds={MessagingChannel.EVOLUTION: _ClienteMudo()}
    )
    try:
        assert tarefa is not None
        await asyncio.wait_for(chamou.wait(), timeout=2)
    finally:
        await _parar_followup(tarefa)


async def test_excecao_numa_rodada_nao_derruba_o_loop(monkeypatch):
    """Sem o `try/except` por rodada, um erro de banco mata a régua até o
    próximo deploy — e "régua parada" e "ninguém elegível" ficam
    indistinguíveis de fora, porque as duas dão zero envio."""
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

    pool = await get_pool()
    tarefa = iniciar_followup(
        pool, outbounds={MessagingChannel.EVOLUTION: _ClienteMudo()}
    )
    assert tarefa is not None
    await asyncio.wait_for(terceira.wait(), timeout=5)
    await _parar_followup(tarefa)

    assert len(chamadas) >= 3, "o loop tem que sobreviver à primeira exceção"


async def test_parar_followup_cancela_task_pendurada():
    """Task de background esquecida no shutdown polui as rodadas seguintes
    da suíte (e, em produção, segue rodando depois do processo achar que já
    parou). `_parar_followup` isola esse cancelamento para ser testável sem
    precisar rodar `main()` inteiro.

    Propositalmente NÃO envolve `_parar_followup(tarefa)` num
    `asyncio.wait_for` com timeout: `Task.cancel()` repassa o cancelamento
    para o que a task está aguardando no momento (`_fut_waiter`) — se
    `_parar_followup` estiver suspensa em `await task` quando o `wait_for`
    externo estoura o timeout e cancela ESSA chamada, o cancelamento vaza
    para dentro de `tarefa` de qualquer jeito, mesmo com o `task.cancel()`
    de produção removido. Isso mascara exatamente a mutação que este teste
    precisa pegar. Por isso o cancelamento roda como task separada e o
    teste faz polling curto e limitado — sem nunca cancelar nada por fora.
    """
    rodou = asyncio.Event()

    async def _loop_infinito():
        while True:
            rodou.set()
            await asyncio.sleep(0.01)

    tarefa = asyncio.create_task(_loop_infinito())
    parar_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(rodou.wait(), timeout=2)

        parar_task = asyncio.create_task(_parar_followup(tarefa))
        for _ in range(100):
            if parar_task.done():
                break
            await asyncio.sleep(0.01)

        assert parar_task.done(), (
            "_parar_followup nunca retornou — sem cancel(), fica preso no await"
        )
        assert tarefa.done()
        assert tarefa.cancelled()
    finally:
        for t in (parar_task, tarefa):
            if t is not None and not t.done():
                t.cancel()
        for t in (parar_task, tarefa):
            if t is not None:
                try:
                    await t
                except BaseException:
                    pass


async def test_parar_followup_com_none_nao_faz_nada():
    await _parar_followup(None)
