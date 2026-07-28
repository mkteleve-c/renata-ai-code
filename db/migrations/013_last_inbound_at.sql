-- 013_last_inbound_at.sql
-- Relógio da janela de 24h da Cloud API.
--
-- last_interaction_at mistura "o lead falou" com "nós falamos" — o follow-up
-- também o atualiza, o que torna a escada cumulativa (medido: o degrau 3 caía
-- em ~24h28 desde a criação do lead, fora da janela). Esta coluna registra só
-- o inbound do lead, e é o que ancora os três degraus.
--
-- SEM DEFAULT, e nullable, DE PROPÓSITO. NULL = "nunca vi este lead falar", e
-- é seguro por construção: `NULL > now() - interval` não é TRUE, então o lead
-- não é reivindicado. Com DEFAULT now(), toda linha importada nasceria com
-- janela aberta e receberia follow-up que a Meta rejeita.

ALTER TABLE leads_crm ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;

-- Backfill conservador: só para quem nunca recebeu follow-up, onde
-- last_interaction_at ainda é o inbound do lead. Para followup_count > 0 o
-- valor é provadamente o NOSSO envio — copiá-lo superestimaria a janela em até
-- 24h. Esses ficam NULL e voltam à régua quando falarem de novo.
UPDATE leads_crm SET last_inbound_at = last_interaction_at
WHERE last_inbound_at IS NULL AND followup_count = 0;

DROP INDEX IF EXISTS idx_leads_followup;
CREATE INDEX idx_leads_followup
    ON leads_crm (last_inbound_at)
    INCLUDE (phase, followup_count)
    WHERE followup_active AND agent_active;
