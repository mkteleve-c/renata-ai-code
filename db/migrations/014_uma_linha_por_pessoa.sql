-- 014_uma_linha_por_pessoa.sql
-- Fecha em definitivo o furo que sobreviveu a quatro rodadas de revisão da
-- régua de follow-up: duas linhas físicas em leads_crm representando a
-- MESMA pessoa, cada uma recebendo a mesma mensagem de WhatsApp.
--
-- `variacoes()` (shared/phone.py) só devolve DUAS formas físicas (com/sem o
-- 9º dígito). A terceira forma real desta base -- zero de tronco na frente
-- do DDI (`55011...`, ~26 registros legados, ver o comentário de
-- `_sem_zero_de_tronco` em phone.py) escapa por construção: não existe
-- conjunto finito conhecido de formas malformadas que `variacoes()` possa
-- enumerar. Alcançar o irmão por enumeração NUNCA pode ser completo, e
-- `aplicar_gate` (shared/leads.py) tem a mesma limitação -- a terceira forma
-- não se auto-cura por inbound.
--
-- Medido na base legada em 27/07/2026: 3.368 linhas, 151 identidades
-- duplicadas, 305 linhas envolvidas, 3 grupos de 3.
--
-- A INVERSÃO: em vez de a régua (e todo consumidor futuro) se defender de
-- duplicata, ela deixa de ser representável. A invariante passa a ser
-- `leads_crm.phone` é SEMPRE a forma canônica -- garantida pelo banco, não
-- por convenção de quem escreve.
--
-- ORDEM OBRIGATÓRIA, e é isso que faz este arquivo funcionar em produção:
--   1) consolida cada grupo de linhas que compartilha identidade canônica
--      numa única linha, com as MESMAS regras que shared/leads.py já
--      implementa para o par (com/sem 9);
--   2) só DEPOIS o CHECK entra, proibindo as duas formas físicas que a
--      canonicalização recusa. Um CHECK antes do passo 1 rejeitaria a
--      própria migração de consolidação.
--
-- A fusão abaixo REIMPLEMENTA em PL/pgSQL as regras de
-- `shared/leads.py::_fundir` / `_vencedor_pausa` / `_FASE_RANK` porque a
-- consolidação de uma base inteira roda melhor como operação de conjunto no
-- banco do que como N chamadas do gate em Python. *** AS DUAS CÓPIAS DESTA
-- REGRA -- esta aqui e `shared/leads.py` -- PRECISAM ANDAR JUNTAS. Se uma
-- mudar (nova regra de desempate, novo campo coalescível, novo rank de
-- fase), a outra tem que mudar junto, ou a consolidação de produção e a
-- fusão do dia a dia do gate divergem silenciosamente. ***
--
-- UMA DIVERGÊNCIA DELIBERADA, não esquecimento: `_fundir`/`_persistir_
-- consolidacao` NUNCA tocam `last_inbound_at` -- a linha canônica que
-- sobrevive ao gate mantém o valor que ela já tinha, porque o gate grava
-- essa coluna a cada inbound de qualquer forma (não precisa de fusão para
-- ficar correta). A consolidação em massa abaixo não tem esse luxo -- é
-- uma operação única sobre um grupo que pode nunca mais ser revisitado --
-- então aplica `min(last_inbound_at)` do grupo inteiro, o conservador para
-- a janela de 24h da régua. As duas regras estão CERTAS cada uma no seu
-- contexto; não é uma cópia desatualizada da outra.
--
-- Por que migração nova em vez de editar uma das anteriores: o runner
-- (`shared/db.py` e `db/migrate.py`) pula migração pelo NOME do arquivo
-- registrado em `_migrations` e não guarda checksum -- editar uma já
-- aplicada não reexecuta nada em banco algum já migrado.

-- Canonicaliza um `leads_crm.phone` já existente (formato já validado pelo
-- CHECK original, `^[0-9]{8,15}$`) para a mesma forma que
-- `shared/phone.py::canonicalizar` produziria. Cobre os dois desvios
-- conhecidos desta tabela: zero de tronco colado no DDI (`550...`) e o 9º
-- dígito do celular. Números não-BR (não começam com 55) passam
-- inalterados, mesma regra do fallback de `canonicalizar`.
--
-- *** ESPELHA `shared/phone.py::canonicalizar`/`_sem_zero_de_tronco`. As
-- DUAS PRECISAM ANDAR JUNTAS. ***
CREATE OR REPLACE FUNCTION _migracao_014_canonico(bruto TEXT) RETURNS TEXT AS $$
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

