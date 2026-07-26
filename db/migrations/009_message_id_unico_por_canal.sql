-- 009_message_id_unico_por_canal.sql
-- Idempotência de ingestão: um message_id do provedor só pode virar uma
-- linha na fila.
--
-- Provedores reentregam o webhook em timeout ou resposta >= 400 (a Evolution
-- faz isso por padrão). Sem restrição no banco, a reentrega vira uma segunda
-- linha na fila e uma segunda resposta ao lead — ou, dentro da janela de
-- debounce, o mesmo texto concatenado duas vezes.
--
-- O índice é (channel, message_id) e não só (message_id): os IDs são
-- namespaced por provedor e nada garante que um MessageSid do Twilio nunca
-- colida com um id do Baileys.
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
-- perdem só o message_id e saem do índice parcial. No-op quando não há
-- duplicata, o que torna a migração repetível.
--
-- ÚNICA divergência em relação ao conteúdo com que esta migração foi
-- aplicada pela primeira vez, que aqui fazia DELETE. Trocado para UPDATE
-- porque o DELETE apagaria fila de produção — todo banco real ainda está na
-- 008, então ninguém jamais executou o DELETE contra dado que importa. O
-- estado de índice, que é o que precisa convergir para o ON CONFLICT
-- resolver, é idêntico nas duas versões. Ver 010 para a chave definitiva.
DO $$
DECLARE
    removidas integer;
BEGIN
    UPDATE message_queue
       SET message_id = NULL,
           updated_at = NOW()
     WHERE id IN (
        SELECT id
          FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY channel, message_id ORDER BY id
                       ) AS posicao
                  FROM message_queue
                 WHERE message_id IS NOT NULL
               ) ranqueadas
         WHERE posicao > 1
     );

    GET DIAGNOSTICS removidas = ROW_COUNT;
    IF removidas > 0 THEN
        RAISE NOTICE
            'message_queue: message_id zerado em % duplicata(s) por (channel, message_id)',
            removidas;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_message_queue_channel_message_id
    ON message_queue (channel, message_id)
    WHERE message_id IS NOT NULL;
