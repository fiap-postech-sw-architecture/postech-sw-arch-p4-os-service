"""Metricas Prometheus do servico (herdado do p3 @ 08dcffe, ADR-032).

Um ``MeterProvider`` OTel com ``PrometheusMetricReader`` registra os
instrumentos no ``REGISTRY`` default do ``prometheus_client``; a exposicao e a
rota ``GET /metrics`` (``generate_latest``) no proprio FastAPI: o Prometheus
raspa a API na mesma porta HTTP.

Metricas (meter ``pytstop-os-service``):

- ``http_request_duration`` (``unit="s"`` — o exportador anexa a unidade ao
  nome: serie ``http_request_duration_seconds``) com labels ``method``,
  ``rota`` e ``status``, observada pelo ``MetricasHTTPMiddleware``. ``rota`` e
  o TEMPLATE da rota casada (ex.: ``/api/v1/ordens-de-servico/{ordem_id}``),
  nunca o path bruto — cardinalidade limitada; requests sem rota casada
  agregam em ``nao_roteada``. Buckets 5ms..5s para p50/p90/p99.
- ``pytstop_os_criadas_total`` (counter): OS abertas. Incrementado pelo
  listener de ``src/ordem_servico/infraestrutura/metrics.py``.
- ``pytstop_os_duracao_status_segundos`` (histogram, label ``status``):
  permanencia da OS no status ANTERIOR, observada a cada transicao. SEM
  ``unit=``: o nome e contrato com os dashboards e ``unit="s"`` geraria o
  duplo ``_segundos_seconds``. Mesmo listener acima.

Default OFF: sem ``API_METRICS_ENABLED`` truthy a funcao retorna antes de
qualquer import de OpenTelemetry — custo zero para CI e testes. Imports lazy
porque o extra ``otel`` fica fora do grupo ``dev``: flag ligada sem o extra
degrada para warning + no-op, e a fachada ``metricas_api`` e no-op enquanto
``configurar_metricas_api`` nao vincula os instrumentos reais.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI

    # So anotacao: o extra `otel` fica fora do ambiente de lint/teste, e o
    # override `opentelemetry.*` (ignore_missing_imports) mantem o mypy verde
    # sem ele (os tipos degradam para Any nesse ambiente).
    from opentelemetry.metrics import Counter, Histogram
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request

_log = structlog.get_logger(__name__)

_VALORES_VERDADEIROS = frozenset({"true", "1"})
_NOME_METER = "pytstop-os-service"

# Label de rota para requests que nao casaram rota nenhuma (404 de path
# desconhecido): agrega tudo num unico valor em vez de explodir a
# cardinalidade com paths arbitrarios.
_ROTA_NAO_ROTEADA: Final = "nao_roteada"

# Buckets de latencia HTTP: 5ms..5s cobre do hit de cache ao pior
# caso com lock pessimista, com resolucao suficiente para p50/p90/p99.
_BUCKETS_LATENCIA_HTTP: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)

# Buckets de permanencia em status: de 1 minuto a 7 dias — uma OS
# fica horas/dias em cada status, nao milissegundos.
_BUCKETS_DURACAO_STATUS: Final = (
    60.0,
    300.0,
    900.0,
    3600.0,
    14400.0,
    43200.0,
    86400.0,
    259200.0,
    604800.0,
)


class MetricasApi:
    """Fachada dos instrumentos da API; no-op ate ``configurar_metricas_api``.

    O middleware HTTP e o listener de ordem_servico chamam os metodos em todo
    request/flush. Enquanto as metricas estao desligadas (ou o extra ``otel``
    ausente) os instrumentos ficam ``None`` e os metodos sao no-op — os
    caminhos quentes nunca quebram nem custam nada.
    """

    def __init__(self) -> None:
        self._http_duracao: Histogram | None = None
        self._os_criadas: Counter | None = None
        self._os_duracao_status: Histogram | None = None

    def _vincular(
        self,
        *,
        http_duracao: Histogram,
        os_criadas: Counter,
        os_duracao_status: Histogram,
    ) -> None:
        self._http_duracao = http_duracao
        self._os_criadas = os_criadas
        self._os_duracao_status = os_duracao_status

    def observar_http(
        self, *, metodo: str, rota: str, status: int, duracao_s: float
    ) -> None:
        if self._http_duracao is not None:
            self._http_duracao.record(
                duracao_s,
                attributes={"method": metodo, "rota": rota, "status": str(status)},
            )

    def os_criada(self) -> None:
        if self._os_criadas is not None:
            self._os_criadas.add(1)

    def os_duracao_status(self, status: str, duracao_s: float) -> None:
        if self._os_duracao_status is not None:
            self._os_duracao_status.record(duracao_s, attributes={"status": status})


# Singleton modulo-level usado pelo middleware abaixo e pelo listener de
# ``src/ordem_servico/infraestrutura/metrics.py``. No-op por padrao;
# ``configurar_metricas_api`` vincula os instrumentos reais.
metricas_api = MetricasApi()


class MetricasHTTPMiddleware(BaseHTTPMiddleware):
    """Observa a latencia de cada request no histograma da fachada.

    Instalado por ``configurar_metricas_api`` (so quando as metricas estao
    ligadas). O label ``rota`` usa o path TEMPLATE da rota casada (o router
    grava ``scope["route"]`` durante o roteamento, visivel aqui apos o
    ``call_next`` porque o scope e compartilhado); sem rota casada, agrega em
    ``nao_roteada``. Excecao nao tratada conta como status 500 e segue
    propagando (o ``try/finally`` garante a observacao sem engolir o erro).
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
            metricas_api.observar_http(
                metodo=request.method,
                rota=_rota_template(request),
                status=status,
                duracao_s=time.perf_counter() - inicio,
            )


