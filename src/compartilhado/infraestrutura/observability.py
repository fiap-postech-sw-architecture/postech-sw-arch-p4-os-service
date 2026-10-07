"""Instrumentacao OpenTelemetry (ADR-020 do p3, ADR-043).

Auto-instrumentation de FastAPI + SQLAlchemy exportando traces OTLP/gRPC
direto para o Jaeger all-in-one do cluster de demo. Telemetria e detalhe de
borda (ADR-015): nenhuma camada interna importa OTel. ``configurar_otel`` e
chamado pelo lifespan em ``src/main.py``; ``criar_tracer``, pelo relay e pelo
consumidor, que abrem os spans de mensagem (ADR-043).

O SDK do OpenTelemetry e dependencia principal (o relay e o consumidor abrem
spans sempre, para o ``traceparent`` seguir pela outbox e pelo AMQP). O extra
``otel``, opcional, traz so o exportador OTLP (com o grpcio) e as
instrumentacoes da API: os imports deles sao lazy, e a flag
``OTEL_ENABLED=true`` sem o extra instalado degrada para warning, sem
exportar, e nunca quebra o boot. Sem a flag a API nem monta o provider.

O recurso de todo span leva ``service.name`` de ``OTEL_SERVICE_NAME`` (padrao
``pytstop-os-service``), como Billing e Execucao, e ``pytstop.processo``
(``api``, ``relay`` ou ``consumidor``), que separa os spans de cada processo.
"""

from __future__ import annotations

import atexit
import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.trace import Tracer
    from sqlalchemy import Engine

_log = structlog.get_logger(__name__)

# Porta 4317 = OTLP/gRPC do Jaeger all-in-one da stack de observabilidade
# (nome DNS `jaeger`). `http://` aqui e trafego intra-cluster (o DNS
# `jaeger` nao resolve fora); um collector
# externo entra via OTEL_EXPORTER_OTLP_ENDPOINT com https, que desliga o
# modo insecure automaticamente (hotspot SonarQube revisado como seguro).
_ENDPOINT_PADRAO = "http://jaeger:4317"
_VALORES_VERDADEIROS = frozenset({"true", "1"})
_NOME_DO_SERVICO = "pytstop-os-service"

# Marcador que substitui a query string nos spans (TD-017).
_QUERY_REDIGIDA = "REDACTED"


def _redigir_pii_da_span(span: object, scope: object) -> None:
    """``server_request_hook`` do FastAPIInstrumentor: remove PII da query.

    A instrumentacao HTTP grava ``url.query``/``http.target`` nos spans, e uma
    query com PII (CPF/CNPJ/placa) chegaria ao Jaeger (TD-017, do tempo em que
    o acompanhamento publico usava query params; hoje placa e documento vao no
    corpo do POST e o hook segue como defesa para qualquer rota). Ele roda na
    criacao do span server — DEPOIS de a instrumentacao setar os atributos
    padrao — e sobrescreve os que carregam a query: ``url.query`` vira
    ``REDACTED`` e ``http.target`` fica so com o path.

    Funcao pura (sem import de OpenTelemetry) para ser testavel com o extra
    ``otel`` ausente: opera apenas sobre o protocolo do ``span`` (set_attribute
    / is_recording) e o ``scope`` ASGI.
    """
    is_recording = getattr(span, "is_recording", None)
    if not callable(is_recording) or not is_recording():
        return
    set_attribute = getattr(span, "set_attribute", None)
    if not callable(set_attribute):
        return
    query = scope.get("query_string", b"") if isinstance(scope, dict) else b""
    if query:
        # So marca REDACTED quando havia query de fato; sem query nao ha o
        # que redigir e o atributo nao e escrito.
        set_attribute("url.query", _QUERY_REDIGIDA)
    path = scope.get("path", "") if isinstance(scope, dict) else ""
    if isinstance(path, str) and path:
        # http.target (conv. antiga) carregava path + "?" + query; url.path
        # (conv. nova) e so o path — ambos ficam sem a query.
        set_attribute("http.target", path)
        set_attribute("url.path", path)


