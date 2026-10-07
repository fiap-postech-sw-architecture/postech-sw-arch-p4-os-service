"""Contexto W3C (``traceparent``/``tracestate``) entre outbox, AMQP e spans (ADR-043).

A propagacao e explicita e so W3C Trace Context: nao depende de
``OTEL_PROPAGATORS`` nem do tracer provider global, e funciona com a exportacao
OTLP desligada (o span existe, so nao sai do processo). Os spans de mensagem
sao abertos a mao no relay e no consumidor, e nao pela instrumentacao do pika:
o contexto tem de atravessar a outbox (gravado na linha, pai do PRODUCER) e a
copia de retry, que a instrumentacao nao alcanca.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from opentelemetry.trace import StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from opentelemetry.context import Context
    from opentelemetry.trace import Span, SpanKind, Tracer
    from opentelemetry.util.types import AttributeValue

_PROPAGADOR = TraceContextTextMapPropagator()
# Tetos do que vem de fora (header AMQP): o traceparent da versao 00 tem 55
# caracteres e o W3C pede suporte a 512 no tracestate (o mesmo teto da coluna da
# outbox). Acima disso o valor e ignorado, e o SDK nao o repete no log.
_TETOS: Final = {"traceparent": 128, "tracestate": 512}


def cabecalhos_do_contexto_atual() -> dict[str, str]:
    """``traceparent``/``tracestate`` do span corrente (vazio sem span valido)."""
    cabecalhos: dict[str, str] = {}
    _PROPAGADOR.inject(cabecalhos)
    return cabecalhos


def contexto_dos_cabecalhos(cabecalhos: Mapping[str, object] | None) -> Context:
    """Contexto pai a partir dos headers AMQP ou das colunas da outbox.

    Valor que nao e texto (header binario ou ausente) ou acima do teto e
    ignorado; sem ``traceparent`` valido o contexto volta vazio e o span vira
    raiz.
    """
    portador = {
        chave: valor
        for chave, teto in _TETOS.items()
        if isinstance(valor := (cabecalhos or {}).get(chave), str)
        and len(valor) <= teto
    }
    return _PROPAGADOR.extract(portador)


@contextmanager
def span_de_mensagem(
    tracer: Tracer,
    nome: str,
    *,
    contexto: Context,
    tipo: SpanKind,
    atributos: Mapping[str, AttributeValue],
) -> Iterator[Span]:
    """Span de publicacao ou de consumo, corrente durante o bloco (logs inclusos).

    Erro que escapa marca o status so com o tipo da excecao, sem evento de
    excecao: a mensagem dela pode trazer dado da mensagem (placa, texto livre).
    """
    with tracer.start_as_current_span(
        nome,
        context=contexto,
        kind=tipo,
        attributes=atributos,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_status(StatusCode.ERROR, type(exc).__name__)
            raise
