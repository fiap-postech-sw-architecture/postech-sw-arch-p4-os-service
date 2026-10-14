"""``configurar_otel``: o que a API liga no lifespan (ADR-020 do p3, ADR-043).

O provider dos testes e o de producao (``Rastreador``); o engine e um SQLite em
memoria, que o instrumentador do SQLAlchemy aceita como qualquer outro.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import baggage, trace
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.trace import SpanKind
from sqlalchemy import create_engine, text

import src.compartilhado.infraestrutura.observability as observability_modulo
from src.compartilhado.infraestrutura.mensageria.telemetria import PropagadorW3C
from src.compartilhado.infraestrutura.observability import (
    configurar_otel,
    instrumentar_banco,
    tracer_da_api,
)
from tests.rastreamento import Rastreador

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

_PAI = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def _estado_global_da_telemetria(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """O que ``configurar_otel`` muda no processo, desfeito ao fim de cada teste."""
    monkeypatch.setattr(
        observability_modulo, "_log", structlog.get_logger("test_observability")
    )
    monkeypatch.setattr(observability_modulo, "_tracer_da_api", None)
    propagador = get_global_textmap()
    yield
    set_global_textmap(propagador)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite://")
    yield eng
    eng.dispose()


def _app_que_responde(onde: dict[str, Any]) -> FastAPI:
    """App com uma rota que guarda o que a requisicao trouxe do contexto."""
    app = FastAPI()

    @app.get("/x")
    def rota() -> dict[str, str]:
        onde["baggage"] = baggage.get_all()
        onde["span"] = trace.get_current_span().get_span_context()
        return {}

    return app


class TestFlagDesligada:
    def test_sem_flag_nao_instrumenta_nada(
        self, monkeypatch: pytest.MonkeyPatch, engine: Engine
    ) -> None:
        monkeypatch.delenv("OTEL_ENABLED", raising=False)
        app = FastAPI()

        assert configurar_otel(app, engine) is None

        assert app.middleware_stack is None
        with tracer_da_api().start_as_current_span("x") as span:
            assert not span.get_span_context().is_valid

    @pytest.mark.parametrize("valor", ["false", "False", "0", "", "off"], ids=repr)
    def test_valores_desligados_nao_instrumentam(
        self, monkeypatch: pytest.MonkeyPatch, engine: Engine, valor: str
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", valor)

        assert configurar_otel(FastAPI(), engine) is None


class TestFlagLigada:
    def test_monta_o_provider_da_api_e_o_devolve_para_o_encerramento(
        self,
        monkeypatch: pytest.MonkeyPatch,
        engine: Engine,
        otlp_falso: dict[str, Any],
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        monkeypatch.setenv("PYTSTOP_GIT_SHA", "abcdef0123456789")
        app = FastAPI()

        provedor = configurar_otel(app, engine)

        assert provedor is not None
        recurso = provedor.resource.attributes
        assert recurso["pytstop.processo"] == "api"
        assert recurso["service.version"] == "abcdef012345"
        assert otlp_falso["endpoint"] == "http://jaeger:4317"
        # O tracer da API (spans a mao do caso de uso) e o do mesmo provider.
        with tracer_da_api().start_as_current_span("saga cancelamento"):
            pass
        provedor.force_flush()
        (exportado,) = otlp_falso["exportados"]
        assert exportado.name == "saga cancelamento"
        assert exportado.resource.attributes["pytstop.processo"] == "api"
        provedor.shutdown()
        assert otlp_falso["encerrado"] is True

    def test_provider_de_fora_e_usado_e_nao_e_devolvido(
        self, monkeypatch: pytest.MonkeyPatch, engine: Engine
    ) -> None:
        # Quem criou o provider e quem o encerra: o lifespan so encerra o que montou.
        monkeypatch.delenv("OTEL_ENABLED", raising=False)
        rastreador = Rastreador("api")

        assert configurar_otel(FastAPI(), engine, rastreador.provedor) is None

        with tracer_da_api().start_as_current_span("saga prazo"):
            pass
        assert [s.name for s in rastreador.spans()] == ["saga prazo"]

    def test_instrumenta_o_app_e_o_engine_no_mesmo_provider(
        self, engine: Engine
    ) -> None:
        rastreador = Rastreador("api")
        app = _app_que_responde({})
        configurar_otel(app, engine, rastreador.provedor)

        with TestClient(app) as cliente:
            cliente.get("/x")
            with rastreador.tracer.start_as_current_span("fora"):
                pass
        servidor = [s for s in rastreador.spans() if s.kind is SpanKind.SERVER]
        assert [s.name for s in servidor] == ["GET /x"]

    def test_o_comando_de_banco_dentro_da_requisicao_e_filho_dela(
        self, engine: Engine
    ) -> None:
        rastreador = Rastreador("api")
        app = FastAPI()

        @app.get("/x")
        def rota() -> dict[str, str]:
            with engine.connect() as conexao:
                conexao.execute(text("SELECT 1"))
            return {}

        configurar_otel(app, engine, rastreador.provedor)

        TestClient(app).get("/x")
        TestClient(app).get("/x")

        (servidor, servidor_2) = [
            s for s in rastreador.spans() if s.kind is SpanKind.SERVER
        ]
        consultas = [s for s in rastreador.spans() if s.kind is SpanKind.CLIENT]
        assert {c.parent.span_id for c in consultas if c.parent} == {
            servidor.get_span_context().span_id,
            servidor_2.get_span_context().span_id,
        }

    def test_consulta_do_engine_fora_de_requisicao_nao_vira_trace(
        self, engine: Engine
    ) -> None:
        rastreador = Rastreador("api")
        configurar_otel(FastAPI(), engine, rastreador.provedor)

        with engine.connect() as conexao:
            conexao.execute(text("SELECT 1"))

        assert rastreador.spans() == []

    def test_reconstroi_o_middleware_stack_ja_construido(self, engine: Engine) -> None:
        """``configurar_otel`` roda no lifespan, DEPOIS de o Starlette construir o
        middleware_stack (o proprio scope `lifespan` atravessa o stack). O
        instrumentador do FastAPI so embrulha `build_middleware_stack`; sem o
        rebuild explicito o middleware OTel nunca entra no caminho HTTP — sintoma
        real observado: spans de SQLAlchemy presentes, de FastAPI ausentes."""
        rastreador = Rastreador("api")
        app = _app_que_responde({})
        app.middleware_stack = app.build_middleware_stack()
        antes = app.middleware_stack

        configurar_otel(app, engine, rastreador.provedor)

        assert app.middleware_stack is not antes
        TestClient(app).get("/x")
        assert [s.name for s in rastreador.spans()] == ["GET /x"]

    def test_nao_constroi_o_stack_antes_da_hora(self, engine: Engine) -> None:
        # Fora do lifespan o Starlette constroi o stack no primeiro scope, ja com o
        # patch do instrumentador: construir aqui seria antecipar estado.
        app = FastAPI()
        assert app.middleware_stack is None

        configurar_otel(app, engine, Rastreador("api").provedor)

        assert app.middleware_stack is None

    def test_segundo_app_no_mesmo_processo_tambem_e_instrumentado(self) -> None:
        # O instrumentador do SQLAlchemy e um singleton que so instrumenta uma vez:
        # `instrumentar_banco` desfaz o anterior (os testes montam varios apps).
        rastreador = Rastreador("api")
        for _ in range(2):
            eng = create_engine("sqlite://")
            instrumentar_banco(eng, rastreador.provedor)
            with (
                rastreador.tracer.start_as_current_span("raiz"),
                eng.connect() as conexao,
            ):
                conexao.execute(text("SELECT 1"))
            eng.dispose()

        assert len([s for s in rastreador.spans() if s.kind is SpanKind.CLIENT]) >= 2


class TestPropagacao:
    def test_baggage_do_chamador_nao_entra_no_processo(self, engine: Engine) -> None:
        visto: dict[str, Any] = {}
        app = _app_que_responde(visto)
        configurar_otel(app, engine, Rastreador("api").provedor)

        TestClient(app).get(
            "/x", headers={"traceparent": _PAI, "baggage": "usuario=CANARIO"}
        )

        assert visto["baggage"] == {}
        # O traceparent segue valendo: o trace do chamador continua.
        assert visto["span"].trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)

    def test_propagador_global_e_so_o_w3c(self, engine: Engine) -> None:
        configurar_otel(FastAPI(), engine, Rastreador("api").provedor)

        assert isinstance(get_global_textmap(), PropagadorW3C)

    def test_tracestate_de_ate_512_caracteres_segue(self, engine: Engine) -> None:
        visto: dict[str, Any] = {}
        app = _app_que_responde(visto)
        configurar_otel(app, engine, Rastreador("api").provedor)
        tracestate = ",".join(f"k{i:02d}=v{'x' * 12}" for i in range(28))
        assert len(tracestate) <= 512

        TestClient(app).get(
            "/x", headers={"traceparent": _PAI, "tracestate": tracestate}
        )

        assert visto["span"].trace_state.to_header() == tracestate

    def test_tracestate_acima_de_512_caracteres_e_descartado_na_entrada_http(
        self, engine: Engine
    ) -> None:
        visto: dict[str, Any] = {}
        app = _app_que_responde(visto)
        configurar_otel(app, engine, Rastreador("api").provedor)
        tracestate = ",".join(f"k{i:02d}=v{'x' * 12}" for i in range(32))
        assert len(tracestate) > 512

        TestClient(app).get(
            "/x", headers={"traceparent": _PAI, "tracestate": tracestate}
        )

        assert len(visto["span"].trace_state) == 0
        assert visto["span"].trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
