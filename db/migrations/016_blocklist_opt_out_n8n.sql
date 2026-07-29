-- 016_blocklist_opt_out_n8n.sql
-- Os opt-outs que só existiam hardcoded no n8n, no nó `Filtro e Permissao
-- v002` do workflow `#1 Agente SDR | 10/02/26 | V2.2`. São pessoas que
-- pediram para parar de receber. Sem esta migração elas voltam a receber no
-- cutover -- e a régua de follow-up é o único caminho do sistema que fala
-- sem o lead ter falado primeiro.
--
-- 26 números únicos na origem (29 linhas, 3 duplicadas), 52 linhas aqui.
--
-- Por que DUAS linhas por pessoa: o n8n bloqueava por SUFIXO
-- (`currentPhone.endsWith(blocked)`), sem DDI, então as duas formas do
-- mesmo celular casavam com a mesma entrada. Aqui `blocklist.phone` é
-- chave primária e o casamento é exato. O gate (`shared/leads.py:301`) e a
-- régua (`worker/followup.py:155`) consultam as duas variações, mas
-- gravar só uma delas deixaria a outra passar por qualquer caminho que
-- venha a consultar só a canônica. Gravar as duas é o que torna o bloqueio
-- indiferente à forma que chegar.
--
-- As formas foram geradas por `shared/phone.py::canonicalizar` +
-- `variacoes`, não por normalização reescrita à mão -- é o mesmo código
-- que o gate usa para montar a consulta. Nenhum dos 26 foi recusado.
--
-- ON CONFLICT DO NOTHING: a migração roda no startup de toda API e a
-- 016 pode reencontrar linhas que a importação do Supabase já tenha
-- gravado. Reaplicar não pode falhar nem sobrescrever um `motivo` mais
-- específico que já esteja lá.

INSERT INTO blocklist (phone, motivo) VALUES
    ('5545998179085', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('554598179085', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5521996182653', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('552196182653', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511986096863', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551186096863', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5554996473605', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('555496473605', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511940075855', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551140075855', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5521997483733', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('552197483733', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5551981645165', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('555181645165', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511980184427', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551180184427', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5521995968026', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('552195968026', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5571991080505', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('557191080505', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511949534480', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551149534480', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5537991411790', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('553791411790', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5521979081010', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('552179081010', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5541987517400', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('554187517400', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5517991134887', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551791134887', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5517996546740', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551796546740', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5554996915385', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('555496915385', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511976655609', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551176655609', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511940569963', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551140569963', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511975406284', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551175406284', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5583999900691', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('558399900691', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511987479763', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551187479763', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5512982390831', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551282390831', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5548998656969', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('554898656969', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5519992463540', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551992463540', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('5511988671528', 'opt-out importado do n8n (Filtro e Permissao v002)'),
    ('551188671528', 'opt-out importado do n8n (Filtro e Permissao v002)')
ON CONFLICT (phone) DO NOTHING;
