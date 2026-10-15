"""Instrumentacao OpenTelemetry (ADR-020 do p3, ADR-043).

A API, o relay, o consumidor e o prazos montam o proprio ``TracerProvider``
(``criar_provedor``), que nao e o global. A API instrumenta FastAPI e SQLAlchemy
(``configurar_otel``, chamado pelo lifespan de ``src/main.py``); os outros tres
processos abrem os spans de mensagem e da saga (``criar_tracer``) e instrumentam
o banco. O SQLAlchemy cobre o psycopg2 (instrumentar o driver a parte duplicaria
cada span de banco) e o cliente AMQP nao tem instrumentador: o relay e o
consumidor abrem os spans de publicacao e de consumo e levam o contexto na
outbox e nos headers (``mensageria/telemetria.py``).

Dois cuidados valem para todo span do provider, venha do codigo do servico ou do
instrumentador:

- o amostrador descarta o span de cliente sem pai (``_RaizSemClienteOrfao``): o
  poll do relay, a raspagem do ``/metrics``, o ciclo ocioso do prazos e a
  limpeza horaria so abrem esse tipo de span, e cada um virava um trace raiz;
- nenhum span leva a mensagem de uma excecao, o stack trace nem a descricao do
  status de erro (``_SemTextoDeErro``): o erro do driver traz o ``DETAIL`` do
  PostgreSQL com a linha inteira (placa, texto livre) e o dos instrumentadores
  fica por fora do scrub de PII do log. O motivo de uma falha vai em atributo
  fixo do span, nunca no texto do erro.

O que a API nao exporta: ``/metrics`` e as sondas (``/api/v1/saude`` e
``/api/v1/saude/pronto``), os spans de ``receive`` e ``send`` do ASGI, os
parametros dos comandos SQL, o caminho cru da requisicao e a query (o span leva o
template da rota, ``redigir_url_da_span``), o user-agent, o endereco do chamador
e o cabecalho ``Host``.

O SDK e as instrumentacoes sao dependencias principais (os spans dao o
``traceparent`` que segue pela outbox e pelo AMQP e o ``trace_id`` dos logs). O
extra ``otel``, opcional, traz so o exportador OTLP (com o grpcio): o import dele
e lazy, e a flag ``OTEL_ENABLED=true`` sem o extra instalado degrada para
warning, sem exportar, e nunca quebra o boot. O recurso de todo span leva
``service.name`` de ``OTEL_SERVICE_NAME`` (padrao ``pytstop-os-service``) e
``pytstop.processo`` (``api``, ``relay``, ``consumidor`` ou ``prazos``).
"""

from __future__ import annotations

import atexit
import os
from typing import TYPE_CHECKING, Any, Final

