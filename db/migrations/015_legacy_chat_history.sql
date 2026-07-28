-- 015_legacy_chat_history.sql
-- Destino do que vem do Supabase legado (Fase 4). Duas tabelas com
-- contratos OPOSTOS:
--   - legacy_chat_history só aceita telefone CANÔNICO -- FK para
--     leads_crm, cai junto no DELETE. É histórico de conversa de gente
--     que existe.
--   - leads_descartados aceita QUALQUER coisa em phone_original, inclusive
--     NULL e a string literal "null" -- é o depósito do que não converge
--     numa migração de dados reais (a base tem uma linha cujo phone é
--     exatamente a string "null", outra com 9 dígitos, outra que é
--     +5511666666665; todas precisam caber). NÃO colocar NOT NULL em
--     phone_original -- isso faria a migração perder exatamente as linhas
--     que ela existe para reportar. Contraintuitivo, mas de propósito: a
--     tentação de "endurecer" essa coluna é o próprio bug.
--
-- As duas tabelas já existem desde 007_elevec.sql, num formato diferente
-- do que esta migração estabelece:
--   - legacy_chat_history: (phone, idx, role, content), PK composta em
--     (phone, idx), sem FK para leads_crm, sem CHECK em role. Nenhum
--     código de produção nem teste lê ou escreve nela (só a criação em
--     007 e o teste de existência de tabela em test_migracao_007.py) --
--     droppada e recriada abaixo com o formato novo, sem perda, porque
--     está vazia.
--   - leads_descartados: (phone_original, motivo, payload, created_at),
--     já sem NOT NULL em nenhuma coluna -- já satisfaz o contrato
--     permissivo hoje, por acidente de nunca ter sido restringida.
--     Mantida com os MESMOS nomes de coluna: `shared/leads.py::
--     _reter_descarte` e os webhooks já escrevem nela em produção, e
--     renomear quebraria esse caminho sem necessidade nenhuma -- o ganho
--     desta migração é só reforçar que motivo/payload (que a aplicação
--     sempre preenche) não podem ficar em branco.

DROP TABLE IF EXISTS legacy_chat_history;

CREATE TABLE legacy_chat_history (
    id       BIGSERIAL PRIMARY KEY,
    phone    TEXT NOT NULL REFERENCES leads_crm(phone) ON DELETE CASCADE,
    ordem    INT  NOT NULL,
    papel    TEXT NOT NULL CHECK (papel IN ('human', 'ai')),
    conteudo TEXT NOT NULL,
    UNIQUE (phone, ordem)
);

CREATE INDEX idx_legacy_chat_phone ON legacy_chat_history (phone, ordem);

ALTER TABLE leads_descartados
    ALTER COLUMN motivo SET NOT NULL,
    ALTER COLUMN payload SET NOT NULL;
