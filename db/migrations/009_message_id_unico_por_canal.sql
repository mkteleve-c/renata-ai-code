-- 009_message_id_unico_por_canal.sql
-- Idempotência de ingestão: um message_id do provedor só pode virar uma
-- linha na fila por agente.
--
-- Provedores reentregam o webhook em timeout ou resposta >= 400 (a Evolution
-- faz isso por padrão). Sem restrição no banco, a reentrega vira uma segunda
-- linha na fila e uma segunda resposta ao lead — ou, dentro da janela de
-- debounce, o mesmo texto concatenado duas vezes.
--
-- A chave é (channel, agent_id, message_id):
--   - channel porque os IDs são namespaced por provedor e nada garante que
--     um MessageSid do Twilio nunca colida com um id do Baileys;
--   - agent_id porque o mesmo payload entregue em ?agent=a e ?agent=b são
--     duas mensagens legítimas — é o mecanismo multi-agente do template.
--
-- Vale lembrar que id de mensagem do WhatsApp só é único por chat: é
-- exatamente por isso que a Evolution exige remoteJid+fromMe+id para baixar
-- mídia. A tripla acima é a chave natural mais próxima disso sem trazer o
-- remoteJid para dentro do índice.
--
-- Parcial em `message_id IS NOT NULL` porque o campo é opcional — o webhook
-- sync e chamadas internas enfileiram sem id, e essas linhas precisam
-- continuar podendo coexistir.

-- String vazia é ausência de id disfarçada (a uazapi manda "" quando o
-- payload não traz messageid). Vira NULL para não colidir consigo mesma
-- sob o índice único.
UPDATE message_queue
   SET message_id = NULL
 WHERE message_id = '';

-- Duplicatas anteriores à restrição: a linha mais antiga de cada grupo (a
-- que o worker de fato processou primeiro) mantém o id; as reentregas
-- perdem só o message_id e saem do índice parcial.
--
-- UPDATE e não DELETE de propósito: a linha continua na fila, processável e
-- auditável. Migrações rodam no startup da API — nem falhar o boot por um
-- dado que é exatamente o bug sendo corrigido, nem apagar fila de produção
-- para destravar um índice.
DO $$
DECLARE
    afetadas integer;
BEGIN
    UPDATE message_queue
       SET message_id = NULL,
           updated_at = NOW()
     WHERE id IN (
        SELECT id
          FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY channel, agent_id, message_id
                           ORDER BY id
                       ) AS posicao
                  FROM message_queue
                 WHERE message_id IS NOT NULL
               ) ranqueadas
         WHERE posicao > 1
     );

    GET DIAGNOSTICS afetadas = ROW_COUNT;
    IF afetadas > 0 THEN
        RAISE NOTICE
            'message_queue: message_id zerado em % reentrega(s) duplicada(s)',
            afetadas;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_message_queue_channel_agent_message_id
    ON message_queue (channel, agent_id, message_id)
    WHERE message_id IS NOT NULL;

-- Nome de uma versão anterior desta mesma migração, que chaveava só
-- (channel, message_id) e recusava o par legítimo de agentes diferentes.
-- Só existe em banco que rodou aquela versão; IF EXISTS cobre o resto.
DROP INDEX IF EXISTS idx_message_queue_channel_message_id;
