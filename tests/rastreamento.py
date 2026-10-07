"""Tracer de teste com os spans em memoria (InMemorySpanExporter do SDK) e os
logs capturados de um teste (``Saidas``), para as provas de LGPD."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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


class RegistrosDoLog(logging.Handler):
    """Handler a mais no root: guarda os registros do logging (o do pika)."""

    def __init__(self) -> None:
        super().__init__()
        self.registros: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.registros.append(record)


@dataclass
class Saidas:
    """Os logs de um teste: eventos do structlog antes de renderizar e do logging."""

    stdlib: RegistrosDoLog
    eventos: list[dict[str, Any]] = field(default_factory=list)

    def texto(self, rastreador: Rastreador) -> str:
        """Tudo o que saiu: linhas de log (as duas pilhas) e spans."""
        partes = [repr(evento) for evento in self.eventos]
        partes += [
            f"{registro.getMessage()} {registro.exc_text or ''}"
            for registro in self.stdlib.registros
        ]
        for span in rastreador.spans():
            partes += [span.name, repr(dict(span.attributes or {}))]
            partes += [span.status.description or "", repr(span.events)]
        return "\n".join(partes)
