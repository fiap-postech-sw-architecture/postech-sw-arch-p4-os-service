"""O coletor da API contra o PostgreSQL: o que le, o cache e a raspagem que nao pendura.

O ``/metrics`` precisa responder bem antes do ``scrape_timeout`` de 10 s do
Prometheus mesmo com o banco mudo ou lento: do contrario o alvo vira ``up=0`` justo
no incidente do banco. Os cenarios aqui sao os que a revisao dos outros dois
servicos achou: o banco que nao responde na abertura, o que emudece com a conexao
ja aberta (o prazo do servidor nao corta isso), tabela travada e conexao ocupada.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import structlog
from sqlalchemy import event, text
from structlog.testing import capture_logs

from src.compartilhado.infraestrutura.database import criar_engine_de_metricas
from src.ordem_servico.infraestrutura import coletor_da_api
from src.ordem_servico.infraestrutura.coletor_da_api import ColetorDaApi
from src.ordem_servico.infraestrutura.metricas_da_saga import FALHAS_DO_COLETOR
from tests.integracao.proxy_tcp import ProxyTcp
from tests.integracao.seed_helpers import criar_ordens_com_historicos
from tests.relogio import RelogioFixo

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, Engine
    from sqlalchemy.orm import Session, sessionmaker

# O relogio das ordens do coletor: as fixtures de historico e o coletor leem este.
_AGORA = datetime(2026, 10, 14, 12, tzinfo=UTC)
_SO_A_DISPONIBILIDADE = {"pytstop_saga_coletor_disponivel": [0]}
_DA_SAGA = {
    "pytstop_saga_ativas",
    "pytstop_saga_etapa_mais_antiga_segundos",
    "pytstop_saga_prazo_vencido_segundos",
}
_DAS_ORDENS = {"pytstop_os_por_status", "pytstop_os_tempo_medio_status_segundos"}
_DA_OUTBOX = {"pytstop_outbox_mais_antiga_segundos"}


@pytest.fixture(autouse=True)
def _log_fresco(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coletor_da_api, "_log", structlog.get_logger("test_coletor"))


@pytest.fixture(autouse=True)
def _banco_limpo(session_factory: sessionmaker[Session]) -> None:
    """Os testes escrevem direto no engine: o teardown da factory trunca as tabelas."""


def _raspar(coletor: ColetorDaApi) -> dict[str, list[float]]:
    return {f.name: [a.value for a in f.samples] for f in coletor.collect()}


def _falhas() -> float:
    return FALHAS_DO_COLETOR._value.get()  # contador do cliente


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture
def metricas(engine: Engine) -> Iterator[Engine]:
    """A engine do coletor, como o lifespan a cria: uma conexao, prazos curtos."""
    eng = criar_engine_de_metricas(_url(engine))
    yield eng
    eng.dispose()


@pytest.fixture
def proxy(engine: Engine) -> Iterator[ProxyTcp]:
    assert engine.url.host is not None
    assert engine.url.port is not None
    with ProxyTcp((engine.url.host, engine.url.port)) as proxy:
        yield proxy


def _metricas_via(proxy: ProxyTcp, engine: Engine) -> Engine:
    url = engine.url.set(host="127.0.0.1", port=proxy.porta)
    return criar_engine_de_metricas(url.render_as_string(hide_password=False))


class TestLeitura:
    def test_publica_os_gauges_da_saga_da_outbox_e_das_ordens(
        self,
        engine: Engine,
        metricas: Engine,
        session_factory: sessionmaker[Session],
    ) -> None:
        criar_ordens_com_historicos(
            session_factory, [("recebida", _AGORA), ("em_diagnostico", _AGORA)]
        )

        raspagem = _raspar(ColetorDaApi(metricas.connect, agora=lambda: _AGORA))

        assert set(raspagem) == _DA_SAGA | _DAS_ORDENS | _DA_OUTBOX | {
            "pytstop_saga_coletor_disponivel"
        }
        assert raspagem["pytstop_saga_coletor_disponivel"] == [1]

    def test_idade_da_outbox_e_a_da_linha_pendente_mais_antiga(
        self, engine: Engine, metricas: Engine
    ) -> None:
        # O banco grava a idade (`now() - intervalo`), e o relogio do teste nao entra.
        with engine.begin() as conexao:
            for status, segundos in (
                ("pendente", 120),
                ("pendente", 10),
                ("entregue", 5000),
                ("dead", 9000),
            ):
                conexao.execute(
                    text(
                        "INSERT INTO outbox (mensagem_id, correlation_id, exchange, "
                        "routing_key, envelope, status, criado_em) VALUES "
                        "(gen_random_uuid(), gen_random_uuid(), 'x', 'y', '{}', "
                        ":status, now() - make_interval(secs => :segundos))"
                    ),
                    {"status": status, "segundos": segundos},
                )

        raspagem = _raspar(ColetorDaApi(metricas.connect))

        (idade,) = raspagem["pytstop_outbox_mais_antiga_segundos"]
        # So a pendente mais antiga conta (120 s, e nao a de 10 s); a entregue
        # (5.000 s) e a morta (9.000 s), mais velhas, nao entram.
        assert 120 <= idade < 5000

    def test_sem_pendente_a_idade_vale_zero_e_a_serie_existe(
        self, metricas: Engine
    ) -> None:
        raspagem = _raspar(ColetorDaApi(metricas.connect))

        assert raspagem["pytstop_outbox_mais_antiga_segundos"] == [0.0]

    def test_uma_conexao_so_por_raspagem(self, engine: Engine) -> None:
        # O prazo de abrir a conexao conta uma vez, e nao uma por gauge.
        aberturas: list[int] = []

        def abrir() -> Connection:
            aberturas.append(1)
            return engine.connect()

        _raspar(ColetorDaApi(abrir))

        assert aberturas == [1]


class TestCache:
    @pytest.fixture
    def leituras_do_historico(self, engine: Engine) -> Iterator[list[str]]:
        lidas: list[str] = []

        def contar(_c: object, _k: object, comando: str, *_: object) -> None:
            # So a leitura da permanencia (o `entradas` da subconsulta), e nao as
            # gravacoes que o teste faz no historico.
            if "entradas.status" in comando:
                lidas.append(comando)

        event.listen(engine, "before_cursor_execute", contar)
        yield lidas
        event.remove(engine, "before_cursor_execute", contar)

    def test_as_ordens_ficam_30_s_em_cache_e_depois_sao_lidas_de_novo(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        leituras_do_historico: list[str],
    ) -> None:
        relogio = RelogioFixo()
        coletor = ColetorDaApi(engine.connect, relogio=relogio, agora=lambda: _AGORA)
        por_status = "pytstop_os_por_status"

        primeira = _raspar(coletor)
        # Muda o banco: a leitura em cache nao o ve.
        criar_ordens_com_historicos(session_factory, [("recebida", _AGORA)])
        relogio.avancar(29)
        dentro_da_validade = _raspar(coletor)
        relogio.avancar(2)
        depois = _raspar(coletor)

        # Duas raspagens de replicas diferentes, 15 s uma da outra, leem uma vez.
        assert len(leituras_do_historico) == 2
        assert dentro_da_validade[por_status] == primeira[por_status]
        assert sum(primeira[por_status]) == 0
        assert sum(depois[por_status]) == 1

    def test_gauges_da_saga_e_da_outbox_nao_ficam_em_cache(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
    ) -> None:
        relogio = RelogioFixo()
        coletor = ColetorDaApi(engine.connect, relogio=relogio)
        antes = _raspar(coletor)["pytstop_outbox_mais_antiga_segundos"]
        with engine.begin() as conexao:
            conexao.execute(
                text(
                    "INSERT INTO outbox (mensagem_id, correlation_id, exchange, "
                    "routing_key, envelope, status, criado_em) VALUES "
                    "(gen_random_uuid(), gen_random_uuid(), 'x', 'y', '{}', "
                    "'pendente', now() - interval '60 seconds')"
                )
            )

        depois = _raspar(coletor)["pytstop_outbox_mais_antiga_segundos"]

        # Sem avancar o relogio: o alerta "Outbox parada" nao espera 30 s.
        assert antes == [0.0]
        assert depois[0] >= 60

    def test_leitura_que_falhou_nao_vai_para_o_cache(
        self, engine: Engine, metricas: Engine
    ) -> None:
        relogio = RelogioFixo()
        coletor = ColetorDaApi(metricas.connect, relogio=relogio)
        with engine.connect() as trava, trava.begin():
            trava.execute(
                text("LOCK TABLE historico_status_ordem IN ACCESS EXCLUSIVE MODE")
            )
            travada = _raspar(coletor)

        # Destravado, a raspagem seguinte (no mesmo instante do relogio) le de novo:
        # a falha nao ficou guardada como se fosse a leitura.
        destravada = _raspar(coletor)

        assert not _DAS_ORDENS & set(travada)
        assert set(destravada) >= _DAS_ORDENS


class TestConexaoDaEngine:
    def test_conexao_derrubada_pelo_servidor_e_reaberta_na_raspagem_seguinte(
        self, engine: Engine, metricas: Engine
    ) -> None:
        # Reinicio do banco ou failover: a conexao do pool morreu. O pre-ping da
        # engine das metricas a descarta e abre outra, e a raspagem nao perde os
        # gauges (sem ele, a primeira raspagem depois da queda sairia vazia).
        coletor = ColetorDaApi(metricas.connect)
        assert "pytstop_saga_ativas" in _raspar(coletor)
        with engine.connect() as admin:
            admin.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE pid <> pg_backend_pid() AND datname = current_database() "
                    "AND state = 'idle'"
                )
            )
            admin.commit()

        raspagem = _raspar(coletor)

        assert raspagem["pytstop_saga_coletor_disponivel"] == [1]


class TestBancoComProblema:
    def test_antes_do_boot_marca_o_coletor_indisponivel(self) -> None:
        def sem_engine() -> Connection:
            msg = "Engine de metricas nao configurada"
            raise RuntimeError(msg)

        falhas = _falhas()

        raspagem = _raspar(ColetorDaApi(sem_engine))

        assert raspagem == _SO_A_DISPONIBILIDADE
        assert _falhas() == falhas + 1

    def test_banco_fora_do_ar_deixa_tudo_ausente_conta_a_falha_e_loga(self) -> None:
        # Porta sem servidor: a conexao e recusada na hora.
        fora = criar_engine_de_metricas("postgresql://os:os@127.0.0.1:1/os")
        falhas = _falhas()

        with capture_logs() as logs:
            raspagem = _raspar(ColetorDaApi(fora.connect))

        assert raspagem == _SO_A_DISPONIBILIDADE
        assert _falhas() == falhas + 1
        assert {
            "event": "api gauges unavailable",
            "log_level": "warning",
            "motivo": "OperationalError",
        } in logs
        fora.dispose()

    def test_tabela_da_saga_travada_omite_so_os_gauges_da_saga(
        self, engine: Engine, metricas: Engine
    ) -> None:
        coletor = ColetorDaApi(metricas.connect)
        falhas = _falhas()

        with engine.connect() as trava, trava.begin():
            trava.execute(text("LOCK TABLE sagas IN ACCESS EXCLUSIVE MODE"))
            inicio = time.monotonic()
            raspagem = _raspar(coletor)
            decorrido = time.monotonic() - inicio

        # O lock_timeout (1 s) corta a espera, bem antes do prazo da raspagem. Os
        # gauges que nao dependem da tabela travada saem; os da saga, ausentes
        # (nunca zero, que esconderia uma saga parada).
        assert decorrido < 5
        assert raspagem["pytstop_saga_coletor_disponivel"] == [0]
        assert not _DA_SAGA & set(raspagem)
        assert set(raspagem) >= (_DA_OUTBOX | _DAS_ORDENS)
        assert _falhas() == falhas + 1

    def test_tabela_das_ordens_travada_omite_so_os_gauges_das_ordens(
        self, engine: Engine, metricas: Engine
    ) -> None:
        with engine.connect() as trava, trava.begin():
            trava.execute(text("LOCK TABLE ordens_de_servico IN ACCESS EXCLUSIVE MODE"))
            raspagem = _raspar(ColetorDaApi(metricas.connect))

        assert raspagem["pytstop_saga_coletor_disponivel"] == [1]
        assert not _DAS_ORDENS & set(raspagem)
        assert set(raspagem) >= (_DA_SAGA | _DA_OUTBOX)

    def test_tabela_da_outbox_travada_derruba_a_saga_e_a_outbox(
        self, engine: Engine, metricas: Engine
    ) -> None:
        # O prazo vencido da saga junta `sagas` e `outbox`.
        with engine.connect() as trava, trava.begin():
            trava.execute(text("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE"))
            raspagem = _raspar(ColetorDaApi(metricas.connect))

        assert raspagem["pytstop_saga_coletor_disponivel"] == [0]
        assert not (_DA_SAGA | _DA_OUTBOX) & set(raspagem)
        assert set(raspagem) >= _DAS_ORDENS

    def test_conexao_ocupada_nao_faz_a_raspagem_esperar_alem_do_prazo(
        self, metricas: Engine
    ) -> None:
        with metricas.connect():
            # A unica conexao da engine das metricas esta em uso: o coletor espera
            # o pool_timeout (1 s) e desiste.
            inicio = time.monotonic()
            raspagem = _raspar(ColetorDaApi(metricas.connect))
            decorrido = time.monotonic() - inicio

        assert raspagem == _SO_A_DISPONIBILIDADE
        # Bem abaixo do prazo da raspagem: com o pool_timeout de 1 s a vez nao
        # espera mais que isso (sem ele o pool esperaria o prazo inteiro).
        assert decorrido < 3

    def test_consulta_que_executa_devagar_e_cortada_pelo_statement_timeout(
        self, metricas: Engine
    ) -> None:
        # A consulta da outbox vira uma que dorme 8 s no servidor (sem lock, so
        # execucao lenta). O statement_timeout (2 s) a corta, e as leituras que
        # vem depois dela ainda cabem no prazo da raspagem (5 s). Sem ele, a
        # raspagem esgotaria o prazo esperando a consulta, e as OS ficariam ausentes.
        def devagar(
            _conexao: object,
            _cursor: object,
            comando: str,
            parametros: object,
            *_: object,
        ) -> tuple[str, object]:
            if "FROM outbox WHERE status" in comando:
                return "SELECT 1.0 FROM (SELECT pg_sleep(8)) AS pausa", ()
            return comando, parametros

        event.listen(metricas, "before_cursor_execute", devagar, retval=True)
        coletor = ColetorDaApi(metricas.connect)

        inicio = time.monotonic()
        raspagem = _raspar(coletor)
        decorrido = time.monotonic() - inicio

        assert 1.9 <= decorrido < 4.5
        assert raspagem["pytstop_saga_coletor_disponivel"] == [1]
        assert not _DA_OUTBOX & set(raspagem)
        assert set(raspagem) >= (_DA_SAGA | _DAS_ORDENS)

    def test_banco_mudo_na_abertura_responde_em_segundos(
        self, engine: Engine, proxy: ProxyTcp
    ) -> None:
        # Aceita o TCP e nao responde o protocolo (no de verdade, no travado ou
        # particao de rede). A conexao de 1 s corta, e conta uma vez so.
        proxy.calar()
        mudo = _metricas_via(proxy, engine)
        falhas = _falhas()

        inicio = time.monotonic()
        raspagem = _raspar(ColetorDaApi(mudo.connect))
        decorrido = time.monotonic() - inicio

        assert raspagem == _SO_A_DISPONIBILIDADE
        assert decorrido < 4
        assert _falhas() == falhas + 1
        mudo.dispose()

    def test_banco_que_emudece_com_a_conexao_aberta_nao_pendura_a_raspagem(
        self, engine: Engine, proxy: ProxyTcp
    ) -> None:
        # O pior caso: a conexao do pool ja esta aberta e o banco cala sem fechar o
        # socket. O statement_timeout nao ajuda (o servidor nem ve o comando); so o
        # prazo da propria raspagem devolve a resposta.
        via_proxy = _metricas_via(proxy, engine)
        coletor = ColetorDaApi(via_proxy.connect, orcamento_s=1.5)
        assert "pytstop_saga_ativas" in _raspar(coletor)  # aquece a conexao do pool
        proxy.calar()

        inicio = time.monotonic()
        mudo = _raspar(coletor)
        decorrido = time.monotonic() - inicio
        inicio = time.monotonic()
        seguinte = _raspar(coletor)
        segunda = time.monotonic() - inicio

        assert mudo == _SO_A_DISPONIBILIDADE
        assert 1.4 <= decorrido < 4
        # Com a leitura anterior ainda presa a raspagem seguinte nem tenta.
        assert seguinte == _SO_A_DISPONIBILIDADE
        assert segunda < 0.5
        proxy.fechar()
        via_proxy.dispose()

    def test_banco_que_emudece_com_a_conexao_aberta_responde_no_orcamento_padrao(
        self, engine: Engine, proxy: ProxyTcp
    ) -> None:
        # Os outros testes de prazo injetam um orcamento curto. Este deixa o PADRAO do
        # coletor, o que roda em producao: o /metrics responde bem antes dos 10 s do
        # scrape_timeout do Prometheus, mesmo com o banco mudo depois do aquecimento.
        via_proxy = _metricas_via(proxy, engine)
        coletor = ColetorDaApi(via_proxy.connect)
        assert "pytstop_saga_ativas" in _raspar(coletor)  # aquece a conexao do pool
        proxy.calar()

        inicio = time.monotonic()
        mudo = _raspar(coletor)
        decorrido = time.monotonic() - inicio

        assert mudo == _SO_A_DISPONIBILIDADE
        assert 4.5 <= decorrido < 7
        proxy.fechar()
        via_proxy.dispose()

    def test_banco_lento_em_todas_as_consultas_responde_antes_do_prazo(
        self, engine: Engine, metricas: Engine
    ) -> None:
        # O banco responde, mas cada consulta esbarra num lock e espera o
        # lock_timeout (1 s): tres leituras seguidas na mesma conexao, cerca de 3 s,
        # menos que o prazo de 5 s e bem menos que os 10 s do Prometheus.
        with engine.connect() as trava, trava.begin():
            for tabela in ("sagas", "outbox", "ordens_de_servico"):
                trava.execute(text(f"LOCK TABLE {tabela} IN ACCESS EXCLUSIVE MODE"))
            inicio = time.monotonic()
            raspagem = _raspar(ColetorDaApi(metricas.connect))
            decorrido = time.monotonic() - inicio

        assert raspagem == _SO_A_DISPONIBILIDADE
        assert 2.5 <= decorrido < 5
