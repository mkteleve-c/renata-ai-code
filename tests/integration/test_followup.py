"""Régua de follow-up: reivindicação atômica, escada ancorada no inbound e janela de
24h.

**Um envio indevido aqui é WhatsApp para gente de verdade** — por isso os
testes de risco mais alto (concorrência real e janela fechada) são
propositalmente difíceis de escrever de forma honesta. Ver os comentários em
cada um.
"""

import asyncio

import pytest_asyncio

from whatsapp_langchain.agents.catalog.elevec_sdr.tools.crm import (
    reverter_fase_apos_cancelamento,
)
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.worker import followup
from whatsapp_langchain.worker.evolution_client import EvolutionSendError
from whatsapp_langchain.worker.followup import (
    _FATOR_SOBRA_BUSCA,
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
    await lead_factory("5511900000001", followup_count=0, minutos_desde_inbound=10)
    await lead_factory("5511900000002", followup_count=0, minutos_desde_inbound=20)
    await lead_factory("5511900000003", followup_count=1, minutos_desde_inbound=80)
    await lead_factory(
        "5511900000004", followup_count=2, minutos_desde_inbound=23 * 60 + 5
    )

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
    await lead_factory(
        "5511900000005",
        followup_count=1,
        minutos_desde_interacao=600,
        minutos_desde_inbound=30,
    )
    assert await _reivindicar() == []


async def test_lead_sem_last_inbound_at_nunca_e_reivindicado(lead_factory):
    """NULL é seguro por construção — leads importados não recebem nada."""
    await lead_factory(
        "5511900000006",
        followup_count=0,
        minutos_desde_interacao=600,
        minutos_desde_inbound=None,
    )
    assert await _reivindicar() == []


async def test_janela_fechada_nao_reivindica_e_nao_queima_nivel(lead_factory):
    await lead_factory(
        "5511900000020", followup_count=2, minutos_desde_inbound=25 * 60
    )  # janela fechou
    irmao = await lead_factory(
        "5511900000021", followup_count=2, minutos_desde_inbound=23 * 60 + 5
    )  # dentro

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
        await lead_factory(
            f"551190000003{i}", phase=fase, followup_count=0, minutos_desde_inbound=60
        )
    assert await _reivindicar() == []


async def test_agente_pausado_nao_e_reivindicado(lead_factory):
    """`agent_active=False` (handover humano em curso) não pode ser perseguido,
    mesmo com fase e relógio elegíveis para o degrau."""
    await lead_factory(
        "5511900000035",
        agent_active=False,
        followup_count=0,
        minutos_desde_inbound=60,
    )
    assert await _reivindicar() == []


async def test_lead_com_followup_active_false_nao_e_reivindicado(lead_factory):
    """Mesma classe da checagem de `agent_active` acima, mas para
    `followup_active` — caminho independente de desligamento da régua."""
    await lead_factory(
        "5511900000036",
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

    `_SQL_CANDIDATOS` busca `limite * _FATOR_SOBRA_BUSCA` candidatos (sobra
    para capturar pares/trios duplicados inteiros — ver o comentário no
    módulo) e trava TODOS via `FOR UPDATE SKIP LOCKED`, mesmo que só
    `limite` identidades acabem avançando. Por isso o lote de leads aqui
    precisa ser maior que `limite_busca` de uma única chamada — senão a
    primeira transação travaria o pool inteiro sozinha e a segunda não
    teria candidato nenhum para pegar, o que provaria o teste errado
    (ausência de disputa, não ausência de duplicata)."""
    limite = 3
    limite_busca = limite * _FATOR_SOBRA_BUSCA
    total_leads = limite_busca + limite + 5
    for i in range(total_leads):
        await lead_factory(
            f"5511900010{i:03d}", followup_count=0, minutos_desde_inbound=30
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


async def test_par_duplicado_e_reivindicado_uma_unica_vez(lead_factory):
    """`phone` não é identidade — gate, blocklist e `variacoes()` tratam a
    mesma pessoa com/sem o 9º dígito como um par; a reivindicação, antes
    deste fix, não. Reproduzido ao vivo: o par abaixo, sem dedup, saía
    inteiro do mesmo lote e a mesma pessoa recebia a mesma mensagem duas
    vezes. Duplicata é realidade documentada desta base (~155 casos, ver
    a especificação), alcançável tanto pelo backfill da 013 quanto pelo
    caminho `agente_desligado` do gate — nenhum dos dois deduplica.

    Só UM lado é devolvido para envio — mas os DOIS avançam
    `followup_count` juntos (ver
    `test_par_duplicado_nao_manda_mensagem_de_novo_na_proxima_rodada` para
    o porquê: sem isso, o lado que não avança fica desbloqueado e
    reivindicado sozinho na rodada seguinte).
    """
    sem_9 = await lead_factory(
        "551187654321", followup_count=0, minutos_desde_inbound=30
    )
    com_9 = await lead_factory(
        "5511987654321", followup_count=0, minutos_desde_inbound=30
    )

    reivindicados = {r.phone for r in await _reivindicar()}

    achados = reivindicados & {sem_9, com_9}
    assert achados, "nenhum dos dois lados do par foi reivindicado"
    assert len(achados) == 1, (
        "os DOIS lados do mesmo par foram reivindicados no mesmo lote — a "
        "mesma pessoa receberia a mesma mensagem duas vezes"
    )

    # o lado de fora não é enviado, mas avança followup_count junto.
    de_fora = com_9 if achados == {sem_9} else sem_9
    assert await _followup_count(de_fora) == 1


async def test_par_duplicado_com_last_inbound_at_identico_ainda_desempata(lead_factory):
    """O desempate do dedup é pela tupla `(last_inbound_at, phone)`, não só
    por `last_inbound_at` — o backfill da 013 grava a MESMA marca de tempo
    nas duas metades do par. Com empate exato e desempate só por
    timestamp, nenhum dos dois lados venceria o `not exists` e os DOIS
    passariam — a mesma duplicidade que o dedup existe para fechar."""
    pool = await get_pool()
    sem_9 = await lead_factory(
        "551187654323", followup_count=0, minutos_desde_inbound=30
    )
    com_9 = await lead_factory(
        "5511987654323", followup_count=0, minutos_desde_inbound=30
    )
    async with pool.connection() as conn:
        await conn.execute(
            "update leads_crm set last_inbound_at = "
            "(select last_inbound_at from leads_crm where phone = %s) "
            "where phone = %s",
            (sem_9, com_9),
        )
        await conn.commit()

    achados = {r.phone for r in await _reivindicar()} & {sem_9, com_9}
    assert len(achados) == 1, achados


async def test_rodada_nao_manda_duas_mensagens_para_o_mesmo_par_duplicado(lead_factory):
    """Mesmo cenário acima, mas pela `rodada` inteira — a forma como o
    Crítico foi reproduzido de verdade: `{'enviados': 2}` para o mesmo par."""
    await lead_factory("551187654322", followup_count=0, minutos_desde_inbound=30)
    await lead_factory("5511987654322", followup_count=0, minutos_desde_inbound=30)

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    resumo = await rodada(await get_pool(), ClienteQueRegistra())
    assert resumo["enviados"] == 1
    assert len(enviados) == 1


async def test_par_duplicado_nao_manda_mensagem_de_novo_na_proxima_rodada(lead_factory):
    """O dedup por si só só ADIA a duplicata um round: o vencedor avança
    `followup_count` e deixa de casar o predicado de elegibilidade — o
    irmão, que nunca avançou, continua elegível e é reivindicado sozinho
    na rodada seguinte. Com a Task 4 rodando a cada 5 minutos, as duas
    mensagens saem quase coladas. Reproduzido ao vivo com o par
    `551197755555`/`5511997755555`.

    O fix: o claim avança `followup_count` de TODAS as linhas que
    compartilham a chave canônica, não só do vencedor — o par anda em
    bloco. Por isso este teste chama `rodada` duas vezes seguidas: nenhum
    teste de uma rodada só prova isso.
    """
    sem_9 = await lead_factory(
        "551197755555", followup_count=0, minutos_desde_inbound=30
    )
    com_9 = await lead_factory(
        "5511997755555", followup_count=0, minutos_desde_inbound=30
    )

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    resumo1 = await rodada(await get_pool(), ClienteQueRegistra())
    assert resumo1["enviados"] == 1

    resumo2 = await rodada(await get_pool(), ClienteQueRegistra())
    assert resumo2["enviados"] == 0, (
        "a segunda rodada não pode mandar mensagem pro irmão que ficou de "
        "fora da primeira — o par tem que ter avançado followup_count "
        "junto na primeira rodada"
    )
    assert len(enviados) == 1

    assert await _followup_count(sem_9) == 1
    assert await _followup_count(com_9) == 1


async def test_par_duplicado_no_degrau_2_tambem_nao_duplica_entre_rodadas(lead_factory):
    """Mesma prova acima, mas partindo do degrau 2 (`followup_count=1`) —
    o brief da revisão mediu que uma mutação que restringe o avanço em
    bloco só ao degrau 1 (`and irmao.followup_count = 0`) sobrevive à
    suíte inteira se nenhum teste exercitar outro degrau."""
    sem_9 = await lead_factory(
        "551197712377", followup_count=1, minutos_desde_inbound=80
    )
    com_9 = await lead_factory(
        "5511997712377", followup_count=1, minutos_desde_inbound=80
    )

    enviados = []

    class ClienteQueRegistra:
        async def send_message(self, to, body, **kwargs):
            enviados.append(to)
            return "id"

    resumo1 = await rodada(await get_pool(), ClienteQueRegistra())
    assert resumo1["enviados"] == 1

    resumo2 = await rodada(await get_pool(), ClienteQueRegistra())
    assert resumo2["enviados"] == 0
    assert len(enviados) == 1

    assert await _followup_count(sem_9) == 2
    assert await _followup_count(com_9) == 2


async def test_trio_duplicado_incluindo_forma_legada_e_reivindicado_uma_vez(
    lead_factory,
):
    """A especificação registra casos reais de três linhas para a mesma
    pessoa. A terceira forma (`55011...`, zero de tronco) é exatamente o
    perfil dos ~26 malformados que `phone.py` documenta — uma regex SQL
    reimplementada não cobria isso; `canonicalizar()` (reaproveitado em
    Python) cobre.

    Limpeza manual da forma malformada no fim: o teardown padrão de
    `lead_factory` apaga `variacoes(canonicalizar(phone))`, que para
    `"55011997712399"` dá `("5511997712399", "551197712399")` — a própria
    string malformada não está nessas duas variações e vazaria para a
    próxima suíte.
    """
    a = await lead_factory("551197712399", followup_count=0, minutos_desde_inbound=30)
    b = await lead_factory("5511997712399", followup_count=0, minutos_desde_inbound=30)
    c = "55011997712399"
    await lead_factory(c, followup_count=0, minutos_desde_inbound=30)

    try:
        reivindicados = await _reivindicar()
        achados = {r.phone for r in reivindicados} & {a, b, c}
        assert len(achados) == 1, achados

        assert await _followup_count(a) == 1
        assert await _followup_count(b) == 1
        assert await _followup_count(c) == 1
    finally:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("delete from leads_crm where phone = %s", (c,))
            await conn.commit()


async def test_telefone_na_blocklist_nao_e_reivindicado(lead_factory, bloquear):
    """A régua é o único caminho do sistema que fala sem o lead ter
    falado — é onde o opt-out mais importa, e nem `reivindicar` nem
    `ainda_vale_enviar` consultavam `blocklist` até este fix (só o gate
    consultava). Reproduzido: telefone bloqueado com linha elegível em
    `leads_crm` → `rodada` → mensagem entregue a quem pediu para parar."""
    phone = await lead_factory(
        "5511900000140", followup_count=0, minutos_desde_inbound=30
    )
    await bloquear(phone)

    assert await _reivindicar() == []

    resumo = await rodada(await get_pool(), _ClienteMudo())
    assert resumo["enviados"] == 0


async def test_telefone_bloqueado_por_variacao_nao_e_reivindicado(
    lead_factory, bloquear
):
    """A blocklist pode ter a OUTRA forma (com/sem o 9º dígito) do mesmo
    telefone — o `CHECK` da blocklist aceita as duas, igual ao gate."""
    await lead_factory("551190000141", followup_count=0, minutos_desde_inbound=30)
    await bloquear("5511990000141")  # forma com o 9º dígito

    assert await _reivindicar() == []


async def test_ainda_vale_enviar_falso_para_telefone_bloqueado(lead_factory, bloquear):
    phone = await lead_factory(
        "5511900000142",
        followup_count=1,
        minutos_desde_inbound=5,
        minutos_desde_interacao=0,
    )
    await bloquear(phone)

    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_par_com_um_lado_pausado_prova_o_ramo_de_timestamp_alcancavel(
    lead_factory,
):
    """O achado da rodada anterior não sumiu — trocou de direção: a
    docstring de `ainda_vale_enviar` afirmava que o ramo de timestamp era
    inalcançável hoje, mas um par duplicado com um lado pausado (`com_9`,
    `agent_active=False`) e outro ativo (`sem_9`, já reivindicado) chega
    lá. Quando `sem_9` recebe um inbound novo, `aplicar_gate` funde as
    duas linhas e a pausa vence (`_vencedor_pausa`) — o gate cai no ramo
    `agente_desligado`, que bumpa `last_inbound_at` das DUAS linhas físicas
    sem tocar `agent_active`/`followup_active`/`followup_count` de
    `sem_9`. As quatro checagens anteriores de `ainda_vale_enviar` passam
    limpo nela; só a comparação de relógios pega.
    """
    sem_9 = "551197799911"
    com_9 = "5511997799911"
    await lead_factory(
        sem_9, agent_active=True, followup_count=0, minutos_desde_inbound=30
    )
    await lead_factory(
        com_9, agent_active=False, followup_count=0, minutos_desde_inbound=30
    )

    reivindicados = await _reivindicar()
    assert {r.phone for r in reivindicados} == {sem_9}

    await aplicar_gate(
        await get_pool(),
        key={"remoteJid": f"{sem_9}@s.whatsapp.net", "fromMe": False},
        push_name="Fulano",
    )

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active, followup_active, followup_count"
            " from leads_crm where phone = %s",
            (sem_9,),
        )
        linha = await cur.fetchone()
    assert linha is not None
    agent_active, followup_active, followup_count = linha
    assert agent_active is True, "as quatro checagens anteriores passam limpo"
    assert followup_active is True
    assert followup_count == 1

    assert await ainda_vale_enviar(pool, sem_9, nivel=1) is False


async def test_lead_que_falou_entre_o_claim_e_o_envio_nao_recebe(lead_factory):
    """O claim commita e só então o HTTP acontece. Nesse intervalo o lead pode
    ter escrito — e mandar "Fulano?" em cima da mensagem dele é o pior caso.

    O telefone usa a forma canônica estável (sem o 9º dígito) de propósito:
    com um telefone "com o 9", o caminho normal de `aplicar_gate` RENOMEIA a
    linha (`update ... set phone = sem_9 ... where phone = com_9`), e o
    `ainda_vale_enviar` cairia no ramo "lead sumiu" — provando um bug
    diferente do que este teste existe para travar. É a mesma armadilha do
    telefone em `TELEFONE_CANONICO_ESTAVEL`.
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


async def test_ainda_vale_enviar_falso_para_lead_inexistente():
    assert await ainda_vale_enviar(await get_pool(), "5511900000099", 1) is False


async def test_ainda_vale_enviar_falso_quando_nivel_nao_bate_mais(lead_factory):
    phone = await lead_factory(
        "5511900000090",
        followup_count=2,
        minutos_desde_inbound=5,
        minutos_desde_interacao=0,
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_ainda_vale_enviar_falso_quando_followup_active_false(lead_factory):
    phone = await lead_factory(
        "5511900000091",
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
        "5511900000092",
        followup_count=1,
        agent_active=False,
        minutos_desde_inbound=5,
        minutos_desde_interacao=0,
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_ainda_vale_enviar_falso_quando_inbound_passa_do_instante_do_claim(
    lead_factory,
):
    """Guarda defensiva para um caminho de escrita FUTURO — hoje nenhum
    caminho real do gate chega neste ramo (ver o docstring de
    `ainda_vale_enviar`): o `agente_desligado` já cai em `agent_active`
    acima, e o caminho normal zera `followup_count` no mesmo `now()` em
    que grava `last_inbound_at`. Construído direto no banco para testar o
    ramo isoladamente, sem depender de nenhum caminho real ficar
    inalcançável.
    """
    phone = await lead_factory(
        "5511900000094",
        followup_count=1,
        minutos_desde_interacao=10,  # "instante do claim", 10 min atrás
        minutos_desde_inbound=1,  # last_inbound_at mais recente que isso
    )
    assert await ainda_vale_enviar(await get_pool(), phone, nivel=1) is False


async def test_ainda_vale_enviar_verdadeiro_quando_nada_mudou(lead_factory):
    phone = await lead_factory(
        "5511900000093",
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
    p1 = await lead_factory("5511900000101", followup_count=0, minutos_desde_inbound=30)
    p2 = await lead_factory("5511900000102", followup_count=0, minutos_desde_inbound=30)
    p3 = await lead_factory("5511900000103", followup_count=0, minutos_desde_inbound=30)

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
        "5511900000110", followup_count=0, minutos_desde_inbound=30
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
        "5511900000111",
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
