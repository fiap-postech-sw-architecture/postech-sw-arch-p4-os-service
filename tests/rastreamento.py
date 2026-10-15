"""Tracer de teste com os spans em memoria (InMemorySpanExporter do SDK) e os
logs capturados de um teste (``Saidas``), para as provas de LGPD."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from src.compartilhado.infraestrutura.observability import criar_provedor

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.trace import Tracer


class Rastreador:
    """Um provider por teste, o de producao (amostrador e limpeza do texto de erro).

    Os spans terminados ficam em ``spans()``; ``provedor`` e o que o app de teste
    instrumenta (``criar_app(provedor=...)``).
    """

    def __init__(self, processo: str = "testes") -> None:
        self._exportador = InMemorySpanExporter()
        self.provedor: TracerProvider = criar_provedor(
            processo, exportador=self._exportador
        )
        self.tracer: Tracer = self.provedor.get_tracer("testes")

    def spans(self, nome: str | None = None) -> list[ReadableSpan]:
        terminados = list(self._exportador.get_finished_spans())
        return [s for s in terminados if nome is None or s.name == nome]


def tudo_que_o_span_leva(span: ReadableSpan) -> str:
    """Tudo o que o span leva ao exportador, em texto: o oraculo das provas de LGPD.

    Nome, atributos, eventos, links, descricao do status e recurso: procurar so
    nos atributos deixou passar o texto do erro e a descricao do status. O escopo
    da instrumentacao (nome do tracer) nao sai no ``to_json()``.
    """
    return json.dumps(json.loads(span.to_json()), ensure_ascii=False, sort_keys=True)


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
        partes += [tudo_que_o_span_leva(span) for span in rastreador.spans()]
        return "\n".join(partes)
