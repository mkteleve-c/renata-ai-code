"""CLI de `migrar_supabase.py` (Fase 5, Task 1) -- a "casca" em torno da
biblioteca já testada na Fase 4: leitura REST paginada do Supabase,
`--dry-run` (padrão) / `--executar` mutuamente exclusivos, relatório em
disco e resumo em stdout.

O Supabase é sempre um `httpx.MockTransport` -- nenhum teste aqui toca a
rede real (mesmo padrão de `tests/unit/test_pipedrive.py`,
`test_evolution_client.py`). A escrita de verdade (`--executar`) é testada
contra o Postgres real de dev (porta 5440), como o resto da camada de banco
deste projeto -- nunca monkeypatchada.

Todo telefone de teste usa o prefixo `5511990009` -- 12 dígitos, forma
canônica válida, não colide com o prefixo `5511990000` que
`tests/integration/test_migrar_supabase.py` já usa. O `autouse` fixture
`limpar` varre esse prefixo inteiro antes e depois de cada teste.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from scripts.migrar_supabase import (
    _FONTE_MIGRACAO_DESCARTE,
    _TABELA_LEADS,
    _TAMANHO_PAGINA,
    MigracaoAbortada,
    _buscar_paginado,
    _parse_args,
    executar_migracao,
    main,
)
from whatsapp_langchain.shared.db import get_pool

_PREFIXO_TESTE = "5511990009"
_URL_FALSA = "https://fake-supabase.test"
_CHAVE_FALSA = "segredo-nao-pode-vazar-8f6c1e"


@pytest.fixture(autouse=True)
async def limpar():
    async def apagar():
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "delete from leads_crm where phone like %s", (f"{_PREFIXO_TESTE}%",)
            )
            await conn.execute(
                "delete from leads_descartados where payload->>'fonte_migracao' = %s",
                (_FONTE_MIGRACAO_DESCARTE,),
            )

    await apagar()
    yield
    await apagar()


@pytest.fixture(autouse=True)
def credenciais_supabase(monkeypatch):
    """Toda chamada de `executar_migracao`/`main` neste arquivo já nasce com
    URL e chave FALSAS -- nenhum teste precisa (nem deve) tocar credencial
    real. Testes que exercitam o caminho de credencial ausente removem essas
    variáveis explicitamente, no próprio corpo do teste."""
    monkeypatch.setenv("SUPABASE_URL", _URL_FALSA)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", _CHAVE_FALSA)


async def _contar_leads_prefixo() -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from leads_crm where phone like %s",
            (f"{_PREFIXO_TESTE}%",),
        )
        linha = await cur.fetchone()
    assert linha is not None
    return linha[0]


def _lead(phone: str | None, *, phase: str = "iniciou_conversa") -> dict:
    """Um registro cru no formato que o REST do Supabase devolve para uma
    linha de `leads_crm` -- mesmos nomes de campo de `LinhaOrigem`, sem
    renomeação (ver `_linha_origem_de_registro`)."""
    return {
        "phone": phone,
        "phase": phase,
        "created_at": "2026-07-01T12:00:00+00:00",
        "last_interaction_at": "2026-07-02T12:00:00+00:00",
        "pipedriveid": None,
        "email": None,
        "name": "Fulano de Teste",
        "username": None,
        "source": "whatsapp_direct",
        "followup_count": 0,
        "agent_active": True,
        "followup_active": True,
        "agent_reactivate_at": None,
        "metadata": {},
    }


def _servidor_postgrest(
    tabelas: dict[str, list[dict]],
    *,
    total_forcado: dict[str, int] | None = None,
    status_forcado: int | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Fábrica de handler para `httpx.MockTransport` -- imita o suficiente
    do PostgREST (paginação via `Range`/`Range-Unit`, total via
    `Content-Range`) para exercitar `_buscar_paginado` sem tocar a rede.

    A cada chamada, confirma que a credencial viajou SÓ em header
    (`apikey`/`Authorization`), nunca em query string -- é a checagem que
    pegaria, na hora, um `httpx.AsyncClient(params={"apikey": ...})` por
    engano (ver o docstring do módulo sobre onde a credencial pode vazar).

    `total_forcado` mente o total do `Content-Range` de uma tabela
    específica -- é como o teste de divergência simula uma origem cujo
    header não bate com o que ela realmente devolveu.
    """
    total_forcado = total_forcado or {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("apikey") == _CHAVE_FALSA
        assert request.headers.get("authorization") == f"Bearer {_CHAVE_FALSA}"
        assert _CHAVE_FALSA not in str(request.url)

        if status_forcado is not None:
            return httpx.Response(status_forcado, json={"message": "erro simulado"})

        tabela = request.url.path.rsplit("/", 1)[-1]
        linhas = tabelas.get(tabela, [])

        range_header = request.headers.get("range", "0-999")
        inicio_s, fim_s = range_header.split("-")
        inicio, fim = int(inicio_s), int(fim_s)
        pagina = linhas[inicio : fim + 1]

        total_real = len(linhas)
        total = total_forcado.get(tabela, total_real)
        if total_real == 0:
            content_range = f"*/{total}"
        else:
            fim_efetivo = max(inicio, min(fim, total_real - 1))
            content_range = f"{inicio}-{fim_efetivo}/{total}"

        return httpx.Response(
            200, json=pagina, headers={"Content-Range": content_range}
        )

    return handler


# --- --dry-run é o padrão, e não escreve -------------------------------------


async def test_dry_run_nao_escreve_nada_no_banco(tmp_path):
    handler = _servidor_postgrest(
        {
            "leads_crm": [
                _lead(f"{_PREFIXO_TESTE}01"),
                _lead(f"{_PREFIXO_TESTE}02"),
            ],
            "n8n_chat_histories": [],
        }
    )

    antes = await _contar_leads_prefixo()
    codigo = await executar_migracao(
        executar=False,
        transporte=httpx.MockTransport(handler),
        caminho_relatorio=tmp_path / "relatorio_migracao.md",
    )
    depois = await _contar_leads_prefixo()

    assert codigo == 0
    assert antes == 0
    assert depois == 0
    assert (tmp_path / "relatorio_migracao.md").exists()


def test_sem_nenhuma_flag_o_padrao_e_nao_executar():
    """`--dry-run` é o padrão -- sem argumento nenhum, `args.executar` fica
    `False`. Não existe um terceiro estado nem uma leitura implícita de
    "quis dizer --executar"."""
    args = _parse_args([])
    assert args.executar is False


def test_main_traduz_flags_para_executar_corretamente(monkeypatch):
    """Fecha o elo entre `_parse_args` e `executar_migracao` -- os dois já
    são testados em isolamento acima, mas nada garante que `main()` liga um
    no outro do jeito certo. Monkeypatcha `executar_migracao` por um dublê
    que só registra o valor recebido, sem tocar rede nem banco -- se `main()`
    passasse `executar=True` incondicionalmente (o mutante "--dry-run
    deixando de ser o padrão"), a primeira chamada (sem flag) já reprovaria.
    """
    import scripts.migrar_supabase as migrar_supabase_mod

    chamadas: list[bool] = []

    async def _dublê(*, executar: bool, **_kwargs: object) -> int:
        chamadas.append(executar)
        return 0

    monkeypatch.setattr(migrar_supabase_mod, "executar_migracao", _dublê)

    assert main([]) == 0
    assert main(["--dry-run"]) == 0
    assert main(["--executar"]) == 0

    assert chamadas == [False, False, True]


# --- --executar escreve -------------------------------------------------


async def test_executar_grava_leads_no_banco(tmp_path):
    handler = _servidor_postgrest(
        {
            "leads_crm": [_lead(f"{_PREFIXO_TESTE}01", phase="qualificado")],
            "n8n_chat_histories": [],
        }
    )

    codigo = await executar_migracao(
        executar=True,
        transporte=httpx.MockTransport(handler),
        caminho_relatorio=tmp_path / "relatorio_migracao.md",
    )

    assert codigo == 0
    assert await _contar_leads_prefixo() == 1


# --- --dry-run e --executar são mutuamente exclusivos ------------------------


def test_dry_run_e_executar_juntos_sao_recusados():
    with pytest.raises(SystemExit) as excinfo:
        _parse_args(["--dry-run", "--executar"])
    assert excinfo.value.code == 2


def test_main_com_dry_run_e_executar_juntos_recusa_antes_de_ler_credencial(
    monkeypatch,
):
    # Sem credencial nenhuma no ambiente -- se o parsing não abortasse
    # primeiro, o teste falharia por outro motivo (CredencialAusente), não
    # pelo SystemExit esperado. É a prova de que a recusa acontece ANTES de
    # qualquer leitura.
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        main(["--dry-run", "--executar"])
    assert excinfo.value.code == 2


# --- Paginação -----------------------------------------------------------


async def test_paginacao_traz_tudo_e_nao_para_na_primeira_pagina():
    """PostgREST corta em 1.000 por padrão -- este teste usa o tamanho de
    página real do módulo (`_TAMANHO_PAGINA`) e um total que exige mais de
    uma página, para provar que a leitura não para (nem finge terminar) na
    primeira."""
    total = _TAMANHO_PAGINA + 200
    leads = [{**_lead(f"linha-{i}"), "phone": f"linha-{i}"} for i in range(total)]
    handler = _servidor_postgrest({"leads_crm": leads})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resultado = await _buscar_paginado(
            client, _URL_FALSA, _CHAVE_FALSA, _TABELA_LEADS, order_by="phone"
        )

    assert len(resultado) == total


async def test_content_range_divergente_aborta():
    """A leitura para cedo (página menor que o tamanho pedido) MAS o
    `Content-Range` mentiu um total maior -- a checagem final tem que
    abortar, não seguir em frente achando que terminou."""
    handler = _servidor_postgrest(
        {"leads_crm": [_lead(f"{_PREFIXO_TESTE}01")]},
        total_forcado={"leads_crm": 999},
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MigracaoAbortada, match="999"):
            await _buscar_paginado(
                client, _URL_FALSA, _CHAVE_FALSA, _TABELA_LEADS, order_by="phone"
            )


# --- Credencial ------------------------------------------------------------


async def test_credencial_ausente_da_erro_legivel_e_nao_vaza_valor(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    codigo = await executar_migracao(executar=False)

    assert codigo == 1
    saida = capsys.readouterr()
    assert "SUPABASE_URL" in saida.err
    assert "SUPABASE_SERVICE_ROLE_KEY" in saida.err
    # Nada a vazar quando a variável está ausente -- mas a chave falsa do
    # fixture `credenciais_supabase` nunca deveria aparecer aqui de jeito
    # nenhum (ela foi explicitamente removida acima).
    assert _CHAVE_FALSA not in saida.err
    assert _CHAVE_FALSA not in saida.out


async def test_erro_do_supabase_nunca_vaza_a_credencial(capsys):
    """A credencial ESTÁ presente (fixture `credenciais_supabase`) e o
    Supabase responde erro -- a mensagem impressa (stdout + stderr) não
    pode conter o valor da chave em hipótese nenhuma."""
    handler = _servidor_postgrest({}, status_forcado=500)

    codigo = await executar_migracao(
        executar=False, transporte=httpx.MockTransport(handler)
    )

    assert codigo == 1
    saida = capsys.readouterr()
    assert _CHAVE_FALSA not in saida.out
    assert _CHAVE_FALSA not in saida.err


async def test_falha_de_transporte_nunca_vaza_a_credencial(capsys):
    """Mesma garantia, agora para uma falha de TRANSPORTE (sem resposta HTTP
    nenhuma) -- é o caminho que capturaria `exc.request`/`str(exc)` do httpx
    por engano, o que incluiria os headers da requisição que falhou."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexão recusada", request=request)

    codigo = await executar_migracao(
        executar=False, transporte=httpx.MockTransport(handler)
    )

    assert codigo == 1
    saida = capsys.readouterr()
    assert _CHAVE_FALSA not in saida.out
    assert _CHAVE_FALSA not in saida.err


# --- Resumo em stdout --------------------------------------------------------


async def test_resumo_no_stdout_mostra_totais_e_descartes_por_motivo(tmp_path, capsys):
    handler = _servidor_postgrest(
        {
            "leads_crm": [_lead(f"{_PREFIXO_TESTE}01"), _lead("null")],
            "n8n_chat_histories": [],
        }
    )

    codigo = await executar_migracao(
        executar=False,
        transporte=httpx.MockTransport(handler),
        caminho_relatorio=tmp_path / "relatorio_migracao.md",
    )

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "Total na origem: 2" in saida
    assert "Descartados: 1" in saida
    assert "telefone_ausente: 1" in saida