-- Consolidação: um DO block, não uma única query gigante, para que a lógica
-- fique auditável linha a linha contra `_fundir`/`_vencedor_pausa` em
-- Python -- é mais fácil provar que as duas cópias concordam quando as duas
-- estão escritas na mesma forma imperativa passo a passo.
DO $$
DECLARE
    canonico              TEXT;
    linha                 leads_crm%ROWTYPE;
    v_pipedriveid         TEXT;
    v_name                TEXT;
    v_username            TEXT;
    v_email               TEXT;
    v_faturamento_mensal  TEXT;
    v_qualificacao_notas  TEXT;
    v_google_event_id     TEXT;
    v_source              lead_source;
    v_phase               lead_phase;
    v_phase_rank          INT;
    linha_rank            INT;
    v_followup_count      INT;
    v_last_inbound_at     TIMESTAMPTZ;
    v_agent_active        BOOLEAN;
    v_followup_active     BOOLEAN;
    v_agent_reactivate_at TIMESTAMPTZ;
    v_pausa_recencia      TIMESTAMPTZ;
    ganha_pausa           BOOLEAN;
    v_metadata            JSONB;
    v_telefones           TEXT[];
    linha_referencia      TEXT;
BEGIN
    FOR canonico IN
        SELECT _migracao_014_canonico(phone)
        FROM leads_crm
        GROUP BY _migracao_014_canonico(phone)
        HAVING count(*) > 1
    LOOP
        v_pipedriveid := NULL;
        v_name := NULL;
        v_username := NULL;
        v_email := NULL;
        v_faturamento_mensal := NULL;
        v_qualificacao_notas := NULL;
        v_google_event_id := NULL;
        v_source := NULL;
        v_phase := NULL;
        v_phase_rank := -2;
        v_followup_count := 0;
        v_last_inbound_at := NULL;
        v_agent_active := NULL;
        v_followup_active := NULL;
        v_agent_reactivate_at := NULL;
        v_pausa_recencia := NULL;
        v_metadata := '{}'::jsonb;
        v_telefones := '{}';
        linha_referencia := NULL;

        -- Do mais antigo pro mais recente: quem é processado por último, na
        -- lógica de COALESCE abaixo, é quem vence -- mesma regra de
        -- `_fundir` ("recente[campo] if not null else antiga[campo]"),
        -- generalizada de par para N linhas.
        FOR linha IN
            SELECT * FROM leads_crm
            WHERE _migracao_014_canonico(phone) = canonico
            ORDER BY last_interaction_at ASC NULLS FIRST
        LOOP
            v_telefones := array_append(v_telefones, linha.phone);
            IF linha.phone = canonico THEN
                linha_referencia := linha.phone;
            END IF;

            -- Campos de conteúdo coalescíveis -- mesma lista de
            -- `leads.py::_CAMPOS_COALESCIVEIS`.
            v_pipedriveid := COALESCE(linha.pipedriveid, v_pipedriveid);
            v_name := COALESCE(linha.name, v_name);
            v_username := COALESCE(linha.username, v_username);
            v_email := COALESCE(linha.email, v_email);
            v_faturamento_mensal := COALESCE(linha.faturamento_mensal, v_faturamento_mensal);
            v_qualificacao_notas := COALESCE(linha.qualificacao_notas, v_qualificacao_notas);
            v_google_event_id := COALESCE(linha.google_event_id, v_google_event_id);
            v_source := COALESCE(linha.source, v_source);

            -- Fase: a mais avançada vence, nunca a mais recente -- mesma
            -- regra de `leads.py::_FASE_RANK`/`_rank_fase`. NULL fica
            -- abaixo de qualquer fase real ou desconhecida.
            IF linha.phase IS NULL THEN
                linha_rank := -1;
            ELSE
                linha_rank := CASE linha.phase::text
                    WHEN 'agendou_sessao' THEN 5
                    WHEN 'desqualificado' THEN 4
                    WHEN 'perdido' THEN 4
                    WHEN 'qualificado' THEN 3
                    WHEN 'iniciou_conversa' THEN 2
                    WHEN 'formulario_preenchido' THEN 1
                    ELSE 0
                END;
            END IF;
            IF linha_rank >= v_phase_rank THEN
                v_phase_rank := linha_rank;
                v_phase := linha.phase;
            END IF;

            -- followup_count: o maior vence -- a escada já percorrida não
            -- pode ser esquecida por uma linha que nasceu com 0.
            v_followup_count := GREATEST(v_followup_count, COALESCE(linha.followup_count, 0));

            -- last_inbound_at: o MENOR vence -- é o conservador para a
            -- janela de 24h da régua (ancorar numa marca mais recente do
            -- que a real reabriria uma janela que já tinha fechado).
            IF linha.last_inbound_at IS NOT NULL
               AND (v_last_inbound_at IS NULL OR linha.last_inbound_at < v_last_inbound_at)
            THEN
                v_last_inbound_at := linha.last_inbound_at;
            END IF;

            -- Pausa: agent_active=false vence -- mesma regra de
            -- `leads.py::_vencedor_pausa`. Entre dois lados no mesmo
            -- estado (ambos pausados ou ambos ativos), o mais recente
            -- (last_interaction_at) desempata. agent_reactivate_at
            -- acompanha o MESMO vencedor, nunca é coalescido à parte.
            IF v_agent_active IS NULL THEN
                ganha_pausa := true;
            ELSIF linha.agent_active = false AND v_agent_active = true THEN
                ganha_pausa := true;
            ELSIF linha.agent_active = true AND v_agent_active = false THEN
                ganha_pausa := false;
            ELSE
                ganha_pausa := COALESCE(linha.last_interaction_at, '-infinity'::timestamptz)
                    >= COALESCE(v_pausa_recencia, '-infinity'::timestamptz);
            END IF;
            IF ganha_pausa THEN
                v_agent_active := linha.agent_active;
                v_followup_active := linha.followup_active;
                v_agent_reactivate_at := linha.agent_reactivate_at;
                v_pausa_recencia := linha.last_interaction_at;
            END IF;

            -- metadata: mescla chave a chave, o processado por último
            -- vence em conflito -- mesma regra de `_fundir_metadata`
            -- (`{**base, **topo}`). `linhas_fundidas` é recalculado à parte
            -- logo abaixo, porque o `||` sobrescreve arrays em vez de
            -- juntá-los.
            v_metadata := v_metadata || COALESCE(linha.metadata, '{}'::jsonb);
        END LOOP;

        -- linhas_fundidas: telefones físicos deste grupo, unidos com
        -- qualquer telefone já registrado como absorvido em fusões
        -- anteriores do gate (Task 3) em qualquer membro do grupo. Mesmo
        -- papel de `_fundir_metadata`, como a 012 já faz para
        -- `message_ids_absorvidos`.
        SELECT array_agg(DISTINCT valor ORDER BY valor)
        INTO v_telefones
        FROM (
            SELECT unnest(v_telefones) AS valor
            UNION
            SELECT jsonb_array_elements_text(
                COALESCE(l.metadata -> 'linhas_fundidas', '[]'::jsonb)
            )
            FROM leads_crm l
            WHERE _migracao_014_canonico(l.phone) = canonico
        ) t;
        v_metadata := v_metadata || jsonb_build_object('linhas_fundidas', to_jsonb(v_telefones));

        -- A linha física que sobrevive: a que já está na forma canônica,
        -- se existir; senão, promove a de last_interaction_at mais recente
        -- (renomeando seu phone para o canônico). last_interaction_at do
        -- sobrevivente NÃO é tocado -- mesmo comportamento de
        -- `_persistir_consolidacao`, que nunca escreve essa coluna.
        IF linha_referencia IS NULL THEN
            SELECT phone INTO linha_referencia
            FROM leads_crm
            WHERE _migracao_014_canonico(phone) = canonico
            ORDER BY last_interaction_at DESC NULLS LAST
            LIMIT 1;
        END IF;

        UPDATE leads_crm SET
            phone = canonico,
            pipedriveid = v_pipedriveid,
            name = v_name,
            username = v_username,
            email = v_email,
            faturamento_mensal = v_faturamento_mensal,
            qualificacao_notas = v_qualificacao_notas,
            google_event_id = v_google_event_id,
            source = v_source,
            phase = v_phase,
            followup_count = v_followup_count,
            last_inbound_at = v_last_inbound_at,
            agent_active = v_agent_active,
            followup_active = v_followup_active,
            agent_reactivate_at = v_agent_reactivate_at,
            metadata = v_metadata
        WHERE phone = linha_referencia;

        DELETE FROM leads_crm
        WHERE _migracao_014_canonico(phone) = canonico
          AND phone <> canonico;
    END LOOP;
END $$;

DROP FUNCTION _migracao_014_canonico(TEXT);

-- Passo 2, só agora que toda linha obedece: tranca a forma. O CHECK
-- original (`^[0-9]{8,15}$`, da 007) já barra `+` e máscara -- este
-- proíbe as duas formas físicas que `canonicalizar()` recusa como
-- identidade própria: o 9º dígito do celular e o zero de tronco. Uma
-- violação depois disto falha alto (ex.: o importador da Fase 4), que é o
-- comportamento desejado -- não silenciosamente cria duplicata de novo.
DO $$ BEGIN
    ALTER TABLE leads_crm
        ADD CONSTRAINT leads_crm_phone_canonico_check
        CHECK (phone !~ '^55[0-9]{2}9[0-9]{8}$' AND phone !~ '^550');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
