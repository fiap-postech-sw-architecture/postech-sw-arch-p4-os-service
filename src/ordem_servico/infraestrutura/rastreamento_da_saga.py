"""Span das acoes que mexem na saga por fora do consumidor (ADR-043).

O cancelamento chega com trace proprio, o da requisicao do atendente. A acao
abre o span ``saga cancelamento`` como filho do contexto que a saga guardou na
ultima transicao (``sagas.traceparent``), com span link para a requisicao: os
comandos que ela grava na outbox, e o contexto que a propria saga passa a
guardar, seguem no trace da saga. A camada de aplicacao so conhece o
``SpanDaSaga``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Link, SpanKind

from src.compartilhado.dominio.exceptions import DomainException
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_dos_cabecalhos,
    marcar_falha,
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
        ) as span:
            try:
                yield
            except DomainException:
                # Recusa de negocio (404, 409, 422): resposta esperada, e a
                # mensagem pode ecoar o que o cliente enviou.
                raise
            except Exception as exc:
                # Falha inesperada (banco, contrato da outbox, invariante): o
                # span marca o erro com o tipo, sem a mensagem nem o traceback.
                marcar_falha(span, type(exc).__name__)
                raise

    return abrir
