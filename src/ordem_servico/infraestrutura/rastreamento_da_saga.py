"""Span das acoes que mexem na saga por fora do consumidor (ADR-043).

O cancelamento, e depois a retomada e o prazo, chegam com trace proprio (a
requisicao do atendente, o ciclo do processo de prazos). Cada um abre o span da
acao como filho do contexto que a saga guardou na ultima transicao
(``sagas.traceparent``), com span link para o que o disparou: os comandos que a
acao grava na outbox, e o contexto que a propria saga passa a guardar, seguem
no trace da saga. A camada de aplicacao so conhece o ``SpanDaSaga``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Link, SpanKind

from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_dos_cabecalhos,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.trace import Tracer

    from src.ordem_servico.aplicacao.saga.contexto_de_envio import SpanDaSaga


def span_da_saga(tracer: Tracer) -> SpanDaSaga:
    """O ``SpanDaSaga`` que abre os spans no ``tracer``."""

    @contextmanager
    def abrir(nome: str, traceparent: str | None) -> Iterator[None]:
        disparo = trace.get_current_span().get_span_context()
        contexto = (
            contexto_dos_cabecalhos({"traceparent": traceparent})
            if traceparent is not None
            else None
        )
        with tracer.start_as_current_span(
            nome,
            context=contexto,
            kind=SpanKind.INTERNAL,
            links=[Link(disparo)] if disparo.is_valid else [],
            record_exception=False,
            set_status_on_exception=False,
        ):
            yield

    return abrir
