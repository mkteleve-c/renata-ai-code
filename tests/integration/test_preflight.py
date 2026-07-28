"""`scripts/preflight_cutover.py` (Fase 5, Task 2) -- as doze checagens do
pré-voo, cada uma isolada com um dublê para cada modo de falha.

Serviços externos (Evolution, Google, Pipedrive, OpenRouter) são sempre
`httpx.MockTransport` -- nenhum teste aqui toca a rede real, mesmo padrão de
`test_migrar_cli.py`/`test_pipedrive.py`/`test_google_calendar.py`. As três
checagens de banco (migrações, CHECK, leads_crm vazia) rodam contra o
Postgres real de dev (porta 5440) dentro de uma transação sempre revertida
com `psycopg.Rollback()` -- mesmo padrão de `test_migracao_014.py`: nenhuma
delas escreve de verdade, e a explicação é a mesma doutrina do módulo sob
teste (nenhuma checagem escreve).

Todo telefone de teste usa o prefixo `5511900099` -- não colide com nenhum
outro prefixo já em uso nesta suíte (ver os módulos citados acima).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import psycopg
from pydantic import SecretStr

from scripts.preflight_cutover import (
    Checagem,
    Transportes,
    _imprimir_tabela,
    checar_check_constraint,
    checar_evolution_alcancavel,
    checar_followup_desligado,
    checar_google_calendar,
    checar_handover_phone,
    checar_internal_service_token,
    checar_leads_crm_vazia,
    checar_migracoes,
    checar_openrouter,
    checar_outbound_evolution,
    checar_pipedrive,
    checar_webhook_secret,
    codigo_de_saida,
    main,
    rodar_preflight,
)
from whatsapp_langchain.shared.config import MIN_PRODUCTION_SECRET_LENGTH, Settings
from whatsapp_langchain.shared.db import get_pool

_CHAVE_EVOLUTION_FALSA = "evo-key-nao-pode-vazar-8f6c1e"
_TOKEN_PIPEDRIVE_FALSO = "pd-token-nao-pode-vazar-77a2f0"
_REFRESH_TOKEN_FALSO = "gcal-refresh-nao-pode-vazar-9c3d"
_CHAVE_OPENROUTER_FALSA = "sk-or-nao-pode-vazar-4b1e"


def _montar_transport_google(
    handler_calendar: Callable, resposta_token: Callable | None = None
) -> httpx.MockTransport:
    """MockTransport que separa o endpoint de token (`oauth2.googleapis.com`)
    da API de calendário -- mesmo helper de `tests/unit/test_google_calendar.py`."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            if resposta_token is not None:
                return resposta_token(request)
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return await handler_calendar(request)

    return httpx.MockTransport(handler)


# --- Migrações -------------------------------------------------------------


async def test_migracoes_aplicadas_no_banco_de_dev_passa():
    pool = await get_pool()
    async with pool.connection() as conn:
        c = await checar_migracoes(conn)
    assert c.ok is True


async def test_migracao_ausente_falha_e_nomeia_o_arquivo():
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "delete from _migrations where name = %s",
                ("015_legacy_chat_history.sql",),
            )
            c = await checar_migracoes(conn)

            assert c.ok is False
            assert "015_legacy_chat_history.sql" in c.detalhe

            raise psycopg.Rollback()


async def test_migrations_dir_vazio_falha_em_vez_de_passar_vacuamente(
    tmp_path, monkeypatch
):
    """`esperadas=[]` (diretório errado/apagado) não pode virar `True :: 0/0
    aplicadas` -- é o cenário em que a checagem é incapaz de confirmar
    QUALQUER coisa, e ainda assim seria a única das doze a cobrir a 015. Sem
    a guarda, este teste reprovaria com `c.ok is True`."""
    import scripts.preflight_cutover as preflight_mod

    monkeypatch.setattr(preflight_mod, "MIGRATIONS_DIR", tmp_path)

    pool = await get_pool()
    async with pool.connection() as conn:
        c = await checar_migracoes(conn)

    assert c.ok is False
    assert str(tmp_path) in c.detalhe


# --- CHECK de leads_crm.phone -----------------------------------------------


async def test_check_constraint_vivo_com_as_tres_clausulas_passa():
    pool = await get_pool()
    async with pool.connection() as conn:
        c = await checar_check_constraint(conn)
    assert c.ok is True