def _rota_template(request: Request) -> str:
    """Path template da rota casada (``APIRoute.path_format``/``Mount.path``)."""
    rota = request.scope.get("route")
    template = getattr(rota, "path_format", None) or getattr(rota, "path", None)
    return template if isinstance(template, str) else _ROTA_NAO_ROTEADA


def _habilitado() -> bool:
    return (
        os.environ.get("API_METRICS_ENABLED", "false").strip().lower()
        in _VALORES_VERDADEIROS
    )


def configurar_metricas_api(app: FastAPI) -> bool:
    """Liga as metricas OTel da API expostas via Prometheus.

    Le ``API_METRICS_ENABLED`` (default ``"false"``); quando ligada, monta um
    ``MeterProvider`` (service.name=pytstop-os-service, service.version=
    PYTSTOP_GIT_SHA curto) com um ``PrometheusMetricReader`` — o reader
    registra os instrumentos no ``REGISTRY`` default do ``prometheus_client``
    — expoe ``GET /metrics`` e instala o
    ``MetricasHTTPMiddleware``. Deve rodar em ``criar_app`` (antes do boot do
    servidor): middleware nao pode ser adicionado com o app ja servindo.

    Returns:
        True quando as metricas foram ativadas; False quando a flag esta
        desligada ou o extra ``otel`` nao esta instalado (warning logado) —
        nesse caso nada e montado e a fachada segue no-op.
    """
    if not _habilitado():
        return False

    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        _log.warning(
            "metricas da API ignoradas: API_METRICS_ENABLED=true mas o extra "
            "'otel' nao esta instalado; instale com `uv sync --extra otel`",
        )
        return False

    resource = Resource.create(
        {
            "service.name": _NOME_METER,
            # Mesmo SHA curto do banner de boot e dos logs (logging.py).
            "service.version": os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12],
        }
    )
    # O reader registra no REGISTRY default do prometheus_client; a rota
    # /metrics abaixo serve esse mesmo registry.
    reader = PrometheusMetricReader()
    provider = MeterProvider(metric_readers=[reader], resource=resource)
    meter = provider.get_meter(_NOME_METER)

    metricas_api._vincular(
        http_duracao=meter.create_histogram(
            "http_request_duration",
            unit="s",
            description="Duracao das requests HTTP da API por rota.",
            explicit_bucket_boundaries_advisory=list(_BUCKETS_LATENCIA_HTTP),
        ),
        os_criadas=meter.create_counter(
            "pytstop_os_criadas_total",
            description="Ordens de servico criadas.",
        ),
        os_duracao_status=meter.create_histogram(
            "pytstop_os_duracao_status_segundos",
            description=(
                "Permanencia (segundos) da OS no status anterior, "
                "observada a cada transicao."
            ),
            explicit_bucket_boundaries_advisory=list(_BUCKETS_DURACAO_STATUS),
        ),
    )

    def _expor_metricas(_request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # Rota (nao mount): um mount so casa `/metrics/` e, sem redirect de barra,
    # o scrape em `/metrics` daria 404.
    app.add_route("/metrics", _expor_metricas, include_in_schema=False)
    app.add_middleware(MetricasHTTPMiddleware)
    _log.info("metricas da API ativas: /metrics exposto")
    return True
