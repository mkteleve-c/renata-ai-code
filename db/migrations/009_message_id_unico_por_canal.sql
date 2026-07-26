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

-- Duplicatas anteriores à restrição: mantém a linha mais antiga de cada
-- grupo (a que o worker de fato processou primeiro) e apaga as reentregas.
-- No-op quando não há duplicata, o que torna a migração repetível.
DO $$
DECLARE
    removidas integer;
BEGIN
    DELETE FROM message_queue
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
            'message_queue: % linha(s) duplicada(s) por (channel, message_id) removida(s)',
            removidas;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_message_queue_channel_message_id
    ON message_queue (channel, message_id)
    WHERE message_id IS NOT NULL;