async def test_check_constraint_ausente_falha():
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
            )
            c = await checar_check_constraint(conn)

            assert c.ok is False
            assert "não existe" in c.detalhe

            raise psycopg.Rollback()


async def test_check_constraint_com_clausula_faltando_no_banco_vivo_falha():
    """A prova de que a leitura é do banco, não do arquivo de migração: a
    constraint viva é recriada com só UMA das três cláusulas -- o arquivo
    `db/migrations/014_uma_linha_por_pessoa.sql` continua intacto, com as
    três. Se `checar_check_constraint` lesse o arquivo em vez de
    `pg_get_constraintdef`, este teste passaria escondendo a divergência --
    exatamente o modo de falha que `test_migracao_014.py::
    test_check_ddl_do_arquivo_e_executado_...` já documentou para a própria
    migração.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
            )
            await conn.execute(
                "alter table leads_crm add constraint "
                "leads_crm_phone_canonico_check check (phone !~ '^550')"
            )
            c = await checar_check_constraint(conn)

            assert c.ok is False
            assert "55[0-9]{2}9[0-9]{8}" in c.detalhe
            assert "[0-9]{10,11}" in c.detalhe

            raise psycopg.Rollback()


async def test_check_constraint_invertida_falha_mesmo_com_as_tres_clausulas():
    """A constraint viva recriada com `~` em vez de `!~` -- as três cláusulas
    (o texto da regex) continuam presentes na definição, mas a constraint
    passa a EXIGIR o formato que deveria PROIBIR (aceita em vez de rejeitar).
    Uma checagem por substring pura (`padrao in definicao`) não vê essa
    troca -- o texto do padrão não muda, só o operador antes dele. Esta é a
    prova de que `checar_check_constraint` de fato lê o operador, não só a
    presença do padrão -- ver `_clausula_nega_com_padrao`."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "alter table leads_crm drop constraint leads_crm_phone_canonico_check"
            )
            await conn.execute(
                "alter table leads_crm add constraint "
                "leads_crm_phone_canonico_check check ("
                "phone ~ '^55[0-9]{2}9[0-9]{8}$' "
                "AND phone ~ '^550' "
                "AND phone ~ '^[0-9]{10,11}$'"
                ") not valid"
            )
            c = await checar_check_constraint(conn)

            assert c.ok is False
            assert "invertida" in c.detalhe

            raise psycopg.Rollback()


# --- leads_crm vazia ---------------------------------------------------------


async def test_leads_crm_vazia_passa():
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("delete from leads_crm")
            c = await checar_leads_crm_vazia(conn)

            assert c.ok is True

            raise psycopg.Rollback()