def configurar_otel(app: FastAPI, engine: Engine) -> bool:
    """Liga a auto-instrumentacao FastAPI + SQLAlchemy com export OTLP.

    Le ``OTEL_ENABLED`` (default ``"false"``); quando ligada, monta
    TracerProvider (recurso de ``atributos_do_recurso("api")``) com
    BatchSpanProcessor -> OTLPSpanExporter gRPC no endpoint
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` (default ``http://jaeger:4317``; o scheme
    ``http://`` seleciona canal gRPC sem TLS). ``/api/v1/saude`` fica fora do
    trace — probes do kubelet e healthchecks gerariam ruido continuo.

    Returns:
        True quando a instrumentacao foi ativada; False quando a flag esta
        desligada ou o extra ``otel`` nao esta instalado (warning logado).
    """
    if not _habilitado():
        return False

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _log.warning(
            "otel ignorado: OTEL_ENABLED=true mas o extra 'otel' nao esta "
            "instalado; instale com `uv sync --extra otel`",
        )
        return False

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT_PADRAO)
    provider = TracerProvider(resource=Resource.create(atributos_do_recurso("api")))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint,
                # Insecure so quando o proprio endpoint declara http:// --
                # default intra-cluster; https via env liga TLS (ver
                # _ENDPOINT_PADRAO).
                insecure=endpoint.startswith("http://"),
            )
        )
    )
    # Pod kill/SIGTERM nao perde os ultimos spans: o shutdown() descarrega o batch.
    atexit.register(provider.shutdown)

    # tracer_provider explicito nos dois instrumentadores em vez de
    # trace.set_tracer_provider global: o provider global so aceita um set
    # por processo (warm restart/testes logariam "Overriding ... not
    # allowed") e nada mais no app le o provider global.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="/api/v1/saude",
        # Redige placa/documento da query string antes de exportar (TD-017).
        server_request_hook=_redigir_pii_da_span,
    )
    # O instrumentador (0.63b) nao adiciona middleware: ele embrulha
    # `build_middleware_stack`. Como esta funcao roda no lifespan e o proprio
    # scope `lifespan` ja fez o Starlette construir o stack, o embrulho nunca
    # seria invocado — spans de SQLAlchemy apareceriam e os de FastAPI nao
    # (sintoma validado contra Jaeger real). Reconstruir aqui e seguro: o
    # uvicorn so aceita conexoes depois de o startup completar, entao nenhuma
    # request usa o stack antigo; o scope lifespan em andamento segue na
    # cadeia antiga, que delega ao mesmo router.
    if app.middleware_stack is not None:
        app.middleware_stack = app.build_middleware_stack()
    SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provider)
    _log.info("otel configurado: traces OTLP ativos", endpoint=endpoint)
    return True


def atributos_do_recurso(processo: str) -> dict[str, str]:
    """``service.name``, ``service.version`` e o processo, para o ``Resource``.

    O nome explicito venceria o ``OTEL_SERVICE_NAME`` que o SDK le sozinho:
    por isso ele e lido aqui, com o padrao do servico.
    """
    return {
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "").strip()
        or _NOME_DO_SERVICO,
        # Mesmo SHA curto do banner de boot e dos logs (logging.py).
        "service.version": os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12],
        "pytstop.processo": processo,
    }


def _habilitado() -> bool:
    return (
        os.environ.get("OTEL_ENABLED", "false").strip().lower() in _VALORES_VERDADEIROS
    )


def criar_tracer(processo: str) -> Tracer:
    """Tracer do relay ou do consumidor (spans de mensagem, ADR-043).

    O SDK fica sempre ligado: os spans dao o ``traceparent`` que segue pela
    outbox e pelo AMQP e o ``trace_id`` dos logs. A exportacao OTLP so liga com
    ``OTEL_ENABLED=true`` e o extra ``otel`` instalado (a imagem o instala);
    sem ela os spans nao saem do processo.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create(atributos_do_recurso(processo)))
    if _habilitado():
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            _log.warning(
                "otel export disabled: OTEL_ENABLED=true but the 'otel' extra is "
                "not installed",
            )
        else:
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT_PADRAO)
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=endpoint, insecure=endpoint.startswith("http://")
                    )
                )
            )
            _log.info("otel export enabled", endpoint=endpoint, processo=processo)
    # Encerramento gracioso descarrega o ultimo lote de spans.
    atexit.register(provider.shutdown)
    return provider.get_tracer(f"{_NOME_DO_SERVICO}.{processo}")