import structlog
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.semconv.attributes.exception_attributes import (
    EXCEPTION_MESSAGE,
    EXCEPTION_STACKTRACE,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

from src.compartilhado.infraestrutura.mensageria.telemetria import PropagadorW3C
from src.compartilhado.infraestrutura.rotas_http import ROTA_NAO_ROTEADA

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fastapi import FastAPI
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import Span as SpanDoSdk
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.trace import Link, Span, Tracer, TraceState
    from opentelemetry.util.types import Attributes
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

# O que o span guarda no lugar do que identifica a requisicao ou o chamador.
_REDIGIDO: Final = "REDACTED"
# URL da requisicao nas duas convencoes semanticas do OpenTelemetry (a antiga e a
# padrao; ``url.path`` e ``url.query`` sao as da nova).
_ATRIBUTOS_DE_URL: Final = ("url.path", "http.target", "http.url", "url.full")
# Quem chama: user-agent (texto livre), IP do par e o ``Host`` cru.
_TEXTOS_DO_CHAMADOR: Final = (
    "http.user_agent",
    "user_agent.original",
    "http.server_name",
    "net.peer.ip",
    "client.address",
)
# A porta do par. O instrumentador a grava como inteiro, e o span a guarda como
# ``0``: o mesmo tipo, para o backend que valida o tipo do atributo (o Billing e a
# Execucao fazem igual).
_PORTAS_DO_CHAMADOR: Final = ("net.peer.port", "client.port")
# Regex sobre a URL que o instrumentador monta (``scheme://<Host><path>``, sem a
# query), ancorada: o ``Host`` e do chamador, e um ``Host: x/api/v1/saude`` nao
# pode tirar da trace uma requisicao de outra rota. O prefixo de um segmento e o
# ``--root-path`` do uvicorn (``/os``). ``/api/v1/saude`` cobre ``/pronto``.
_URLS_SEM_TRACE: Final = (
    r"^https?://[^/]+(?:/[^/]+)?/(?:api/v1/saude(?:/pronto)?|metrics)$"
)

# Tracer do provider que ``configurar_otel`` monta, para os spans que o caso de
# uso abre a mao (``rastreamento_da_saga``); sem a exportacao ligada, o no-op.
_tracer_da_api: Tracer | None = None


class _RaizSemClienteOrfao(Sampler):
    """Raiz do ``ParentBased``: o span de cliente sem pai nao vira trace.

    So chega aqui o span sem pai. O de cliente (uma consulta do SQLAlchemy) sem
    span em volta e o poll do relay, a raspagem do coletor ou um ciclo ocioso:
    descartado. O de cliente com pai (a consulta de uma requisicao ou de uma
    mensagem) nao passa por aqui e segue a decisao do pai. Qualquer outro tipo de
    span sem pai (a requisicao, a publicacao, o consumo) abre o trace.
    """

    def should_sample(  # noqa: PLR0913  # assinatura do Sampler do SDK
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if kind is SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return "RaizSemClienteOrfao"


def _sem_texto_de_excecao(evento: Event) -> Event:
    if evento.name != "exception":
        return evento
    atributos = {
        chave: valor
        for chave, valor in (evento.attributes or {}).items()
        if chave not in {EXCEPTION_MESSAGE, EXCEPTION_STACKTRACE}
    }
    return Event(evento.name, atributos, evento.timestamp)


class _SemTextoDeErro(SpanProcessor):
    """Tira do span o texto de erro, que pode trazer dado do pedido, e repassa.

    A mensagem e o stack trace saem de todo evento ``exception`` (fica o tipo), e
    o status de erro perde a descricao: o instrumentador do SQLAlchemy a preenche
    com o texto do driver, o do FastAPI e o ``with`` do SDK, com o da excecao.

    E o primeiro processador do provider e e quem recebe o exportador: o span so
    chega a ele depois de limpo, em qualquer ordem em que se acrescentem
    processadores. O ``ReadableSpan`` nao tem setter, entao o processador troca
    os atributos protegidos ``_status`` e ``_events`` da copia que todos os
    processadores recebem (nao os do span vivo, que ja terminou); os testes de
    cada regra reprovam se o SDK mudar isso.
    """

    def __init__(self, destino: SpanProcessor | None = None) -> None:
        self._destino = destino

    def on_start(self, span: SpanDoSdk, parent_context: Context | None = None) -> None:
        if self._destino is not None:
            self._destino.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if span.status.status_code is StatusCode.ERROR:
            span._status = Status(StatusCode.ERROR)
        span._events = tuple(_sem_texto_de_excecao(e) for e in span.events)
        if self._destino is not None:
            self._destino.on_end(span)

    def shutdown(self) -> None:
        if self._destino is not None:
            self._destino.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self._destino is None:
            return True
        return self._destino.force_flush(timeout_millis)


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


def _exportador_otlp() -> SpanExporter | None:
    """O exportador OTLP/gRPC, ou ``None`` sem a flag ou sem o extra ``otel``."""
    if not _habilitado():
        return None
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError:
        _log.warning(
            "otel export disabled: OTEL_ENABLED=true but the 'otel' extra is not "
            "installed; install it with `uv sync --extra otel`",
        )
        return None
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT_PADRAO)
    # Insecure so quando o proprio endpoint declara http:// -- default
    # intra-cluster; https via env liga TLS (ver _ENDPOINT_PADRAO).
    _log.info("otel export enabled", endpoint=endpoint)
    return OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://"))


def criar_provedor(
    processo: str, *, exportador: SpanExporter | None = None
) -> TracerProvider:
    """``TracerProvider`` do processo, com o amostrador e a limpeza do texto de erro.

    Exporta por OTLP so com ``OTEL_ENABLED=true`` e o extra ``otel``; os spans
    existem sempre, e sem a exportacao nao saem do processo. ``exportador`` (os
    testes) toma o lugar do OTLP e exporta na hora, sem lote.
    """
    provedor = TracerProvider(
        sampler=ParentBased(root=_RaizSemClienteOrfao()),
        resource=Resource.create(atributos_do_recurso(processo)),
    )
    destino: SpanProcessor | None
    if exportador is not None:
        destino = SimpleSpanProcessor(exportador)
    elif (otlp := _exportador_otlp()) is not None:
        destino = BatchSpanProcessor(otlp)
    else:
        destino = None
    provedor.add_span_processor(_SemTextoDeErro(destino))
    return provedor


# O scope do ASGI: chaves de texto e valores de tipos variados (str, bytes, listas).
type _ScopeAsgi = Mapping[str, Any]


def redigir_url_da_span(span: Span, scope: _ScopeAsgi) -> None:
    """``server_request_hook`` do FastAPI: o span guarda o template da rota.

    O caminho cru traz o que identifica a requisicao (``/ordens-de-servico/<id>``)
    e a query pode trazer placa ou documento. O hook roda com o span ja criado e
    os atributos da instrumentacao gravados, e troca os de URL pelo template da
    rota (``http.route``, que o proprio instrumentador resolveu; sem rota casada,
    ``nao_roteada``, como a metrica HTTP), sem montar nem interpretar a URL, que
    leva o ``Host`` do chamador. ``url.query`` vira ``REDACTED`` quando ha query.
    Quem chama (user-agent, IP do par, ``Host``) tambem vira ``REDACTED`` e a
    porta do par, ``0``, nas duas convencoes.

    O template so e certo com a instrumentacao 0.66b ou mais nova: a 0.63b nao
    acha as rotas de ``include_router`` do FastAPI 0.137 em diante, grava o
    caminho cru em ``http.route`` e no nome do span e estoura no 405 (os testes de
    ``test_instrumentacao_da_api.py`` montam o app assim e reprovam com ela).
    """
    if not span.is_recording():
        return
    atributos = getattr(span, "attributes", None) or {}
    rota = atributos.get("http.route", ROTA_NAO_ROTEADA)
    for nome in _ATRIBUTOS_DE_URL:
        span.set_attribute(nome, rota)
    for nome in _TEXTOS_DO_CHAMADOR:
        if nome in atributos:
            span.set_attribute(nome, _REDIGIDO)
    for nome in _PORTAS_DO_CHAMADOR:
        if nome in atributos:
            span.set_attribute(nome, 0)
    if scope.get("query_string"):
        span.set_attribute("url.query", _REDIGIDO)


def instrumentar_api(app: FastAPI, provedor: TracerProvider) -> None:
    """Um span SERVER por requisicao, com o template da rota no lugar do caminho."""
    # Import so aqui: o relay, o consumidor e o prazos nao carregam o FastAPI.
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provedor,
        server_request_hook=redigir_url_da_span,
        excluded_urls=_URLS_SEM_TRACE,
        exclude_spans=["receive", "send"],
    )


