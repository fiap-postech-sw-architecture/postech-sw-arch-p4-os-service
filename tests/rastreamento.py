"""Tracer de teste com os spans em memoria (InMemorySpanExporter do SDK)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Tracer


class Rastreador:
    """Um provider por teste: os spans terminados ficam em ``spans()``."""

    def __init__(self) -> None:
        self._exportador = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self._exportador))
        self.tracer: Tracer = provider.get_tracer("testes")

    def spans(self, nome: str | None = None) -> list[ReadableSpan]:
        terminados = list(self._exportador.get_finished_spans())
        return [s for s in terminados if nome is None or s.name == nome]


def traceparent(span: ReadableSpan) -> str:
    """``traceparent`` W3C do span (versao 00, com as flags do contexto)."""
    contexto = span.get_span_context()
    return (
        f"00-{contexto.trace_id:032x}-{contexto.span_id:016x}-"
        f"{contexto.trace_flags:02x}"
    )
