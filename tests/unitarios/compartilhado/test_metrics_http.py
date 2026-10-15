"""``GET /metrics`` e a latencia HTTP da API (ADR-032, ADR-043): sempre ligados.

As metricas vivem no registro padrao do ``prometheus_client``, sem flag, como no
Billing e na Execucao. O app e um FastAPI pequeno com as rotas incluidas por
``APIRouter``, como o do OS.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.requests import Request

from src.compartilhado.infraestrutura.metrics import (
    LATENCIA_HTTP,
    MetricasHTTPMiddleware,
    configurar_metricas,
)
from src.compartilhado.infraestrutura.rotas_http import ROTA_NAO_ROTEADA
from tests.conftest import existe_a_serie, valor_da_metrica

_NOME = "http_request_duration_seconds_count"
_TEMPLATE = "/api/v1/ordens-de-servico/{ordem_id}"


def _app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/api/v1/ordens-de-servico")

    @router.get("/{ordem_id}")
    def obter(ordem_id: str) -> dict[str, str]:
        return {"id": ordem_id}

    @router.get("/{ordem_id}/estoura")
    def estourar(ordem_id: str) -> None:
        msg = f"erro da ordem {ordem_id}"
        raise RuntimeError(msg)

    app.include_router(router)
    configurar_metricas(app)
    return app


def _contagem(metodo: str, rota: str, status: int) -> float:
    return valor_da_metrica(_NOME, method=metodo, rota=rota, status=str(status))


class TestLatenciaHttp:
    def test_request_observa_metodo_template_e_status(self) -> None:
        antes = _contagem("GET", _TEMPLATE, 200)

        resposta = TestClient(_app()).get("/api/v1/ordens-de-servico/abc-123")

        assert resposta.status_code == 200
        assert _contagem("GET", _TEMPLATE, 200) == antes + 1
        # O id da OS nunca vira rotulo.
        assert not existe_a_serie(
            _NOME,
            method="GET",
            rota="/api/v1/ordens-de-servico/abc-123",
            status="200",
        )

    def test_soma_a_duracao_no_histograma(self) -> None:
        antes = valor_da_metrica(
            "http_request_duration_seconds_sum",
            method="GET",
            rota=_TEMPLATE,
            status="200",
        )

        TestClient(_app()).get("/api/v1/ordens-de-servico/x")

        depois = valor_da_metrica(
            "http_request_duration_seconds_sum",
            method="GET",
            rota=_TEMPLATE,
            status="200",
        )
        assert depois > antes

    def test_sem_rota_casada_agrega_em_nao_roteada(self) -> None:
        antes = _contagem("GET", ROTA_NAO_ROTEADA, 404)

        TestClient(_app()).get("/api/v1/nao-existe/tok-3f9a1c")

        assert _contagem("GET", ROTA_NAO_ROTEADA, 404) == antes + 1
        assert not existe_a_serie(
            _NOME, method="GET", rota="/api/v1/nao-existe/tok-3f9a1c", status="404"
        )

    def test_metodo_nao_permitido_mantem_o_template(self) -> None:
        antes = _contagem("DELETE", _TEMPLATE, 405)

        resposta = TestClient(_app()).delete("/api/v1/ordens-de-servico/abc")

        assert resposta.status_code == 405
        assert _contagem("DELETE", _TEMPLATE, 405) == antes + 1

    def test_excecao_nao_tratada_conta_500_e_segue_propagando(self) -> None:
        template = "/api/v1/ordens-de-servico/{ordem_id}/estoura"
        antes = _contagem("GET", template, 500)

        with pytest.raises(RuntimeError, match="erro da ordem"):
            TestClient(_app()).get("/api/v1/ordens-de-servico/abc/estoura")

        assert _contagem("GET", template, 500) == antes + 1

    def test_metodo_fora_dos_conhecidos_vira_outro(self) -> None:
        # O metodo vem do chamador: um token novo por request explodiria a
        # cardinalidade do rotulo.
        antes = _contagem("OUTRO", ROTA_NAO_ROTEADA, 405)

        TestClient(_app()).request("PROPFIND", "/api/v1/nao-existe")

        assert (
            _contagem("OUTRO", ROTA_NAO_ROTEADA, 405)
            + _contagem("OUTRO", ROTA_NAO_ROTEADA, 404)
            > antes
        )
        assert not existe_a_serie(
            _NOME, method="PROPFIND", rota=ROTA_NAO_ROTEADA, status="404"
        )

    def test_buckets_de_5_ms_a_5_s(self) -> None:
        limites = [
            float(b)
            for b in LATENCIA_HTTP._upper_bounds  # contrato dos buckets
        ]

        assert limites == [
            0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, float("inf"),
        ]  # fmt: skip


class TestRotaMetrics:
    def test_responde_no_formato_do_prometheus_sem_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sempre ligada: nem a variavel de antes (API_METRICS_ENABLED) a desliga.
        monkeypatch.setenv("API_METRICS_ENABLED", "false")

        resposta = TestClient(_app()).get("/metrics")

        assert resposta.status_code == 200
        assert resposta.headers["content-type"] == CONTENT_TYPE_LATEST
        assert "# TYPE http_request_duration_seconds histogram" in resposta.text

    def test_rota_fica_fora_do_openapi(self) -> None:
        assert "/metrics" not in _app().openapi()["paths"]

    def test_middleware_fica_instalado_no_app(self) -> None:
        classes = [m.cls for m in _app().user_middleware]

        assert MetricasHTTPMiddleware in classes


def test_middleware_usa_so_o_scope_da_request() -> None:
    # Sem rota no scope (ex.: erro antes do roteamento) o rotulo e nao_roteada.
    from src.compartilhado.infraestrutura.metrics import _rota_template

    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": []})

    assert _rota_template(request) == ROTA_NAO_ROTEADA
