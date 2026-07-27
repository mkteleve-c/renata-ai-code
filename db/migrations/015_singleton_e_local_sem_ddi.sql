-- 015_singleton_e_local_sem_ddi.sql
-- Dois furos que sobraram na revisão da 014, os dois na mesma superfície
-- (o CHECK novo pode falhar alto de verdade, e pode deixar passar forma que
-- deveria recusar) -- por isso os dois vão na mesma migração.
--
-- CRÍTICO: a 014 só consolida GRUPOS (`HAVING count(*) > 1`). Uma linha
-- não-canônica SOZINHA -- sem nenhuma irmã física no momento em que a
-- migração roda -- nunca é tocada pelo laço, e o `ADD CONSTRAINT` da 014
-- estoura em cima dela: `check constraint "leads_crm_phone_canonico_check"
-- is violated by some row`. Como `run_migrations` aplica cada arquivo numa
-- transação própria e só grava `_migrations` DEPOIS do arquivo inteiro
-- rodar sem erro, essa exceção derruba a transação inteira (inclusive a
-- consolidação de grupos que tinha acabado de funcionar) e propaga para
-- fora de `run_migrations` -- que roda no lifespan da API. A 014 nunca fica
-- registrada, e todo boot seguinte tenta de novo e falha de novo.
--
-- Reproduzido com o SQL exato do arquivo contra uma base no formato da
-- legada: 417 linhas violam o CHECK, e 404 delas são singletons que o laço
-- de grupos nunca alcança (ele só enxerga as outras 13, os grupos).
--
-- Isto NUNCA vai acontecer no `leads_crm` do harness em produção -- a única
-- coisa que escreve nessa tabela é `aplicar_gate`, que já grava a forma
-- canônica; a base de 3.368 linhas com singletons malformados é do
-- SUPABASE legado, uma tabela diferente, que a 014 nunca vê (ver o
-- contrato da Fase 4 em `docs/AGENTE_ELEVEC.md`). Mas é exatamente a classe
-- de furo que devia estar fechada por construção, não por sorte de este
-- deploy específico não ter dado de cara com uma linha malformada -- e é
-- barato fechar: com os grupos já resolvidos pela 014, o resto é
-- renomeação pura, sem risco de colisão (duas linhas SÓ colidem se
-- compartilham identidade canônica, e aí já são um GRUPO, que a 014 já
-- pegou).
--
-- Por que migração nova e não editar a 014: a 014 já está registrada em
-- `_migrations` neste banco de dev (rodou sem erro aqui -- a tabela estava
-- vazia, nenhum singleton para expor o furo). Editar o arquivo não
-- reexecuta nada num banco que já a aplicou; o runner pula pelo nome.

-- Mesma função da 014, mesmo corpo -- copiada porque a 014 dropa a dela ao
-- final e não pode ser editada para deixar a sua reutilizável.
-- *** ESPELHA `shared/phone.py::canonicalizar`/`_sem_zero_de_tronco` E A
-- CÓPIA DA 014. AS TRÊS PRECISAM ANDAR JUNTAS. ***
CREATE OR REPLACE FUNCTION _migracao_015_canonico(bruto TEXT) RETURNS TEXT AS $$
DECLARE
    digitos TEXT := bruto;
BEGIN
    IF digitos LIKE '550%' THEN
        digitos := '55' || regexp_replace(substring(digitos FROM 3), '^0+', '');
    ELSIF digitos LIKE '0%' THEN
        digitos := regexp_replace(digitos, '^0+', '');
    END IF;

    IF digitos ~ '^55[0-9]{2}9[0-9]{8}$' THEN
        RETURN '55' || substring(digitos FROM 3 FOR 2) || substring(digitos FROM 6 FOR 8);
    END IF;

    RETURN digitos;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Renomeação pura -- nenhuma fusão aqui. Se esta linha colidisse com outra
-- ao ser renomeada, as duas já compartilhariam a identidade canônica e já
-- teriam sido um GRUPO que a 014 consolidou; sobrar como singleton aqui
-- prova que não há conflito. Uma violação de PK nesta UPDATE (o que só
-- aconteceria se essa garantia estiver errada) falha alto, não corrompe
-- silenciosamente -- aceitável, porque não deveria ser alcançável.
UPDATE leads_crm
SET phone = _migracao_015_canonico(phone)
WHERE phone <> _migracao_015_canonico(phone);

DROP FUNCTION _migracao_015_canonico(TEXT);

-- IMPORTANTE: o CHECK da 014 (duas cláusulas) deixa passar a forma local
-- SEM DDI -- `1187654321` (10 dígitos) ou `11987654321` (11 dígitos, com o
-- 9º). `canonicalizar()` NUNCA produz essas formas para número brasileiro
-- (sempre prefixa "55"), então nenhum caminho de escrita real do harness
-- as gera hoje -- mas é exatamente a classe de bug de importador que o
-- CHECK existe para pegar, e o contrato da Fase 4 (`docs/AGENTE_ELEVEC.md`)
-- se apoia nisso. Sem esta terceira cláusula, as três formas abaixo
-- conviveriam na mesma tabela como identidades DIFERENTES, cada uma
-- recebendo follow-up -- a mesma duplicata que a 014 existe para impedir,
-- só que por um portão que ela deixou aberto:
--
--     1187654321 | 11987654321 | 551187654321   <- todas = 551187654321
--
-- DROP + ADD porque Postgres não tem ALTER CONSTRAINT para CHECK.
ALTER TABLE leads_crm DROP CONSTRAINT IF EXISTS leads_crm_phone_canonico_check;

ALTER TABLE leads_crm
    ADD CONSTRAINT leads_crm_phone_canonico_check
    CHECK (
        phone !~ '^55[0-9]{2}9[0-9]{8}$'
        AND phone !~ '^550'
        AND phone !~ '^[0-9]{10,11}$'
    );
