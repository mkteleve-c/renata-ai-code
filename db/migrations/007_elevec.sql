-- 007_elevec.sql
-- Schema do SDR da EleveC: funil de leads, blocklist e histórico legado.
-- Roda automaticamente no startup da API (run_migrations).

DO $$ BEGIN
    CREATE TYPE lead_phase AS ENUM (
        'formulario_preenchido', 'iniciou_conversa', 'qualificado',
        'agendou_sessao', 'desqualificado', 'perdido');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE lead_source AS ENUM (
        'linkedin_form', 'respondiapp_form', 'whatsapp_direct', 'manual_import');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS leads_crm (
    phone                TEXT PRIMARY KEY CHECK (phone ~ '^[0-9]{8,15}$'),
    pipedriveid          TEXT,
    name                 TEXT,
    username             TEXT,
    email                TEXT,
    faturamento_mensal   TEXT,
    qualificacao_notas   TEXT,
    google_event_id      TEXT,
    phase                lead_phase  DEFAULT 'formulario_preenchido',
    source               lead_source,
    followup_count       INT         DEFAULT 0,
    followup_active      BOOLEAN     DEFAULT true,
    agent_active         BOOLEAN     DEFAULT true,
    agent_reactivate_at  TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT now(),
    last_interaction_at  TIMESTAMPTZ DEFAULT now(),
    metadata             JSONB       DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS blocklist (
    phone      TEXT PRIMARY KEY CHECK (phone ~ '^[0-9]{8,15}$'),
    motivo     TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS legacy_chat_history (
    phone   TEXT,
    idx     INT,
    role    TEXT,
    content TEXT,
    PRIMARY KEY (phone, idx)
);

CREATE TABLE IF NOT EXISTS leads_descartados (
    phone_original TEXT,
    motivo         TEXT,
    payload        JSONB,
    created_at     TIMESTAMPTZ DEFAULT now()
);

-- DROP explícito antes do CREATE: quem já rodou uma versão anterior desta
-- migração (colunas líder followup_active/agent_active redundantes com o
-- predicado, sem phase) precisa que a reaplicação troque a definição do
-- índice. IF NOT EXISTS sozinho não faria isso — só rodaria uma vez e nunca
-- mais tocaria no índice existente, mesmo com a definição errada.
DROP INDEX IF EXISTS idx_leads_followup;

CREATE INDEX IF NOT EXISTS idx_leads_followup
    ON leads_crm (last_interaction_at, phase)
    WHERE followup_active AND agent_active;