def instrumentar_banco(engine: Engine, provedor: TracerProvider) -> None:
    """Um span CLIENT por comando SQL do engine, filho do span em andamento.

    Sem span em andamento (poll, raspagem, ciclo ocioso) o amostrador descarta. O
    span leva o texto do comando (so com os marcadores de parametro, o engine
    esconde os valores) e nunca o texto do erro do driver.
    """
    instrumentador = SQLAlchemyInstrumentor()
    # O instrumentador e um singleton e so instrumenta uma vez por processo: num
    # segundo ``instrument`` ele avisa e nao faz nada (os testes montam varios apps).
    if instrumentador.is_instrumented_by_opentelemetry:
        instrumentador.uninstrument()
    instrumentador.instrument(engine=engine, tracer_provider=provedor)


def configurar_otel(
    app: FastAPI, engine: Engine, provedor: TracerProvider | None = None
) -> TracerProvider | None:
    """Liga a instrumentacao do FastAPI e do SQLAlchemy da API.

    Sem ``provedor`` monta o da API, so com ``OTEL_ENABLED=true`` (default
    ``false``): ``/api/v1/saude`` e ``/metrics`` ficam fora da trace, as sondas
    do kubelet e a raspagem do Prometheus gerariam ruido continuo. Com
    ``provedor`` (os testes) instrumenta com ele, ligada a flag ou nao. O
    propagador global passa a ser so o W3C, sem ``baggage``.

    Returns:
        O provider que esta funcao montou, para o chamador encerra-lo (o
        ``shutdown`` descarrega o lote de spans); ``None`` quando nao montou
        nenhum: flag desligada ou ``provedor`` vindo de fora, que quem o criou
        encerra.
    """
    montado = None
    if provedor is None:
        if not _habilitado():
            return None
        provedor = montado = criar_provedor("api")
    instrumentar_api(app, provedor)
    instrumentar_banco(engine, provedor)
    # O instrumentador nao adiciona middleware: ele embrulha
    # `build_middleware_stack`. Como esta funcao roda no lifespan e o proprio
    # scope `lifespan` ja fez o Starlette construir o stack, o embrulho nunca
    # seria invocado — spans de SQLAlchemy apareceriam e os de FastAPI nao
    # (sintoma validado contra Jaeger real). Reconstruir aqui e seguro: o
    # uvicorn so aceita conexoes depois de o startup completar, entao nenhuma
    # request usa o stack antigo; o scope lifespan em andamento segue na
    # cadeia antiga, que delega ao mesmo router.
    if app.middleware_stack is not None:
        app.middleware_stack = app.build_middleware_stack()
    # Global: e o que o instrumentador do FastAPI usa para ler o contexto de
    # entrada. A saida (outbox e AMQP) usa o propagador de `telemetria.py`.
    set_global_textmap(PropagadorW3C())
    global _tracer_da_api  # noqa: PLW0603  # init-once, como o provider
    _tracer_da_api = provedor.get_tracer(f"{_NOME_DO_SERVICO}.api")
    _log.info("otel configured: api traces active")
    return montado


def tracer_da_api() -> Tracer:
    """Tracer da API: o do provider de ``configurar_otel``, ou o no-op sem ele."""
    from opentelemetry.trace import NoOpTracer

    return _tracer_da_api or NoOpTracer()


def criar_tracer(processo: str, engine: Engine | None = None) -> Tracer:
    """Tracer do relay, do consumidor ou do prazos (spans de mensagem, ADR-043).

    O SDK fica sempre ligado: os spans dao o ``traceparent`` que segue pela
    outbox e pelo AMQP e o ``trace_id`` dos logs. A exportacao OTLP so liga com
    ``OTEL_ENABLED=true`` e o extra ``otel`` instalado (a imagem o instala); sem
    ela os spans nao saem do processo. Com a exportacao ligada e o ``engine``, o
    banco do processo e instrumentado: cada comando SQL de uma mensagem ou de uma
    saga tratada vira um span filho dela.
    """
    provedor = criar_provedor(processo)
    if engine is not None and _habilitado():
        instrumentar_banco(engine, provedor)
    # Encerramento gracioso descarrega o ultimo lote de spans.
    atexit.register(provedor.shutdown)
    return provedor.get_tracer(f"{_NOME_DO_SERVICO}.{processo}")