async def test_leads_crm_com_linha_falha_o_preflight():
    """Também é a mutação "checagem que falha vira aviso": se
    `checar_leads_crm_vazia` deixasse de devolver `ok=False` com a tabela
    não vazia (virando só um aviso), este teste reprovaria."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "insert into leads_crm (phone, name) values (%s, %s)",
                ("551190009901", "Teste Preflight"),
            )
            c = await checar_leads_crm_vazia(conn)

            assert c.ok is False
            assert "1" in c.detalhe

            raise psycopg.Rollback()


# --- OUTBOUND_MODE + canal Evolution -----------------------------------------


def test_outbound_falha_se_nao_for_real():
    s = Settings(
        outbound_mode="mock",
        evolution_base_url="https://evo.test",
        evolution_api_key="k",
        evolution_instance="i",
    )
    c = checar_outbound_evolution(s)
    assert c.ok is False
    assert "real" in c.detalhe


def test_outbound_falha_se_canal_intocado():
    c = checar_outbound_evolution(Settings(outbound_mode="real"))
    assert c.ok is False


def test_outbound_falha_se_canal_incompleto():
    c = checar_outbound_evolution(
        Settings(outbound_mode="real", evolution_base_url="https://evo.test")
    )
    assert c.ok is False
    assert "EVOLUTION_API_KEY" in c.detalhe
    assert "EVOLUTION_INSTANCE" in c.detalhe


def test_outbound_passa_completo():
    c = checar_outbound_evolution(
        Settings(
            outbound_mode="real",
            evolution_base_url="https://evo.test",
            evolution_api_key="k",
            evolution_instance="i",
        )
    )
    assert c.ok is True


# --- EVOLUTION_WEBHOOK_SECRET -------------------------------------------------


def test_webhook_secret_ausente_falha():
    assert checar_webhook_secret(Settings(evolution_webhook_secret="")).ok is False


def test_webhook_secret_curto_falha():
    c = checar_webhook_secret(Settings(evolution_webhook_secret="x" * 10))
    assert c.ok is False


def test_webhook_secret_forte_passa():
    c = checar_webhook_secret(
        Settings(evolution_webhook_secret="x" * MIN_PRODUCTION_SECRET_LENGTH)
    )
    assert c.ok is True


def test_webhook_secret_detalhe_nunca_contem_o_valor():
    segredo = "s3gr3d0-de-verdade-que-nao-pode-vazar-32c"
    c = checar_webhook_secret(Settings(evolution_webhook_secret=segredo))
    assert segredo not in c.detalhe


# --- Evolution alcançável ------------------------------------------------


def _settings_evolution(**kwargs) -> Settings:
    return Settings(
        evolution_base_url="https://evo.test",
        evolution_api_key=_CHAVE_EVOLUTION_FALSA,
        evolution_instance="instancia-teste",
        **kwargs,
    )


async def test_evolution_nao_configurada_falha():
    c = await checar_evolution_alcancavel(Settings(), None)
    assert c.ok is False


async def test_evolution_instancia_conectada_passa():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == _CHAVE_EVOLUTION_FALSA
        assert _CHAVE_EVOLUTION_FALSA not in str(request.url)
        return httpx.Response(200, json=[{"name": "instancia-teste", "status": "open"}])

    c = await checar_evolution_alcancavel(
        _settings_evolution(), httpx.MockTransport(handler)
    )
    assert c.ok is True


async def test_evolution_instancia_desconectada_falha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"name": "instancia-teste", "status": "close"}]
        )

    c = await checar_evolution_alcancavel(
        _settings_evolution(), httpx.MockTransport(handler)
    )
    assert c.ok is False
    assert "close" in c.detalhe


async def test_evolution_instancia_nao_encontrada_falha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "outra-instancia", "status": "open"}])

    c = await checar_evolution_alcancavel(
        _settings_evolution(), httpx.MockTransport(handler)
    )
    assert c.ok is False


async def test_evolution_http_erro_falha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="erro interno")

    c = await checar_evolution_alcancavel(
        _settings_evolution(), httpx.MockTransport(handler)
    )
    assert c.ok is False
    assert "500" in c.detalhe


async def test_evolution_falha_de_transporte_falha():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexão recusada", request=request)

    c = await checar_evolution_alcancavel(
        _settings_evolution(), httpx.MockTransport(handler)
    )
    assert c.ok is False


async def test_evolution_falha_nunca_vaza_a_api_key(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="erro interno")

    c = await checar_evolution_alcancavel(
        _settings_evolution(), httpx.MockTransport(handler)
    )
    _imprimir_tabela([c])

    saida = capsys.readouterr().out
    assert _CHAVE_EVOLUTION_FALSA not in saida


# --- Google Calendar -------------------------------------------------------


def _settings_google(**kwargs) -> Settings:
    return Settings(
        google_client_id="cid",
        google_client_secret="csecret",
        google_refresh_token=_REFRESH_TOKEN_FALSO,
        google_calendar_id="silvio@exemplo.com",
        **kwargs,
    )


async def test_google_nao_configurado_falha():
    c = await checar_google_calendar(Settings(), None)
    assert c.ok is False


async def test_google_refresh_token_valido_passa():
    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    transporte = _montar_transport_google(api)
    c = await checar_google_calendar(_settings_google(), transporte)
    assert c.ok is True


async def test_google_refresh_token_invalido_falha():
    def token_ruim(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    transporte = _montar_transport_google(api, resposta_token=token_ruim)
    c = await checar_google_calendar(_settings_google(), transporte)
    assert c.ok is False
    assert "400" in c.detalhe


async def test_google_nunca_faz_post_de_escrita_no_calendario():
    """A checagem só pode listar -- um POST em `/events` (criar_evento)
    aqui seria escrever na agenda do Silvio. O handler falha o teste se
    receber qualquer requisição que não seja GET (ou o POST do refresh de
    token, que é autenticação, não dado de agenda)."""

    async def api(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "checagem de calendário não pode escrever"
        return httpx.Response(200, json={"items": []})

    transporte = _montar_transport_google(api)
    c = await checar_google_calendar(_settings_google(), transporte)
    assert c.ok is True


# --- Pipedrive ---------------------------------------------------------------


def _settings_pipedrive(**kwargs) -> Settings:
    return Settings(
        pipedrive_api_token=_TOKEN_PIPEDRIVE_FALSO,
        pipedrive_stage_qualificado=12,
        pipedrive_stage_agendado=13,
        **kwargs,
    )


async def test_pipedrive_token_ausente_falha():
    c = await checar_pipedrive(Settings(), None)
    assert c.ok is False


async def test_pipedrive_estagios_presentes_passa():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-token"] == _TOKEN_PIPEDRIVE_FALSO
        assert _TOKEN_PIPEDRIVE_FALSO not in str(request.url)
        return httpx.Response(
            200,
            json={"success": True, "data": [{"id": 12}, {"id": 13}, {"id": 99}]},
        )

    c = await checar_pipedrive(_settings_pipedrive(), httpx.MockTransport(handler))
    assert c.ok is True


async def test_pipedrive_estagio_faltando_falha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": [{"id": 12}]})

    c = await checar_pipedrive(_settings_pipedrive(), httpx.MockTransport(handler))
    assert c.ok is False
    assert "13" in c.detalhe


async def test_pipedrive_http_erro_falha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    c = await checar_pipedrive(_settings_pipedrive(), httpx.MockTransport(handler))
    assert c.ok is False


async def test_pipedrive_falha_nunca_vaza_o_token(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    c = await checar_pipedrive(_settings_pipedrive(), httpx.MockTransport(handler))
    _imprimir_tabela([c])

    saida = capsys.readouterr().out
    assert _TOKEN_PIPEDRIVE_FALSO not in saida


# --- INTERNAL_SERVICE_TOKEN --------------------------------------------------


def test_internal_service_token_ausente_falha():
    assert (
        checar_internal_service_token(Settings(internal_service_token="")).ok is False
    )


def test_internal_service_token_fraco_falha():
    c = checar_internal_service_token(Settings(internal_service_token="curto"))
    assert c.ok is False


def test_internal_service_token_forte_passa():
    c = checar_internal_service_token(
        Settings(internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH)
    )
    assert c.ok is True


# --- FOLLOWUP_ENABLED ----------------------------------------------------


def test_followup_ligado_falha():
    assert checar_followup_desligado(Settings(followup_enabled=True)).ok is False


def test_followup_desligado_passa():
    assert checar_followup_desligado(Settings(followup_enabled=False)).ok is True


# --- HANDOVER_NOTIFY_PHONE -----------------------------------------------


def test_handover_ausente_falha():
    assert checar_handover_phone(Settings(handover_notify_phone="")).ok is False


def test_handover_nao_canonicalizavel_falha():
    c = checar_handover_phone(Settings(handover_notify_phone="não sei"))
    assert c.ok is False


def test_handover_valido_passa():
    c = checar_handover_phone(Settings(handover_notify_phone="+5511999998888"))
    assert c.ok is True


# --- OPENROUTER_API_KEY ----------------------------------------------------


def _settings_openrouter(**kwargs) -> Settings:
    return Settings(openrouter_api_key=SecretStr(_CHAVE_OPENROUTER_FALSA), **kwargs)


async def test_openrouter_ausente_falha():
    c = await checar_openrouter(Settings(openrouter_api_key=None), None)
    assert c.ok is False


async def test_openrouter_chave_valida_passa():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {_CHAVE_OPENROUTER_FALSA}"
        assert _CHAVE_OPENROUTER_FALSA not in str(request.url)
        return httpx.Response(200, json={"data": {"label": "teste"}})

    c = await checar_openrouter(_settings_openrouter(), httpx.MockTransport(handler))
    assert c.ok is True


async def test_openrouter_chave_invalida_falha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "no auth credentials"})

    c = await checar_openrouter(_settings_openrouter(), httpx.MockTransport(handler))
    assert c.ok is False
    assert "401" in c.detalhe


async def test_openrouter_falha_nunca_vaza_a_chave(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "no auth credentials"})

    c = await checar_openrouter(_settings_openrouter(), httpx.MockTransport(handler))
    _imprimir_tabela([c])

    saida = capsys.readouterr().out
    assert _CHAVE_OPENROUTER_FALSA not in saida


# --- Código de saída -----------------------------------------------------


def test_codigo_de_saida_zero_quando_tudo_ok():
    checagens = [Checagem("a", True, ""), Checagem("b", True, "")]
    assert codigo_de_saida(checagens) == 0


def test_codigo_de_saida_nao_zero_quando_alguma_falha():
    checagens = [Checagem("a", True, ""), Checagem("b", False, "motivo")]
    assert codigo_de_saida(checagens) == 1


def test_codigo_de_saida_nao_zero_quando_todas_falham():
    checagens = [Checagem("a", False, "x"), Checagem("b", False, "y")]
    assert codigo_de_saida(checagens) == 1


# --- Composição: rodar_preflight ------------------------------------------


async def test_rodar_preflight_roda_as_doze_checagens_sem_tocar_rede_real():
    """Nenhuma checagem foi esquecida na lista de `rodar_preflight`, e
    nenhuma delas tenta a rede real -- todo serviço externo é um dublê. O
    resultado das três checagens de banco reflete o estado real do dev, de
    propósito: é o que prova que `rodar_preflight` de fato passa o `pool`
    adiante em vez de pular essas três."""

    def handler_evolution(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "i", "status": "open"}])

    async def handler_google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    def handler_pipedrive(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "data": [{"id": 12}, {"id": 13}]}
        )

    def handler_openrouter(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    settings = Settings(
        outbound_mode="real",
        evolution_base_url="https://evo.test",
        evolution_api_key="k",
        evolution_instance="i",
        evolution_webhook_secret="x" * MIN_PRODUCTION_SECRET_LENGTH,
        google_client_id="cid",
        google_client_secret="cs",
        google_refresh_token="rt",
        google_calendar_id="c@exemplo.com",
        pipedrive_api_token="tok",
        pipedrive_stage_qualificado=12,
        pipedrive_stage_agendado=13,
        internal_service_token="x" * MIN_PRODUCTION_SECRET_LENGTH,
        followup_enabled=False,
        handover_notify_phone="+5511999998888",
        openrouter_api_key=SecretStr("sk-x"),
    )
    transportes = Transportes(
        evolution=httpx.MockTransport(handler_evolution),
        google=_montar_transport_google(handler_google),
        pipedrive=httpx.MockTransport(handler_pipedrive),
        openrouter=httpx.MockTransport(handler_openrouter),
    )
    pool = await get_pool()

    checagens = await rodar_preflight(
        settings=settings, pool=pool, transportes=transportes
    )

    assert len(checagens) == 12
    assert len({c.nome for c in checagens}) == 12


# --- main(): wiring e propagação do código de saída -------------------------


async def _pool_dublê():
    """`main()` chama `asyncio.run()` -- um loop novo a cada chamada. O pool
    real é um singleton aberto no loop da suíte de testes (pytest-asyncio
    cria um por função); reusá-lo dentro de um loop diferente trava. Como o
    dublê de `rodar_preflight` abaixo nunca toca o `pool` de verdade, um
    sentinela basta -- mesma razão pela qual o dublê de `executar_migracao`
    em `test_migrar_cli.py` nunca chama `get_pool()`."""
    return "pool-falso"


def test_main_retorna_0_quando_todas_passam(monkeypatch):
    import scripts.preflight_cutover as mod

    async def _dublê(*, settings, pool, transportes=None):
        return [Checagem("x", True, "ok")]

    monkeypatch.setattr(mod, "rodar_preflight", _dublê)
    monkeypatch.setattr(mod, "get_pool", _pool_dublê)

    assert main() == 0


def test_main_retorna_1_quando_alguma_falha(monkeypatch):
    """É a segunda linha de defesa contra a mutação "código de saída sempre
    zero" -- a primeira é `test_codigo_de_saida_nao_zero_quando_alguma_falha`,
    que testa `codigo_de_saida` isolado. Esta fecha o elo até `main()`."""
    import scripts.preflight_cutover as mod

    async def _dublê(*, settings, pool, transportes=None):
        return [Checagem("x", False, "motivo")]

    monkeypatch.setattr(mod, "rodar_preflight", _dublê)
    monkeypatch.setattr(mod, "get_pool", _pool_dublê)

    assert main() == 1
