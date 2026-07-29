-- 017_baloes_enviados.sql
-- Quantos balões desta mensagem já chegaram ao lead.
--
-- A Renata responde em balões (`saida.py::extrair_baloes`), cada um um
-- `send_message` independente. Se o terceiro falha, os dois primeiros JÁ
-- foram entregues -- e o retry do worker reinvocava o agente do zero e
-- recomeçava o envio no índice 0, entregando de novo o que a pessoa já
-- tinha lido. Com `max_attempts = 3`, o mesmo balão podia chegar três
-- vezes.
--
-- O contador é incrementado DEPOIS de cada envio confirmado, em transação
-- própria: se o processo morrer entre o envio e o incremento, o retry
-- reenvia UM balão -- errar por um a mais é recuperável, errar pela
-- sequência inteira não era.
--
-- `default 0` cobre as linhas que já existem: mensagem antiga nunca
-- retomou nada, e uma que estivesse em voo no momento da migração
-- simplesmente recomeça do zero, que é o comportamento de hoje.

ALTER TABLE message_queue
    ADD COLUMN IF NOT EXISTS baloes_enviados INT NOT NULL DEFAULT 0;
