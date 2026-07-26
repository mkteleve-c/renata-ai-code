"""Webhook da Evolution: do payload até a fila."""

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from whatsapp_langchain.server.dependencies import request_history
from whatsapp_langchain.server.main import app
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool

TELEFONE = "551166665555"
AGENTE = "illumi_assistant"
OUTRO_AGENTE = "rhawk_assistant"
JID = "5511966665555@s.whatsapp.net"


def payload(
    texto="Olá",
    from_me=False,
    remote_jid=JID,
    message_id="MSG1",
):
    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": message_id},
            "pushName": "Fulano",
            "messageType": "conversation",
            "message": {"conversation": texto},
        },
    }


def payload_midia(remote_jid=JID, message_id="MSG-MEDIA", com_url=True):
    imagem: dict[str, Any] = {"caption": "Olha essa foto"}
    if com_url:
        imagem["url"] = "https://mmg.whatsapp.net/criptografado"

    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": False, "id": message_id},
            "pushName": "Fulano",
            "messageType": "imageMessage",
            "message": {"imageMessage": imagem},
        },
    }


def payload_tipo(message_type: str, message: dict[str, Any], message_id="MSG-TIPO"):
    return {
        "event": "messages.upsert",
        "instance": "instancia-apioficial",
        "data": {
            "key": {"remoteJid": JID, "fromMe": False, "id": message_id},
            "pushName": "Fulano",
            "messageType": message_type,
            "message": message,
        },
    }


async def _apagar_rastro() -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from leads_crm where phone = %s", (TELEFONE,))
        await conn.execute(
            "delete from message_queue where phone_number = %s", (f"+{TELEFONE}",)
        )
    # request_history é módulo-level e sobrevive entre testes — sem zerar,
    # a soma dos POSTs deste arquivo estoura RATE_LIMIT_PER_HOUR (30).
    request_history.pop(f"+{TELEFONE}", None)


@pytest.fixture
async def limpar():
    """Zera lead, fila e rate limit do telefone de teste, antes e depois.

    O teardown importa: sem ele uma linha com message_id repetido sobrevive
    para o teste seguinte e a deduplicação por (channel, message_id) faz o
    próximo enqueue devolver `duplicata` em vez de enfileirar.
    """
    await _apagar_rastro()
    yield
    await _apagar_rastro()


async def cliente():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://teste")


async def linhas_na_fila(colunas: str = "*") -> list[Any]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"select {colunas} from message_queue where phone_number = %s "
            "order by id asc",
            (f"+{TELEFONE}",),
        )
        return await cur.fetchall()


async def unica_linha(colunas: str = "*") -> Any:
    linhas = await linhas_na_fila(colunas)
    assert len(linhas) == 1, f"esperava 1 linha na fila, veio {len(linhas)}"
    return linhas[0]


