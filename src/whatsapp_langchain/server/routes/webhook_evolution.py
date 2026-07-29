"""Webhook da Evolution API (integração WHATSAPP-BUSINESS).

O payload chega no formato Baileys mesmo quando a integração é a Cloud API
oficial — a Evolution normaliza os dois casos:

    {
      "event": "messages.upsert",
      "instance": "instancia-apioficial",
      "data": {
        "key": {"remoteJid": "...@s.whatsapp.net", "remoteJidAlt": "...",
                "fromMe": false, "id": "..."},
        "pushName": "Fulano",
        "messageType": "conversation",
        "message": {"conversation": "texto"}
      }
    }

`data` também pode vir como array (formato nativo do Baileys, quando a
Evolution repassa o upsert cru) — cada item é uma mensagem e todos são
processados, mesmo caminho que `webhook_uazapi` já usa.

Diferente dos outros canais do harness, aqui o gate de ingestão roda ANTES
de enfileirar: `fromMe` é eco de mensagem enviada por humano e não pode
virar item de fila, e filtrar antes evita ocupar a fila com descarte.

O nome do evento varia entre versões da Evolution (`messages.upsert`,
`messages`, `MESSAGES_UPSERT`, `messages-upsert`) — separador e caixa são
normalizados antes da comparação.

Só vira linha na fila o que o agente consegue processar: evento sem texto
nem mídia (reação, enquete, mensagem apagada, protocolo) é descartado com
200, antes do gate. Sem esse corte o agente seria invocado com conteúdo
vazio e um emoji criaria lead novo em `leads_crm`.

Mídia sem `url` NÃO é descarte no momento da ingestão: basta `media_type`
para a mensagem entrar na fila como mídia. Na integração WHATSAPP-BUSINESS
a URL sempre vem (e é ela que baixa), mas descartar aqui perderia áudio de
lead em silêncio, com 200 e sem reentrega, se algum payload chegar sem ela
— quem responde ao lead que a mídia não foi processada é o worker.

Reentrega é esperada: a Evolution repete o POST em timeout ou resposta
>= 400. A rota reconhece a reentrega ANTES do rate limit e do gate (ambos
gastam algo por mensagem) e responde 200 com motivo `duplicata` — responder
erro faria a Evolution reentregar de novo, em loop. `enqueue_or_buffer`
mantém a mesma checagem sob lock, como rede de segurança para POSTs
simultâneos do mesmo id.

Nada aqui responde 4xx por erro de configuração — agente inexistente e body
malformado saem com 200 e log de erro. Reentrega não conserta typo na URL
do webhook nem JSON quebrado; 4xx só produziria loop infinito. A exceção é
o 401 do secret, em `verify_evolution_webhook_secret`.

Mídia recebida nesta integração é baixável por GET na própria `url` do
payload (`lookaside.fbsbx.com`, não criptografada) — ver `worker/media.py`.
`data.key` continua sendo gravado em `provider_message_key` como dado de
diagnóstico e como via de download de uma instância Baileys; ver o
comentário no `enqueue_or_buffer` desta rota.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from whatsapp_langchain.agents.loader import list_agents
from whatsapp_langchain.server.dependencies import (
    check_rate_limit,
    verify_evolution_webhook_secret,
)
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.leads import aplicar_gate
from whatsapp_langchain.shared.models import MessagingChannel
from whatsapp_langchain.shared.phone import resolver_telefone, to_e164
from whatsapp_langchain.shared.queue import buscar_duplicata, enqueue_or_buffer

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

EVENTOS_DE_MENSAGEM = {"messages.upsert", "messages"}

# Campo dentro de `message` -> MIME de fallback, usado só quando o nó não
# declara o MIME. É o conteúdo que manda, não o `messageType`: em mensagem
# embrulhada (viewOnce, efêmera, documento com legenda) o tipo declarado é o
# do envelope, não o da mídia de dentro — um `messageType=documentMessage`
# com `imageMessage` dentro viraria `application/octet-stream`, que o
# preprocessor classifica como mídia não suportada.
#
# `pttMessage` não está aqui porque não existe: no Baileys, áudio de voz é
# `audioMessage` com `ptt: true`.
CAMPOS_DE_MIDIA: dict[str, str] = {
    "imageMessage": "image/jpeg",
    "audioMessage": "audio/ogg",
    "videoMessage": "video/mp4",
    "documentMessage": "application/octet-stream",
}

# Figurinha não vira mídia: tratá-la como imagem custava um download mais
# uma chamada multimodal ao LLM por figurinha, para descrever uma figurinha
# — e webp animado provavelmente nem seria aceito pelo modelo. Vira marcador
# de texto: o agente sabe o que chegou, responde no fluxo, e o gate roda
# normalmente (a mensagem existe no chat, diferente de uma reação).
CAMPO_DE_FIGURINHA = "stickerMessage"
TEXTO_DE_FIGURINHA = "[figurinha]"

# O nome do campo do MIME muda com a integração da instância: a
# WHATSAPP-BUSINESS (Cloud API oficial) manda `mime_type`, o Baileys manda
# `mimetype`. Este é um template herdado por clientes que rodam as duas, e
# ler só uma forma joga a mensagem no default do campo — um `audio/mp4`
# viraria `audio/ogg` e a transcrição sairia com o formato errado.
CAMPOS_DE_MIME = ("mime_type", "mimetype")

# Envelopes que aninham a mensagem real em `message.<envelope>.message`.
ENVELOPES = (
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "documentWithCaptionMessage",
    "editedMessage",
)


def _normalizar_evento(bruto: Any) -> str:
    """Reduz o nome do evento a uma forma só: `messages.upsert`.

    A Evolution v2 emite o mesmo evento como `messages.upsert`,
    `MESSAGES_UPSERT` ou `messages-upsert` conforme a versão e o modo de
    entrega (webhook global, rabbitmq, websocket).
    """
    if not isinstance(bruto, str):
        return ""
    return bruto.strip().lower().replace("-", ".").replace("_", ".")


def _desembrulhar(msg: dict[str, Any], profundidade: int = 3) -> dict[str, Any]:
    """Desce nos envelopes até chegar na mensagem que carrega o conteúdo."""
    for _ in range(profundidade):
        for envelope in ENVELOPES:
            interno = msg.get(envelope)
            if isinstance(interno, dict):
                aninhada = interno.get("message")
                msg = aninhada if isinstance(aninhada, dict) else interno
                break
        else:
            return msg
    return msg


def _extrair_conteudo(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Devolve (texto, media_url, media_type) do payload da Evolution.

    Tudo que não é texto nem mídia conhecida sai como `("", None, None)` —
    a rota trata isso como evento a ignorar. Reação, atualização de enquete
    e `protocolMessage` de mensagem apagada caem aqui.

    O `media_type` vem do MIME declarado no próprio nó de mídia, em
    `mime_type` (Cloud API) ou `mimetype` (Baileys) — ver `CAMPOS_DE_MIME`.
    Os consumidores aguentam o parâmetro do MIME: `_media_kind` testa
    `startswith("audio/")` e `_audio_format_from_media_type` acha `"ogg"`
    dentro de `"audio/ogg; codecs=opus"`.
    """
    bruto = data.get("message")
    msg = _desembrulhar(bruto) if isinstance(bruto, dict) else {}

    texto = msg.get("conversation")
    if not isinstance(texto, str) or not texto:
        estendida = msg.get("extendedTextMessage")
        candidato = estendida.get("text") if isinstance(estendida, dict) else None
        texto = candidato if isinstance(candidato, str) else ""

    if isinstance(msg.get(CAMPO_DE_FIGURINHA), dict):
        return texto or TEXTO_DE_FIGURINHA, None, None

    for campo, mime_padrao in CAMPOS_DE_MIDIA.items():
        conteudo = msg.get(campo)
        if not isinstance(conteudo, dict):
            continue

        url = conteudo.get("url")
        legenda = conteudo.get("caption")
        if not texto and isinstance(legenda, str):
            texto = legenda

        declarado = ""
        for campo_mime in CAMPOS_DE_MIME:
            valor = conteudo.get(campo_mime)
            if isinstance(valor, str) and valor.strip():
                declarado = valor.strip()
                break

        return (
            texto,
            url if isinstance(url, str) and url else None,
            declarado or mime_padrao,
        )

    return texto, None, None


