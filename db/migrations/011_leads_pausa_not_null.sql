-- 011_leads_pausa_not_null.sql
-- NULL em agent_active significava coisas opostas em dois lugares.
--
-- O gate (`shared/leads.py`) testa `agent_active is False`, então NULL era
-- lead ATIVO. O índice parcial de follow-up da 007
-- (`WHERE followup_active AND agent_active`) não indexa linha com NULL, então
-- para a escada de follow-up NULL era lead PAUSADO. O mesmo lead recebia
-- resposta do agente e nunca entrava na régua de follow-up, sem nada no log
-- explicando por quê.
--
-- Resolvido na origem: as colunas passam a ser NOT NULL e a ambiguidade
-- deixa de existir para os dois leitores.
--
-- Backfill para `true` (não `false`) porque é o DEFAULT já declarado na 007 e
-- é a leitura que o gate faz hoje em produção: nenhum lead muda de
-- comportamento no inbound. O que muda é que essas linhas passam a entrar no
-- índice de follow-up — que é exatamente a correção. Backfill para `false`
-- calaria o agente para leads reais, dano pior e não recuperável pelo lead.
--
-- Por que migração nova em vez de editar a 007: o runner (`shared/db.py` e
-- `db/migrate.py`) pula migração pelo NOME do arquivo registrado em
-- `_migrations` e não guarda checksum. Um banco que já aplicou a 007 nunca
-- reexecutaria o arquivo editado.

UPDATE leads_crm SET agent_active    = true WHERE agent_active    IS NULL;
UPDATE leads_crm SET followup_active = true WHERE followup_active IS NULL;
UPDATE leads_crm SET followup_count  = 0    WHERE followup_count  IS NULL;
UPDATE leads_crm SET metadata        = '{}' WHERE metadata        IS NULL;

ALTER TABLE leads_crm ALTER COLUMN agent_active    SET NOT NULL;
ALTER TABLE leads_crm ALTER COLUMN followup_active SET NOT NULL;
ALTER TABLE leads_crm ALTER COLUMN followup_count  SET NOT NULL;
ALTER TABLE leads_crm ALTER COLUMN metadata        SET NOT NULL;
