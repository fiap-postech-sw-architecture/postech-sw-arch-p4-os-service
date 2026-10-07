"""Contexto W3C (``traceparent``/``tracestate``) entre outbox, AMQP e spans (ADR-043).

A propagacao e explicita e so W3C Trace Context: nao depende de
``OTEL_PROPAGATORS`` nem do tracer provider global, e funciona com a exportacao
OTLP desligada (o span existe, so nao sai do processo).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.context import Context

_PROPAGADOR = TraceContextTextMapPropagator()
_CHAVES = ("traceparent", "tracestate")


def cabecalhos_do_contexto_atual() -> dict[str, str]:
    """``traceparent``/``tracestate`` do span corrente (vazio sem span valido)."""
    cabecalhos: dict[str, str] = {}
    _PROPAGADOR.inject(cabecalhos)
    return cabecalhos


def contexto_dos_cabecalhos(cabecalhos: Mapping[str, object] | None) -> Context:
    """Contexto pai a partir dos headers AMQP ou das colunas da outbox.

    Valor que nao e texto (header binario ou ausente) e ignorado; sem
    ``traceparent`` valido o contexto volta vazio e o span vira raiz.
    """
    portador = {
        chave: valor
        for chave in _CHAVES
        if isinstance(valor := (cabecalhos or {}).get(chave), str)
    }
    return _PROPAGADOR.extract(portador)
