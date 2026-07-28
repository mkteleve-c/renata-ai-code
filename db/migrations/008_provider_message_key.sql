-- 008_provider_message_key.sql
-- Key completa da mensagem, quando o provedor exige ela para operações
-- subsequentes (ex: download de mídia).
-- A Evolution rejeita getBase64FromMediaMessage se a key não vier completa
-- (remoteJid + fromMe + id) — um id isolado não é suficiente. Gravamos
-- data.key inteiro ao enfileirar mensagens com mídia para o worker
-- reencaminhar sem precisar reconstruir a key a partir de campos soltos.

ALTER TABLE message_queue
    ADD COLUMN IF NOT EXISTS provider_message_key JSONB;
