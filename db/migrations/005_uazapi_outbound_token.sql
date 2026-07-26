-- 005_uazapi_outbound_token.sql
-- Token outbound dinâmico por mensagem.
-- Necessário para a uazapi (uazapiGO), que entrega o token da instância
-- no payload do webhook em vez de exigir configuração estática. O worker
-- usa este valor no envio outbound (header `token`).

ALTER TABLE message_queue
    ADD COLUMN IF NOT EXISTS outbound_token TEXT;
