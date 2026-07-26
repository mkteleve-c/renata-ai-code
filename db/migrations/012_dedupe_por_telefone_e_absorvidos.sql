-- 012_dedupe_por_telefone_e_absorvidos.sql
-- Fecha os dois furos que sobraram na deduplicação de reentrega.
--
-- 1) A chave da 010 é (channel, agent_id, message_id) e não inclui o
--    telefone. O comentário da própria 010 diz que o id do WhatsApp só é
--    único POR CHAT — é por isso que a Evolution exige remoteJid+fromMe+id
--    para baixar mídia. Dois leads distintos com o mesmo `key.id` faziam a
--    segunda mensagem ser descartada como "reentrega" em silêncio.
--
-- 2) O `message_id` só chega ao banco no INSERT. O UPDATE do debounce
--    concatena o texto e não grava id nenhum, então numa rajada só a
--    PRIMEIRA mensagem fica protegida:
--
--        A(ID-A) -> linha nova "oi"
--        B(ID-B) -> debounce   "oi\ntudo bem?"   (message_id continua ID-A)
--        B(ID-B) reentregue    "oi\ntudo bem?\ntudo bem?"
--
--    Rajada é o caso normal no WhatsApp — é a razão de o debounce existir.
--    `message_ids_absorvidos` guarda os ids que entraram na linha por
--    debounce, e o lookup de dedupe passa a olhar para ele também.
--
-- Por que migração nova em vez de editar a 010: o runner (`shared/db.py` e
-- `db/migrate.py`) pula migração pelo NOME do arquivo registrado em
-- `_migrations` e não guarda checksum. Editar a 010 deixaria todo banco já
-- migrado com o índice antigo e faria o ON CONFLICT do `enqueue_or_buffer`
-- falhar no planejamento da query — parada total de ingestão em todos os
-- canais, não degradação de um.

ALTER TABLE message_queue
    ADD COLUMN IF NOT EXISTS message_ids_absorvidos TEXT[] NOT NULL DEFAULT '{}';

-- Nenhuma limpeza prévia é necessária: a chave nova é um SUPERCONJUNTO da
-- chave da 010, então toda violação da nova já seria violação da antiga — e
-- a antiga está em vigor desde a 010.
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_queue_dedupe
    ON message_queue (channel, agent_id, phone_number, message_id)
    WHERE message_id IS NOT NULL;

-- O índice da 010 sai só depois do novo existir: entre um comando e outro a
-- tabela nunca fica sem restrição de unicidade.
DROP INDEX IF EXISTS idx_message_queue_channel_agent_message_id;

CREATE INDEX IF NOT EXISTS idx_message_queue_absorvidos
    ON message_queue USING GIN (message_ids_absorvidos);
