-- 010_message_id_unico_por_agente.sql
-- Corrige a chave de deduplicação introduzida pela 009.
--
-- A 009 chaveou (channel, message_id) e com isso quebrou o mecanismo
-- multi-agente do template: o mesmo payload entregue em `?agent=a` e
-- `?agent=b` são duas mensagens legítimas, e a segunda era recusada pelo
-- índice como se fosse reentrega da primeira.
--
-- Id de mensagem do WhatsApp só é único por chat — é exatamente por isso que
-- a Evolution exige remoteJid+fromMe+id para baixar mídia. (channel,
-- agent_id, message_id) é a chave natural mais próxima disso sem trazer o
-- remoteJid para dentro do índice.
--
-- Por que uma migração nova em vez de editar a 009: o runner
-- (`shared/db.py` e `db/migrate.py`) pula migração pelo NOME do arquivo
-- registrado em `_migrations`. Um banco que já aplicou a 009 nunca
-- reexecutaria o arquivo, ficaria só com o índice antigo, e todo INSERT de
-- `enqueue_or_buffer` passaria a falhar com
--
--     ERROR: there is no unique or exclusion constraint matching the
--            ON CONFLICT specification
--
-- porque o alvo do ON CONFLICT é resolvido no planejamento da query, não na
-- colisão. Não seria degradação de um canal: seria parada total de ingestão
-- em Twilio, Meta, uazapi e Evolution ao mesmo tempo.
--
-- Esta migração é o ponto de convergência: banco novo (que acabou de rodar a
-- 009) e banco que já estava com a 009 aplicada terminam idênticos, sem
-- nenhuma intervenção manual.

-- Recalcula a limpeza pela chave nova. É mais permissiva que a da 009: um
-- par que era duplicata por (channel, message_id) pode ser legítimo por
-- (channel, agent_id, message_id). Nada é reatribuído — só sobra menos
-- linha para zerar.
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

-- O índice da 009 sai só depois do novo existir: entre um comando e outro a
-- tabela nunca fica sem restrição de unicidade.
DROP INDEX IF EXISTS idx_message_queue_channel_message_id;