async def lead_do_teste(colunas: str = "*") -> Any | None:
    """Linha de leads_crm do telefone de teste, ou None se não existe.

    `xmin` é o id da transação que escreveu a versão atual da linha: se um
    UPDATE roda, ele muda mesmo que nenhuma coluna mude de valor. É como se
    observa "o UPDATE não foi refeito" sem depender de coluna de auditoria,
    que leads_crm não tem.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"select {colunas} from leads_crm where phone = %s", (TELEFONE,)
        )
        return await cur.fetchone()


async def test_mensagem_valida_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("channel, incoming_message")
    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


async def test_from_me_nao_entra_na_fila(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload(from_me=True)
        )

    assert r.json()["status"] == "ignorado"
    assert r.json()["motivo"] == "from_me"
    assert await linhas_na_fila("id") == []


async def test_grupo_e_ignorado(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(remote_jid="12345-67890@g.us"),
        )

    assert r.json()["status"] == "ignorado"
    assert r.json()["motivo"] == "telefone_invalido"


async def test_lid_e_descartado(limpar):
    """`@lid` de 12 dígitos começando em 55 casaria com a chave de um lead real."""
    lid = "551188654321"
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "delete from leads_crm where phone in (%s, %s)", (lid, "5511988654321")
        )
        await conn.execute(
            "delete from message_queue where phone_number = %s", (f"+{lid}",)
        )
    request_history.pop(f"+{lid}", None)

    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(remote_jid="551188654321@lid", message_id="MSG-LID"),
        )

    assert r.json()["motivo"] == "telefone_invalido"

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select 1 from leads_crm where phone in (%s, %s)",
            (lid, "5511988654321"),
        )
        leads = await cur.fetchone()
        cur = await conn.execute(
            "select 1 from message_queue where phone_number = %s", (f"+{lid}",)
        )
        na_fila = await cur.fetchone()
        await conn.execute(
            "delete from leads_descartados where phone_original = %s", (f"{lid}@lid",)
        )

    assert leads is None, "LID não pode virar lead"
    assert na_fila is None, "LID não pode virar linha na fila"


async def test_descarte_retem_agente_e_instancia(limpar):
    """Sem `agent` e `instance` o descarte não é reprocessável.

    O `agent` vem da query string e o `instance` do topo do payload — nenhum
    dos dois está dentro de `data`, que era tudo que ia para a tabela.
    """
    jid = "999888777666555@lid"
    async with await cliente() as c:
        await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(remote_jid=jid, message_id="MSG-DESCARTE-CTX"),
        )

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select payload from leads_descartados where phone_original = %s", (jid,)
        )
        linha = await cur.fetchone()
        await conn.execute(
            "delete from leads_descartados where phone_original = %s", (jid,)
        )

    assert linha is not None
    retido = linha[0]
    assert retido["agent"] == AGENTE
    assert retido["instance"] == "instancia-apioficial"
    assert retido["data"]["key"]["id"] == "MSG-DESCARTE-CTX"


async def test_evento_nao_mensagem_e_ignorado(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json={"event": "connection.update", "instance": "x", "data": {}},
        )

    assert r.status_code == 200
    assert r.json()["status"] == "ignorado"


async def test_agente_inexistente_responde_200(limpar):
    """Typo no `?agent=` é erro de configuração — 400 põe a Evolution em loop.

    A reentrega nunca vai melhorar: o mesmo POST com o mesmo agente errado
    volta indefinidamente. 200 + log ruidoso corta o loop.
    """
    async with await cliente() as c:
        r = await c.post("/webhook/evolution?agent=nao_existe", json=payload())

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ignorado", "motivo": "agente_desconhecido"}
    assert await linhas_na_fila("id") == []


async def test_json_invalido_responde_200(limpar):
    """Body malformado não melhora em retry — 400 vira loop de reentrega."""
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            content=b'{"event": "messages.upsert", "data": {',
            headers={"content-type": "application/json"},
        )

    assert r.status_code == 200, r.text
    assert r.json()["motivo"] == "json_invalido"


async def test_evento_messages_sem_sufixo_e_aceito(limpar):
    body = payload()
    body["event"] = "messages"

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=body)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("channel, incoming_message")
    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


@pytest.mark.parametrize(
    "nome_do_evento", ["MESSAGES_UPSERT", "messages-upsert", "Messages.Upsert"]
)
async def test_variacao_do_nome_do_evento_e_aceita(limpar, nome_do_evento):
    body = payload()
    body["event"] = nome_do_evento

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert len(await linhas_na_fila("id")) == 1


async def test_mensagem_com_midia_grava_provider_message_key(limpar):
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload_midia())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("provider_message_key, media_url, media_type")
    assert linha[0] == {
        "remoteJid": JID,
        "fromMe": False,
        "id": "MSG-MEDIA",
    }
    assert linha[1] == "https://mmg.whatsapp.net/criptografado"
    assert linha[2] == "image/jpeg"


async def test_reacao_nao_invoca_o_agente(limpar):
    corpo = payload_tipo(
        "reactionMessage",
        {"reactionMessage": {"text": "👍", "key": {"id": "MSG1"}}},
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json() == {"status": "ignorado", "motivo": "conteudo_nao_suportado"}
    assert await linhas_na_fila("id") == []


async def test_reacao_de_lead_nao_cria_lead(limpar):
    """O guard tem que cortar antes do gate, que escreve em leads_crm."""
    corpo = payload_tipo(
        "reactionMessage",
        {"reactionMessage": {"text": "👍", "key": {"id": "MSG1"}}},
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.json()["motivo"] == "conteudo_nao_suportado"
    assert await lead_do_teste("phone") is None, "um emoji não pode criar lead"


async def test_reacao_de_lead_nao_altera_lead_existente(limpar):
    async with await cliente() as c:
        await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    antes = await lead_do_teste("xmin::text, followup_count, last_interaction_at")
    assert antes is not None

    corpo = payload_tipo(
        "reactionMessage",
        {"reactionMessage": {"text": "👍", "key": {"id": "MSG-REACAO"}}},
    )
    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.json()["motivo"] == "conteudo_nao_suportado"
    depois = await lead_do_teste("xmin::text, followup_count, last_interaction_at")
    assert depois == antes, "reação não pode renovar last_interaction_at"


async def test_mensagem_apagada_nao_invoca_o_agente(limpar):
    corpo = payload_tipo(
        "protocolMessage",
        {"protocolMessage": {"type": "REVOKE", "key": {"id": "MSG1"}}},
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.json()["motivo"] == "conteudo_nao_suportado"
    assert await linhas_na_fila("id") == []


async def test_sticker_vira_marcador_de_texto_sem_chamada_multimodal(limpar):
    """Figurinha não vale uma chamada de visão ao LLM.

    Tratada como mídia, cada figurinha virava download + descrição
    multimodal — custo por sticker para descrever uma figurinha, e webp
    animado provavelmente nem seria aceito pelo modelo. Vira marcador de
    texto: o agente sabe que o lead mandou uma figurinha, responde no fluxo,
    e o gate roda normalmente.
    """
    corpo = payload_tipo(
        "stickerMessage",
        {
            "stickerMessage": {
                "url": "https://mmg.whatsapp.net/sticker",
                "mimetype": "image/webp",
                "isAnimated": True,
            }
        },
        message_id="MSG-STICKER",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha(
        "media_url, media_type, incoming_message, provider_message_key"
    )
    assert linha[0] is None
    assert linha[1] is None
    assert linha[2] == "[figurinha]"
    assert linha[3] is None


async def test_mimetype_do_payload_prevalece_sobre_o_padrao(limpar):
    """Todo nó de mídia Baileys carrega `mimetype` — inferir é desnecessário."""
    corpo = payload_tipo(
        "audioMessage",
        {"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": 3}},
        message_id="MSG-PTT",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200, r.text
    linha = await unica_linha("media_type")
    assert linha[0] == "audio/ogg; codecs=opus"


async def test_mimetype_de_imagem_nao_e_forcado_para_jpeg(limpar):
    corpo = payload_tipo(
        "imageMessage",
        {"imageMessage": {"mimetype": "image/png", "url": "https://mmg/x.enc"}},
        message_id="MSG-PNG",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200, r.text
    linha = await unica_linha("media_type")
    assert linha[0] == "image/png"


async def test_mimetype_ausente_cai_no_campo_e_nao_no_messagetype(limpar):
    """O envelope declara `documentMessage`; a mídia de dentro é imagem.

    Inferir pelo `messageType` devolvia `application/octet-stream` — que o
    preprocessor classifica como não suportada — para uma foto que o lead
    mandou com legenda.
    """
    corpo = payload_tipo(
        "documentMessage",
        {
            "documentWithCaptionMessage": {
                "message": {"imageMessage": {"caption": "planta baixa"}}
            }
        },
        message_id="MSG-SEM-MIME",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200, r.text
    linha = await unica_linha("media_type")
    assert linha[0] == "image/jpeg"


async def test_audio_sem_url_entra_na_fila_como_midia(limpar):
    """Na Evolution a URL é inútil — quem baixa é a provider_message_key.

    Descartar mídia sem URL perderia áudio de lead em silêncio, com 200 e
    sem reentrega.
    """
    corpo = payload_tipo(
        "audioMessage",
        {"audioMessage": {"seconds": 3}},
        message_id="MSG-AUDIO",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("media_url, media_type, provider_message_key")
    assert linha[0] is None
    assert linha[1] == "audio/ogg"
    assert linha[2] == {"remoteJid": JID, "fromMe": False, "id": "MSG-AUDIO"}


async def test_midia_com_legenda_preserva_legenda_e_via_de_download(limpar):
    """A legenda é preservada JUNTO com os campos de mídia, não no lugar deles.

    Decisão consciente (fix round 3): a mensagem vai para o branch de mídia
    mesmo tendo texto. O que o branch de texto faria seria gravar media_url e
    media_type nulos, e uma linha assim é indistinguível de texto puro —
    nem o worker de hoje nem o da Task 9 conseguiriam recuperar a imagem, e
    o agente responderia a uma legenda sobre uma foto que ninguém viu.

    No branch de mídia nada do que o lead escreveu se perde: a legenda fica
    em `incoming_message`, ao lado de `media_type` e da key. A entrega dessa
    legenda ao agente depende de `preprocess_incoming_message`
    (`worker/media.py:244`), que hoje corta em `not media_url` — é a linha
    que a Task 9 precisa mudar de qualquer forma para o download por key
    funcionar.
    """
    corpo = payload_tipo(
        "imageMessage",
        {"imageMessage": {"caption": "olha isso"}},
        message_id="MSG-IMG-SEM-URL",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha(
        "media_url, media_type, incoming_message, provider_message_key"
    )
    assert linha[0] is None
    assert linha[1] == "image/jpeg"
    assert linha[2] == "olha isso"
    assert linha[3] == {"remoteJid": JID, "fromMe": False, "id": "MSG-IMG-SEM-URL"}


async def test_mensagem_sem_campo_message_e_ignorada(limpar):
    corpo = payload()
    del corpo["data"]["message"]

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["motivo"] == "conteudo_nao_suportado"
    assert await linhas_na_fila("id") == []


async def test_midia_dentro_de_envelope_view_once(limpar):
    corpo = payload_tipo(
        "viewOnceMessageV2",
        {
            "viewOnceMessageV2": {
                "message": {
                    "imageMessage": {
                        "url": "https://mmg.whatsapp.net/vo",
                        "caption": "some depois",
                    }
                }
            }
        },
        message_id="MSG-VO",
    )

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("media_url, media_type, incoming_message")
    assert linha[0] == "https://mmg.whatsapp.net/vo"
    assert linha[1] == "image/jpeg"
    assert linha[2] == "some depois"


async def test_data_como_lista_e_processada(limpar):
    corpo = payload()
    corpo["data"] = [corpo["data"]]

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=corpo)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    linha = await unica_linha("channel, incoming_message")
    assert linha[0] == "evolution"
    assert linha[1] == "Olá"


async def test_payload_que_nao_e_objeto_nao_da_500(limpar):
    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=["nao", "e", "isso"]
        )

    assert r.status_code == 200
    assert r.json()["motivo"] == "payload_invalido"


async def test_reentrega_de_texto_nao_duplica_a_fila(limpar):
    async with await cliente() as c:
        primeira = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())
        segunda = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert primeira.json()["status"] == "ok"
    assert segunda.status_code == 200
    assert segunda.json()["motivo"] == "duplicata"
    assert segunda.json()["queue_id"] == primeira.json()["queue_id"]

    # Sem dedupe, a reentrega dentro da janela de debounce concatenaria o
    # mesmo texto na mesma linha.
    linha = await unica_linha("incoming_message")
    assert linha[0] == "Olá"


async def test_reentrega_de_midia_nao_duplica_a_fila(limpar):
    async with await cliente() as c:
        primeira = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload_midia()
        )
        segunda = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload_midia()
        )

    assert primeira.json()["status"] == "ok"
    assert segunda.json()["motivo"] == "duplicata"
    assert len(await linhas_na_fila("id")) == 1


async def test_mesmo_message_id_em_dois_agentes_gera_duas_linhas(limpar):
    """Multi-agente é mecanismo do template, não reentrega.

    O mesmo payload em ?agent=a e ?agent=b são duas mensagens legítimas.
    Com a chave de dedupe sem agent_id, a segunda virava "duplicata" e
    devolvia o queue_id do primeiro agente.
    """
    async with await cliente() as c:
        primeira = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())
        segunda = await c.post(
            f"/webhook/evolution?agent={OUTRO_AGENTE}", json=payload()
        )

    assert primeira.json()["status"] == "ok"
    assert segunda.json()["status"] == "ok", segunda.text
    assert segunda.json()["queue_id"] != primeira.json()["queue_id"]

    linhas = await linhas_na_fila("agent_id, message_id")
    assert len(linhas) == 2
    assert {linha[0] for linha in linhas} == {AGENTE, OUTRO_AGENTE}
    assert {linha[1] for linha in linhas} == {"MSG1"}


async def test_from_me_repetido_nao_reescreve_o_lead(limpar):
    """Rajada de fromMe é o único caminho destrutivo sem rate limit.

    O primeiro fromMe desliga o agente; do segundo em diante o UPDATE seria
    no-op e não deve ser executado.
    """
    async with await cliente() as c:
        await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

        primeiro = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(from_me=True, message_id="HUMANO-1"),
        )
        apos_primeiro = await lead_do_teste("xmin::text, agent_active")

        segundo = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(from_me=True, message_id="HUMANO-2"),
        )
        apos_segundo = await lead_do_teste("xmin::text, agent_active")

    assert primeiro.json()["motivo"] == "from_me"
    assert segundo.json()["motivo"] == "from_me"
    assert apos_primeiro is not None and apos_primeiro[1] is False
    assert apos_segundo == apos_primeiro, "o segundo fromMe refez o UPDATE"


async def test_header_com_caractere_nao_ascii_da_401(limpar, monkeypatch):
    """compare_digest sobre str não-ASCII levanta TypeError → 500 → loop.

    O valor vai como bytes latin-1 porque é assim que trafega no fio: o
    httpx recusa str não-ASCII em header, e o Starlette decodifica de volta
    para `str` com latin-1 antes de a rota ver.
    """
    monkeypatch.setattr(settings, "evolution_webhook_secret", "segredo-forte")

    async with await cliente() as c:
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(),
            headers={"apikey": "café".encode("latin-1")},
        )

    assert r.status_code == 401, r.text
    assert await linhas_na_fila("id") == []


async def test_secret_configurado_rejeita_header_errado(limpar, monkeypatch):
    monkeypatch.setattr(settings, "evolution_webhook_secret", "segredo-forte")

    async with await cliente() as c:
        errado = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(),
            headers={"X-Evolution-Webhook-Secret": "chute"},
        )
        ausente = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())
        certo = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(),
            headers={"X-Evolution-Webhook-Secret": "segredo-forte"},
        )

    assert errado.status_code == 401
    assert ausente.status_code == 401
    assert certo.status_code == 200
    assert certo.json()["status"] == "ok"
    assert len(await linhas_na_fila("id")) == 1


async def test_rate_limit_nao_deixa_o_gate_escrever(limpar, monkeypatch):
    """Mensagem barrada não pode contar como engajamento do lead.

    O gate renova last_interaction_at e zera followup_count. Se rodasse
    antes do rate limit, o lead ficaria "engajado" por uma mensagem que
    nunca chegou ao agente.
    """
    monkeypatch.setattr(settings, "rate_limit_per_hour", 0)

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    # 200 de propósito: 429 faria a Evolution reentregar em loop.
    assert r.status_code == 200
    assert r.json() == {"status": "ignorado", "motivo": "rate_limit"}
    assert await linhas_na_fila("id") == []

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone = %s", (TELEFONE,)
        )
        assert (await cur.fetchone())[0] == 0


async def test_reentrega_nao_consome_cota_de_rate_limit(limpar, monkeypatch):
    """Reentrega é o mesmo evento — contá-la duas vezes evapora mensagem.

    Com o lookup de duplicata depois do rate limit, 15 mensagens do lead
    mais uma reentrega de cada estouram uma janela de 30: a partir daí a
    rota devolve 200 + `rate_limit`, a Evolution não reentrega, e a mensagem
    do lead some sem rastro.
    """
    monkeypatch.setattr(settings, "rate_limit_per_hour", 2)

    async with await cliente() as c:
        primeira = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload(message_id="MSG-A")
        )
        reentrega = await c.post(
            f"/webhook/evolution?agent={AGENTE}", json=payload(message_id="MSG-A")
        )
        segunda = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(texto="segunda", message_id="MSG-B"),
        )

    assert primeira.json()["status"] == "ok"
    assert reentrega.json()["motivo"] == "duplicata"
    assert reentrega.json()["queue_id"] == primeira.json()["queue_id"]
    assert segunda.json()["status"] == "ok", segunda.text


async def test_reentrega_nao_renova_engajamento_do_lead(limpar, monkeypatch):
    """O lookup antes do rate limit também fica antes do gate — de propósito.

    O gate renova `last_interaction_at` e zera `followup_count`. Uma
    reentrega do provedor não é interação nova do lead.
    """
    colunas = "xmin::text, followup_count, last_interaction_at"

    async with await cliente() as c:
        await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())
        antes = await lead_do_teste(colunas)
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert r.json()["motivo"] == "duplicata"
    assert await lead_do_teste(colunas) == antes


async def test_rate_limit_nao_barra_handover_do_atendente(limpar, monkeypatch):
    """fromMe precisa chegar ao gate mesmo com o limite estourado."""
    async with await cliente() as c:
        await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

        monkeypatch.setattr(settings, "rate_limit_per_hour", 0)
        r = await c.post(
            f"/webhook/evolution?agent={AGENTE}",
            json=payload(from_me=True, message_id="MSG-HUMANO"),
        )

    assert r.json()["motivo"] == "from_me"

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select agent_active from leads_crm where phone = %s", (TELEFONE,)
        )
        linha = await cur.fetchone()

    assert linha is not None, "o lead deveria existir"
    assert linha[0] is False, "o agente deveria ter sido desligado pelo handover"


async def test_sem_secret_configurado_a_rota_fica_aberta(limpar):
    assert settings.evolution_webhook_secret == ""

    async with await cliente() as c:
        r = await c.post(f"/webhook/evolution?agent={AGENTE}", json=payload())

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
