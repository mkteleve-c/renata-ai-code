-- 006_message_channel.sql
-- Canal de mensageria por mensagem.
-- Cada webhook (twilio/meta/uazapi) grava o próprio canal aqui ao enfileirar,
-- e o worker usa esse valor para escolher o cliente outbound correto. Substitui
-- a antiga env MESSAGING_CHANNEL, que escolhia um único cliente global.

ALTER TABLE message_queue
    ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'twilio';

-- Default 'twilio' aplica para linhas pré-existentes (compat retroativa do
-- comportamento antigo, em que o canal padrão era twilio).

CREATE INDEX IF NOT EXISTS idx_message_queue_channel
    ON message_queue (channel);
