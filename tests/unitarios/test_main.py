from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import main
from src.compartilhado.interfaces.dependencies import obter_session
from src.main import criar_app, lifespan


class TestMain:
    def test_criar_app_retorna_fastapi(self) -> None:
        application = criar_app()
        assert isinstance(application, FastAPI)

    def test_fastapi_nao_monta_o_export_otlp_automatico(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """O FastAPI 0.142+ soma exportadores OTLP na partida quando o endpoint
        esta no ambiente e avisa que a configuracao falhou (falta o extra
        ``fastapi[opentelemetry]``). A API exporta por ``configurar_otel``, e o
        argumento ``telemetry`` desliga a configuracao automatica mesmo com
        ``FASTAPI_OTEL_AUTO_CONFIGURE=true`` no ambiente (o argumento ganha)."""
        monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
        # O FastAPI 0.143 guarda a configuracao efetiva em ``_telemetry``.
        assert criar_app()._telemetry["auto_configure"] is False

    def test_titulo(self) -> None:
        application = criar_app()
        assert application.title == "PytStop OS Service"

    def test_routers_montados(self) -> None:
        application = criar_app()
        # Contrato publico (OpenAPI) em vez de introspecao de `application.routes`:
        # o FastAPI 0.138 passou a guardar routers incluidos como `_IncludedRouter`
        # (nao mais `Route` achatado com `.path`), entao varrer `.routes` deixou de
        # enxergar os paths montados. `openapi()["paths"]` e a API estavel.
        paths = set(application.openapi()["paths"])
        assert "/api/v1/saude" in paths
        assert any("/api/v1/clientes" in p for p in paths)
        assert any("/api/v1/autenticacao" in p for p in paths)
        # Superficie de OS da fase 4 (RFC-004 secao 6.1).
        assert {
            "/.well-known/jwks.json",
            "/api/v1/ordens-de-servico",
            "/api/v1/ordens-de-servico/{ordem_id}",
            "/api/v1/ordens-de-servico/{ordem_id}/historico",
            "/api/v1/ordens-de-servico/{ordem_id}/cancelamento",
            "/api/v1/ordens-de-servico/{ordem_id}/entrega",
            "/api/v1/sagas/{ordem_id}",
            "/api/v1/publico/acompanhamento",
        } <= paths

    def test_rotas_que_sairam_do_servico_nao_existem(self) -> None:
        paths = set(criar_app().openapi()["paths"])
        removidos = ("/servicos", "/estoque", "/minhas-ordens", "/admin/outbox")
        assert not [p for p in paths if any(r in p for r in removidos)]
        assert not [p for p in paths if p.endswith(("/itens", "/orcamento"))]

    def test_rotas_de_colecao_sem_barra_e_sem_redirect(self) -> None:
        paths = set(criar_app().openapi()["paths"])
        assert not [p for p in paths if p.endswith("/")]
        application = criar_app()
        application.dependency_overrides[obter_session] = lambda: MagicMock()
        client = TestClient(application, follow_redirects=False)
        # Sem barra a rota existe (pede token); com barra nao ha 307, so 404.
        assert client.get("/api/v1/clientes").status_code == 401
        assert client.get("/api/v1/clientes/").status_code == 404
        assert client.get("/api/v1/ordens-de-servico/").status_code == 404

    def test_versao(self) -> None:
        from importlib.metadata import version

        application = criar_app()
        assert application.version == version("pytstop-os-service")

    def test_middleware_registrado(self) -> None:
        application = criar_app()
        middleware_classes = [
            getattr(m.cls, "__name__", "") for m in application.user_middleware
        ]
        assert "SecurityHeadersMiddleware" in middleware_classes

    def test_metricas_http_sempre_ligadas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sem flag: nem API_METRICS_ENABLED=false (a variavel de antes) as desliga.
        monkeypatch.setenv("API_METRICS_ENABLED", "false")
        application = criar_app()
        middleware_classes = [
            getattr(m.cls, "__name__", "") for m in application.user_middleware
        ]

        assert "MetricasHTTPMiddleware" in middleware_classes
        assert TestClient(application).get("/metrics").status_code == 200

    def test_metricas_http_ficam_por_fora_de_todos_os_middlewares(self) -> None:
        # O ultimo middleware adicionado e o mais externo: mede a request inteira,
        # com o roteamento, a seguranca e o rate limiting dentro.
        application = criar_app()

        assert application.user_middleware[0].cls.__name__ == "MetricasHTTPMiddleware"

    def test_criar_app_registra_o_listener_das_ordens_criadas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chamadas: list[str] = []
        monkeypatch.setattr(
            "src.ordem_servico.infraestrutura.metrics.instrumentar_metricas_de_ordens",
            lambda: chamadas.append("listener"),
        )

        criar_app()

        assert chamadas == ["listener"]

    def test_o_coletor_dos_gauges_so_nasce_com_o_lifespan(self) -> None:
        # Fora do lifespan nao ha engine de metricas: com o coletor no registro,
        # toda leitura do REGISTRY tentaria o banco. Ver
        # test_lifespan_registra_o_coletor_e_o_tira_no_fim.
        from prometheus_client import REGISTRY

        criar_app()

        assert REGISTRY.get_sample_value("pytstop_saga_coletor_disponivel") is None

    def test_docs_url_em_development(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=False):
            application = criar_app()
            assert application.docs_url == "/docs"
            assert application.redoc_url == "/redoc"

    def test_docs_ligados_em_production(self) -> None:
        # Swagger e entregavel e fica na borda em todo ambiente.
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=False):
            application = criar_app()
            assert application.docs_url == "/docs"
            assert application.redoc_url == "/redoc"
            assert application.openapi_url == "/openapi.json"

    def test_executar_servidor_dev_usa_localhost_por_padrao(self) -> None:
        with (
            patch("src.main.uvicorn.run") as mock_run,
            patch.dict(os.environ, {}, clear=True),
        ):
            main.executar_servidor_dev()

        mock_run.assert_called_once_with(
            "src.main:app",
            host="127.0.0.1",
            port=8000,
            reload=True,
        )

    def test_executar_servidor_dev_respeita_host_e_porta_env(self) -> None:
        with (
            patch("src.main.uvicorn.run") as mock_run,
            patch.dict(
                os.environ,
                {"UVICORN_HOST": "127.0.0.2", "UVICORN_PORT": "9000"},
                clear=True,
            ),
        ):
            main.executar_servidor_dev()

        mock_run.assert_called_once_with(
            "src.main:app",
            host="127.0.0.2",
            port=9000,
            reload=True,
        )

    def test_openapi_schema_gera_sem_erro(self) -> None:
        """Garante que /openapi.json e gerado com sucesso.

        Pega regressoes de from __future__ import annotations vs Pydantic
        (ForwardRef nao resolvido) e qualquer schema Pydantic malformado
        antes de chegar a producao.
        """
        app = criar_app()
        client = TestClient(app)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert schema["openapi"].startswith("3.")
        assert len(schema["paths"]) > 0

    def test_criar_app_configura_o_log_json_antes_do_servidor_subir(self) -> None:
        # O uvicorn importa o app antes de "Started server process": configurar o
        # log so no lifespan deixava o boot em texto puro e sem o scrub.
        with patch("src.main.configurar_logging") as mock_logging:
            criar_app()

        mock_logging.assert_called_once_with()

    def test_lifespan_executa_mapeamentos(self) -> None:
        app = FastAPI()

        async def _run() -> None:
            with (
                patch(
                    "src.compartilhado.infraestrutura.bootstrap.iniciar_todos_mapeamentos"
                ) as mock_mapeamentos,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine"
                ) as mock_engine,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ) as mock_factory,
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ) as mock_configurar,
                patch(
                    "src.compartilhado.infraestrutura.mensageria.contratos.catalogo"
                ) as mock_catalogo,
                # Fixa ambiente de teste para que o ramo de fail-fast de
                # DATABASE_URL em producao nao interfira (o ramo e coberto
                # em test_lifespan_sem_database_url_em_producao_falha).
                patch.dict(
                    os.environ,
                    {
                        "ENVIRONMENT": "development",
                        "POSTGRES_DB": "pytstop",
                        "POSTGRES_USER": "app_user",
                        "POSTGRES_PASSWORD": "senha-dev-local",
                    },
                    clear=False,
                ),
            ):
                mock_engine.return_value.dispose = lambda: None
                async with lifespan(app):
                    pass
                mock_mapeamentos.assert_called_once()
                # Os contratos sao lidos no boot (fail-fast sem contratos/).
                mock_catalogo.assert_called_once_with()
                mock_engine.assert_called_once()
                mock_factory.assert_called_once()
                mock_configurar.assert_called_once()

        asyncio.run(_run())

    def test_lifespan_chama_configurar_otel_com_app_e_engine(self) -> None:
        """O boot liga a observabilidade no ponto unico em que app e
        engine existem juntos. Com OTEL_ENABLED ausente a funcao e no-op,
        entao a chamada incondicional nao afeta os demais testes de lifespan."""
        app = FastAPI()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine"
                ) as mock_engine,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch(
                    "src.compartilhado.infraestrutura.observability.configurar_otel"
                ) as mock_otel,
                patch.dict(
                    os.environ,
                    {"DATABASE_URL": "postgresql://x:x@localhost:5432/x"},
                    clear=False,
                ),
            ):
                mock_engine.return_value.dispose = lambda: None
                mock_otel.return_value = None
                async with lifespan(app):
                    pass
                # Sem provider de fora (`criar_app()` sem `provedor`), o lifespan
                # deixa o `configurar_otel` montar o dele.
                mock_otel.assert_called_once_with(app, mock_engine.return_value, None)

        asyncio.run(_run())

    @pytest.mark.parametrize("de_fora", [False, True], ids=["montado", "de-fora"])
    def test_lifespan_encerra_so_o_provider_que_o_configurar_otel_montou(
        self, de_fora: bool
    ) -> None:
        """O ``shutdown`` descarrega o lote de spans no fim do lifespan: o
        ``atexit`` do SDK nao roda quando o uvicorn nao e o PID 1. O provider que
        veio de fora (os testes) e de quem o criou, e o lifespan o entrega ao
        ``configurar_otel`` sem encerra-lo."""
        provedor_de_fora = MagicMock()
        app = criar_app(provedor_de_fora if de_fora else None)
        montado = MagicMock()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.compartilhado.infraestrutura.database.criar_engine"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch(
                    "src.compartilhado.infraestrutura.observability.configurar_otel",
                    return_value=None if de_fora else montado,
                ) as mock_otel,
                patch.dict(
                    os.environ,
                    {"DATABASE_URL": "postgresql://x:x@localhost:5432/x"},
                    clear=False,
                ),
            ):
                async with lifespan(app):
                    montado.shutdown.assert_not_called()
                assert mock_otel.call_args.args[2] is (
                    provedor_de_fora if de_fora else None
                )

        asyncio.run(_run())

        provedor_de_fora.shutdown.assert_not_called()
        assert montado.shutdown.call_count == (0 if de_fora else 1)

    def test_lifespan_liga_a_engine_das_metricas_e_a_descarta_no_fim(self) -> None:
        """Os gauges da saga raspam por uma engine propria (uma conexao, prazos
        curtos), criada com a URL do banco, configurada no boot e descartada no
        shutdown; a das requisicoes e outra."""
        app = FastAPI()
        url = "postgresql://x:x@localhost:5432/x"

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine"
                ) as mock_engine,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine_de_metricas"
                ) as mock_metricas,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_engine_de_metricas"
                ) as mock_configurar,
                patch.dict(os.environ, {"DATABASE_URL": url}, clear=False),
            ):
                async with lifespan(app):
                    mock_metricas.assert_called_once_with(url)
                    mock_configurar.assert_called_once_with(mock_metricas.return_value)
                    # No ar, as duas engines seguem abertas.
                    mock_engine.return_value.dispose.assert_not_called()
                    mock_metricas.return_value.dispose.assert_not_called()
                mock_engine.return_value.dispose.assert_called_once_with()
                mock_metricas.return_value.dispose.assert_called_once_with()

        asyncio.run(_run())

    @pytest.mark.parametrize("ja_havia", [False, True], ids=["primeiro", "segundo"])
    def test_lifespan_registra_o_coletor_e_o_tira_no_fim(self, ja_havia: bool) -> None:
        """O coletor dos gauges por consulta existe so com o app de pe, e so o app
        que o registrou o tira; o segundo app do processo (os testes) nao o tem."""
        app = FastAPI()
        coletor = MagicMock()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.compartilhado.infraestrutura.database.criar_engine"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch(
                    "src.ordem_servico.infraestrutura.coletor_da_api.registrar_coletor",
                    return_value=None if ja_havia else coletor,
                ) as mock_registrar,
                patch(
                    "src.ordem_servico.infraestrutura.coletor_da_api.desregistrar_coletor"
                ) as mock_desregistrar,
                patch.dict(
                    os.environ,
                    {"DATABASE_URL": "postgresql://x:x@localhost:5432/x"},
                    clear=False,
                ),
            ):
                async with lifespan(app):
                    # O coletor abre a conexao pela engine das metricas, configurada
                    # antes dele.
                    (abrir,) = mock_registrar.call_args.args
                    assert abrir.__name__ == "abrir_conexao_de_metricas"
                    mock_desregistrar.assert_not_called()
                if ja_havia:
                    mock_desregistrar.assert_not_called()
                else:
                    mock_desregistrar.assert_called_once_with(coletor)

        asyncio.run(_run())

    def test_lifespan_desfaz_o_coletor_e_as_engines_se_a_observabilidade_falha(
        self,
    ) -> None:
        """Falha ao ligar a observabilidade (exportador, instrumentacao) nao deixa o
        coletor registrado no processo nem as engines abertas."""
        app = FastAPI()
        coletor = MagicMock()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine"
                ) as mock_engine,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine_de_metricas"
                ) as mock_metricas,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_engine_de_metricas"
                ),
                patch(
                    "src.ordem_servico.infraestrutura.coletor_da_api.registrar_coletor",
                    return_value=coletor,
                ),
                patch(
                    "src.ordem_servico.infraestrutura.coletor_da_api.desregistrar_coletor"
                ) as mock_desregistrar,
                patch(
                    "src.compartilhado.infraestrutura.observability.configurar_otel",
                    side_effect=RuntimeError("exportador"),
                ),
                patch.dict(
                    os.environ,
                    {"DATABASE_URL": "postgresql://x:x@localhost:5432/x"},
                    clear=False,
                ),
            ):
                with pytest.raises(RuntimeError, match="exportador"):
                    async with lifespan(app):
                        pytest.fail("o app nao pode subir")
                mock_desregistrar.assert_called_once_with(coletor)
                mock_engine.return_value.dispose.assert_called_once_with()
                mock_metricas.return_value.dispose.assert_called_once_with()

        asyncio.run(_run())

    def test_lifespan_configura_session_factory_com_database_url(self) -> None:
        app = FastAPI()
        url_customizada = "postgresql://custom:custom@remote:5433/db"

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine"
                ) as mock_engine,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch.dict(os.environ, {"DATABASE_URL": url_customizada}, clear=False),
            ):
                mock_engine.return_value.dispose = lambda: None
                async with lifespan(app):
                    pass
                mock_engine.assert_called_once_with(url_customizada)

        asyncio.run(_run())

    def test_lifespan_sem_database_url_em_development_usa_variaveis_postgres(
        self,
    ) -> None:
        app = FastAPI()
        env_sem_db = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        env_sem_db["ENVIRONMENT"] = "development"
        env_sem_db["POSTGRES_DB"] = "pytstop"
        env_sem_db["POSTGRES_USER"] = "app_user"
        env_sem_db["POSTGRES_PASSWORD"] = "senha-dev-local"
        env_sem_db["POSTGRES_HOST"] = "localhost"
        env_sem_db["POSTGRES_PORT"] = "5432"
        database_url_esperada = (
            "postgresql://app_user:senha-dev-local@localhost:5432/pytstop"
        )

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine"
                ) as mock_engine,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch.dict(os.environ, env_sem_db, clear=True),
            ):
                mock_engine.return_value.dispose = lambda: None
                async with lifespan(app):
                    pass
                mock_engine.assert_called_once_with(database_url_esperada)

        asyncio.run(_run())

    def test_lifespan_sem_database_url_e_sem_password_em_development_falha(
        self,
    ) -> None:
        app = FastAPI()
        env_sem_db = {
            k: v
            for k, v in os.environ.items()
            if k not in {"DATABASE_URL", "POSTGRES_PASSWORD"}
        }
        env_sem_db["ENVIRONMENT"] = "development"

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.compartilhado.infraestrutura.database.criar_engine"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch.dict(os.environ, env_sem_db, clear=True),
            ):
                async with lifespan(app):
                    pass

        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            asyncio.run(_run())

    def test_lifespan_sem_database_url_em_producao_falha(self) -> None:
        app = FastAPI()
        env_sem_db = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        env_sem_db["ENVIRONMENT"] = "production"

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.compartilhado.infraestrutura.database.criar_engine"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch.dict(os.environ, env_sem_db, clear=True),
            ):
                async with lifespan(app):
                    pass

        with pytest.raises(RuntimeError, match="DATABASE_URL obrigatoria"):
            asyncio.run(_run())

    def test_lifespan_chama_validar_segredos(self) -> None:
        """O lifespan exerce a guarda de segredos no startup.

        Em dev a guarda e no-op, mas precisa ser CHAMADA -- senao em producao a
        validacao nunca roda. Verifica a chamada por patch da funcao no modulo
        onde o lifespan a referencia.
        """
        app = FastAPI()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_engine"
                ) as mock_engine,
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch("src.main.validar_segredos_no_startup") as mock_validar,
                patch("src.main.validar_chave_jwt_no_startup") as mock_validar_jwt,
                patch.dict(
                    os.environ,
                    {"DATABASE_URL": "postgresql://x:x@localhost:5432/x"},
                    clear=False,
                ),
            ):
                mock_engine.return_value.dispose = lambda: None
                async with lifespan(app):
                    pass
                mock_validar.assert_called_once()
                mock_validar_jwt.assert_called_once()

        asyncio.run(_run())

    def test_lifespan_guarda_o_prazo_de_resposta_lido_uma_vez(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rota do cancelamento usa o valor lido no boot, nao o ambiente."""
        from datetime import timedelta

        from src.ordem_servico.interfaces import dependencies

        monkeypatch.setattr(dependencies, "_prazo_de_resposta", None)
        app = FastAPI()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.compartilhado.infraestrutura.database.criar_engine"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch("src.main.validar_segredos_no_startup"),
                patch("src.main.validar_chave_jwt_no_startup"),
                patch.dict(
                    os.environ,
                    {
                        "DATABASE_URL": "postgresql://x:x@localhost:5432/x",
                        "SAGA_PRAZO_RESPOSTA_SEGUNDOS": "45",
                    },
                    clear=False,
                ),
            ):
                async with lifespan(app):
                    pass

        asyncio.run(_run())

        assert dependencies._prazo_de_resposta == timedelta(seconds=45)

    def test_lifespan_aborta_com_prazo_de_resposta_invalido(self) -> None:
        """O cancelamento grava o prazo da primeira compensacao: o valor invalido
        de ``SAGA_PRAZO_RESPOSTA_SEGUNDOS`` aborta o boot, como no consumidor."""
        app = FastAPI()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.compartilhado.infraestrutura.database.criar_engine"),
                patch("src.main.validar_segredos_no_startup"),
                patch("src.main.validar_chave_jwt_no_startup"),
                patch.dict(
                    os.environ,
                    {
                        "DATABASE_URL": "postgresql://x:x@localhost:5432/x",
                        "SAGA_PRAZO_RESPOSTA_SEGUNDOS": "0",
                    },
                    clear=False,
                ),
            ):
                async with lifespan(app):
                    pass

        with pytest.raises(RuntimeError, match="SAGA_PRAZO_RESPOSTA_SEGUNDOS"):
            asyncio.run(_run())

    def test_lifespan_aborta_com_a_chave_rsa_de_demo_em_producao(self) -> None:
        """Boot em producao falha com a chave RSA de demonstracao do compose.

        Prova a integracao ponta-a-ponta: o lifespan (sem mockar as guardas)
        aborta antes de aceitar requisicoes. DATABASE_URL e ENCRYPTION_KEY
        validas isolam a falha na guarda da chave do JWT.
        """
        from cryptography.fernet import Fernet

        from tests.chaves_jwt import pem_demo_do_compose

        app = FastAPI()
        env = dict(os.environ)
        env["ENVIRONMENT"] = "production"
        env["DATABASE_URL"] = "postgresql://x:x@localhost:5432/x"
        env["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        env["JWT_PRIVATE_KEY"] = pem_demo_do_compose()

        async def _run() -> None:
            with (
                patch("src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"),
                patch("src.compartilhado.infraestrutura.database.criar_engine"),
                patch(
                    "src.compartilhado.infraestrutura.database.criar_session_factory"
                ),
                patch(
                    "src.compartilhado.interfaces.dependencies.configurar_session_factory"
                ),
                patch.dict(os.environ, env, clear=True),
            ):
                async with lifespan(app):
                    pass

        with pytest.raises(RuntimeError, match="chave RSA de demonstracao"):
            asyncio.run(_run())