def _mensagens_do_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normaliza `data` (objeto único ou array do Baileys) numa lista."""
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


async def _processar_mensagem(
    data: dict[str, Any],
    agent: str,
    instance: Any,
) -> dict[str, Any]:
    """Roda gate, rate limit e enfileiramento para uma mensagem do payload.

    Ordem: extração e guard de conteúdo → duplicata → rate limit → gate →
    fila. Cada passo é mais caro que o anterior, e tudo que não vai virar
    linha na fila é cortado ANTES do gate, que escreve em leads_crm.
    `fromMe` é a única exceção — ele nunca é aceito pelo gate, mas precisa
    chegar lá para desligar o agente (handover do atendente).
    """
    bruto = data.get("key")
    key: dict[str, Any] = bruto if isinstance(bruto, dict) else {}
    message_id = key.get("id") if isinstance(key.get("id"), str) else None
    eh_from_me = key.get("fromMe") is True

    texto, media_url, media_type = _extrair_conteudo(data)

    # Nem texto nem mídia: reação, enquete, mensagem apagada, protocolo.
    # Enfileirar isso invocaria o agente com conteúdo vazio. O corte é por
    # `media_type` e não por `media_url`: um payload de mídia sem URL não
    # baixa, mas quem avisa o lead disso é o worker (auto-resposta), não um
    # descarte silencioso com 200 e sem reentrega.
    #
    # Antes do gate: uma reação de lead que passasse por ele renovaria
    # last_interaction_at e zeraria followup_count, criando ou "reengajando"
    # um lead por um emoji que o agente nunca vê.
    #
    # `fromMe` NÃO é exceção a este corte, apesar de ser exceção ao gate. O
    # handover do atendente é implícito — responder pelo número comercial
    # desliga a Renata para aquele lead, de forma irreversível por qualquer
    # caminho automático (`_SQL_REATIVAR` exige `agent_reactivate_at < now()`
    # e o gate grava `null`). O gatilho disso precisa ser uma mensagem DE
    # VERDADE. Com a exceção que existia aqui, reagir com 👍 ou apagar a
    # própria mensagem chegava ao gate e executava o handover: um emoji
    # desligava o agente para sempre, com log em INFO e nenhum sinal de erro.
    if not texto and not media_type:
        logger.info(
            "evolution_conteudo_nao_suportado",
            phone=key.get("remoteJid"),
            message_type=data.get("messageType"),
            message_key_id=message_id,
            instance=instance,
        )
        return {"status": "ignorado", "motivo": "conteudo_nao_suportado"}

    pool = await get_pool()

    # Reentrega antes de tudo que gasta: cota de rate limit e escrita do
    # gate. A Evolution repete o POST em timeout ou resposta >= 400, e o
    # evento é o mesmo — contá-lo de novo estoura a janela (com
    # RATE_LIMIT_PER_HOUR=30, 15 mensagens do lead mais uma reentrega de
    # cada já bastam) e a partir daí a mensagem seguinte, legítima, some com
    # 200 e sem reentrega. Ficar antes do gate também impede que uma
    # reentrega renove last_interaction_at e zere followup_count.
    #
    # `fromMe` fica fora: não vira linha na fila, então não há duplicata a
    # encontrar, e ele precisa chegar ao gate para desligar o agente.
    canonico_previo = resolver_telefone(key)
    if canonico_previo and not eh_from_me:
        phone_previo = to_e164(canonico_previo)

        duplicada = await buscar_duplicata(
            pool,
            phone_number=phone_previo,
            agent_id=agent,
            message_id=message_id,
            channel=MessagingChannel.EVOLUTION,
        )
        if duplicada is not None:
            # 200: a Evolution reentrega tudo que responder >= 400.
            logger.info(
                "evolution_duplicata",
                phone=phone_previo,
                message_key_id=message_id,
                queue_id=duplicada,
                instance=instance,
            )
            return {
                "status": "ignorado",
                "motivo": "duplicata",
                "queue_id": duplicada,
            }

        # Rate limit antes do gate: o gate escreve (renova
        # last_interaction_at, zera followup_count) e mensagem barrada aqui
        # nunca chega ao agente — deixar o gate rodar antes contaria como
        # engajamento o que foi jogado fora. Sem telefone resolvível não há
        # chave de rate limit; o gate devolve `telefone_invalido` abaixo.
        try:
            await check_rate_limit(phone_previo)
        except HTTPException:
            # 200 de propósito: 429 faria a Evolution reentregar em loop.
            logger.warning(
                "evolution_rate_limit",
                phone=phone_previo,
                message_key_id=message_id,
                instance=instance,
            )
            return {"status": "ignorado", "motivo": "rate_limit"}

    push_name = data.get("pushName")
    resultado = await aplicar_gate(
        pool,
        key,
        push_name=push_name if isinstance(push_name, str) else None,
        # Sem telefone resolvível o gate não tem onde gravar o lead, e o que
        # ele retém em `leads_descartados` é este payload. `agent` vem da
        # query string e `instance` do topo do corpo — nenhum dos dois está
        # dentro de `data`, e sem eles não dá para saber por qual instância a
        # mensagem entrou nem reconstruir o POST que a reprocessaria.
        payload={"agent": agent, "instance": instance, "data": data},
    )

    if not resultado.aceito:
        logger.info(
            "evolution_descartado",
            motivo=resultado.motivo,
            phone=resultado.canonico,
            message_key_id=message_id,
        )
        return {"status": "ignorado", "motivo": resultado.motivo}

    if resultado.canonico is None:
        # Não acontece — o gate só aceita depois de resolver o telefone.
        # Explícito em vez de assert porque assert some com -O.
        raise RuntimeError("gate aceitou mensagem sem telefone canônico")

    phone_e164 = to_e164(resultado.canonico)

    enfileirado = await enqueue_or_buffer(
        pool=pool,
        phone_number=phone_e164,
        agent_id=agent,
        body=texto,
        channel=MessagingChannel.EVOLUTION,
        media_url=media_url,
        media_type=media_type,
        message_id=message_id,
        buffer_seconds=settings.message_buffer_seconds,
        # A key NÃO é mais via de download. Ela existe porque a Fase 1
        # assumiu payload Baileys — URL cifrada, bytes só por
        # `getBase64FromMediaMessage`, que exige a key inteira. O tráfego
        # real desmentiu isso nesta integração: a URL vem aberta e o
        # download é um GET (`worker/media.py`). Continua sendo gravada por
        # dois motivos, nenhum deles o download: é o identificador que
        # correlaciona a linha da fila com a mensagem no lado da Evolution/
        # Meta quando algo precisa ser investigado, e é o que uma instância
        # Baileys (este repositório é template) precisaria para voltar a
        # baixar por `EvolutionClient.baixar_midia`. A coluna vem da
        # migração 008 e fica — migração aplicada é imutável.
        #
        # Key vazia vira None: sem `id`/`remoteJid` ela não identifica nada.
        provider_message_key=key if media_type and key else None,
    )

    if enfileirado.is_duplicate:
        # 200: a Evolution reentrega tudo que responder >= 400.
        logger.info(
            "evolution_duplicata",
            phone=phone_e164,
            message_key_id=message_id,
            queue_id=enfileirado.message_id,
            instance=instance,
        )
        return {
            "status": "ignorado",
            "motivo": "duplicata",
            "queue_id": enfileirado.message_id,
        }

    logger.info(
        "webhook_evolution_recebido",
        phone=phone_e164,
        agent_id=agent,
        instance=instance,
        message_key_id=message_id,
        queue_id=enfileirado.message_id,
        buffered=enfileirado.is_buffered,
        phase=resultado.lead.get("phase") if resultado.lead else None,
    )

    return {"status": "ok", "queue_id": enfileirado.message_id}


@router.post("/webhook/evolution")
async def webhook_evolution_receive(
    request: Request,
    agent: str = Query(
        default="",
        description="ID do agente para processar a mensagem",
    ),
    _secret: None = Depends(verify_evolution_webhook_secret),
) -> dict[str, Any]:
    # Erro de configuração responde 200, não 4xx: um typo no `?agent=` do
    # webhook da instância é o mesmo POST para sempre, e cada resposta >= 400
    # faz a Evolution reentregar — loop indefinido por um erro que reentrega
    # nenhuma vai corrigir. ERROR e não WARNING porque nada vai funcionar até
    # alguém arrumar a URL.
    #
    # `default=""` existe por essa mesma doutrina: sem ele, a URL cadastrada
    # SEM `?agent=` — o erro de configuração mais provável de todos — dava
    # 422 do FastAPI antes de chegar aqui, e a Evolution reentregava para
    # sempre, sem uma linha de log da aplicação. O caminho de erro mais
    # provável era justamente o único que escapava do tratamento pensado
    # para ele.
    if agent not in list_agents():
        logger.error(
            "evolution_agente_desconhecido",
            agent=agent,
            agentes_disponiveis=sorted(list_agents()),
        )
        return {"status": "ignorado", "motivo": "agente_desconhecido"}

    try:
        payload = await request.json()
    except Exception:
        # Mesmo raciocínio: body malformado não melhora em retry.
        logger.error("evolution_json_invalido", agent=agent)
        return {"status": "ignorado", "motivo": "json_invalido"}

    if not isinstance(payload, dict):
        # Body válido como JSON mas fora do formato (lista, string, número).
        # 200 para não entrar em loop de reentrega do mesmo payload quebrado.
        logger.warning("evolution_payload_invalido", tipo=type(payload).__name__)
        return {"status": "ignorado", "motivo": "payload_invalido"}

    evento = _normalizar_evento(payload.get("event") or payload.get("EventType"))
    if evento not in EVENTOS_DE_MENSAGEM:
        # INFO e não DEBUG: com LOG_LEVEL=info (default) um nome de evento
        # novo sumiria sem deixar rastro.
        logger.info("evolution_evento_ignorado", evento=evento or None)
        return {"status": "ignorado", "motivo": "evento_nao_tratado"}

    instance = payload.get("instance")
    mensagens = _mensagens_do_payload(payload)
    if not mensagens:
        logger.warning("evolution_sem_mensagem", instance=instance)
        return {"status": "ignorado", "motivo": "payload_sem_mensagem"}

    resultados = [
        await _processar_mensagem(data, agent, instance) for data in mensagens
    ]

    # Caso normal (a Evolution manda uma mensagem por POST): resposta direta,
    # com `queue_id` no topo. Array só aparece quando o upsert vem cru.
    if len(resultados) == 1:
        return resultados[0]

    return {
        "status": "ok",
        "processados": sum(1 for r in resultados if r["status"] == "ok"),
        "resultados": resultados,
    }
