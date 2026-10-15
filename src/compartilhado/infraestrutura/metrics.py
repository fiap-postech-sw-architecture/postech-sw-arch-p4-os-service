"""Metricas Prometheus da API (herdado do p3 @ 08dcffe, ADR-032 e ADR-043).

As metricas ficam no registro padrao do ``prometheus_client``, sempre ligadas
(como no Billing e na Execucao), e a rota ``GET /metrics`` (``generate_latest``)
as serve no proprio FastAPI: o Prometheus raspa a API na mesma porta HTTP.

- ``http_request_duration_seconds`` (histograma) com os labels ``method``,
  ``rota`` e ``status``, observado pelo ``MetricasHTTPMiddleware``. ``rota`` e o
  TEMPLATE da rota casada (ex.: ``/api/v1/ordens-de-servico/{ordem_id}``), nunca o
  caminho bruto: cardinalidade limitada. Requests sem rota casada agregam em
  ``nao_roteada``, e um metodo fora dos conhecidos, em ``OUTRO``. Buckets de 5 ms
  a 5 s, para p50, p90 e p99.
- ``pytstop_os_criadas_total``: ``ordem_servico/infraestrutura/metrics.py``.
- Os gauges por consulta (saga, outbox e OS por status): o ``ColetorDaApi``, em
  ``ordem_servico/infraestrutura/coletor_da_api.py``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from src.compartilhado.infraestrutura.rotas_http import ROTA_NAO_ROTEADA

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request

# O metodo vem do chamador e pode ser qualquer token: so os conhecidos viram label.
_METODOS: Final = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_METODO_DESCONHECIDO: Final = "OUTRO"

# Buckets de latencia HTTP: 5ms..5s cobre do hit de cache ao pior
# caso com lock pessimista, com resolucao suficiente para p50/p90/p99.
LATENCIA_HTTP: Final = Histogram(
    "http_request_duration_seconds",
    "Duracao das requests HTTP da API por rota.",
    ["method", "rota", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


class MetricasHTTPMiddleware(BaseHTTPMiddleware):
    """Observa a latencia de cada request no histograma ``LATENCIA_HTTP``.

    O label ``rota`` usa o path TEMPLATE da rota casada (o router grava
    ``scope["route"]`` durante o roteamento, visivel aqui apos o ``call_next``
    porque o scope e compartilhado); sem rota casada, agrega em ``nao_roteada``.
    Excecao nao tratada conta como status 500 e segue propagando (o
    ``try/finally`` garante a observacao sem engolir o erro).
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        inicio = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            LATENCIA_HTTP.labels(
                method=_metodo(request),
                rota=_rota_template(request),
                status=str(status),
            ).observe(time.perf_counter() - inicio)


def _metodo(request: Request) -> str:
    return request.method if request.method in _METODOS else _METODO_DESCONHECIDO


def _rota_template(request: Request) -> str:
    """Path template da rota casada (``APIRoute.path_format``/``Mount.path``)."""
    rota = request.scope.get("route")
    template = getattr(rota, "path_format", None) or getattr(rota, "path", None)
    return template if isinstance(template, str) else ROTA_NAO_ROTEADA


def _expor_metricas(_request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def configurar_metricas(app: FastAPI) -> None:
    """Expoe ``GET /metrics`` e instala o ``MetricasHTTPMiddleware``.

    Deve rodar em ``criar_app`` (antes do boot do servidor): middleware nao pode ser
    adicionado com o app ja servindo, e o adicionado por ultimo fica o mais externo
    e mede a latencia completa.
    """
    # Rota (nao mount): um mount so casa `/metrics/` e, sem redirect de barra,
    # o scrape em `/metrics` daria 404.
    app.add_route("/metrics", _expor_metricas, include_in_schema=False)
    app.add_middleware(MetricasHTTPMiddleware)
