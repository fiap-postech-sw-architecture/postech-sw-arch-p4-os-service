"""Span das acoes que mexem na saga por fora do consumidor (ADR-043)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry.trace import INVALID_SPAN_ID

from src.ordem_servico.aplicacao.saga.contexto_de_envio import sem_span
from src.ordem_servico.infraestrutura.rastreamento_da_saga import span_da_saga
from tests.rastreamento import Rastreador, traceparent
from tests.unitarios.ordem_servico.cenario_da_saga import CenarioDaSaga

if TYPE_CHECKING:
    from collections.abc import Iterator


def _contexto(rastreador: Rastreador, nome: str) -> str:
    """``traceparent`` de um span que ja terminou, como a saga o guardaria."""
    with rastreador.tracer.start_as_current_span(nome):
        pass
    (span,) = rastreador.spans(nome)
    return traceparent(span)


def test_span_filho_do_contexto_da_saga_com_link_para_a_requisicao() -> None:
    rastreador = Rastreador()
    da_saga = _contexto(rastreador, "transicao da saga")

    with (
        rastreador.tracer.start_as_current_span("POST cancelamento") as requisicao,
        span_da_saga(rastreador.tracer)("saga cancelamento", da_saga),
    ):
        pass

    (acao,) = rastreador.spans("saga cancelamento")
    (anterior,) = rastreador.spans("transicao da saga")
    assert acao.context.trace_id == anterior.context.trace_id
    assert acao.parent is not None
    assert acao.parent.span_id == anterior.context.span_id
    # O trace da requisicao e outro: o link e o que a liga a acao.
    assert acao.context.trace_id != requisicao.get_span_context().trace_id
    (link,) = acao.links
    assert link.context.span_id == requisicao.get_span_context().span_id


def test_sem_contexto_guardado_e_filho_do_span_corrente_sem_link() -> None:
    rastreador = Rastreador()

    with (
        rastreador.tracer.start_as_current_span("POST cancelamento") as requisicao,
        span_da_saga(rastreador.tracer)("saga cancelamento", None),
    ):
        pass

    (acao,) = rastreador.spans("saga cancelamento")
    assert acao.parent is not None
    assert acao.parent.span_id == requisicao.get_span_context().span_id
    assert acao.context.trace_id == requisicao.get_span_context().trace_id


def test_fora_de_qualquer_span_a_acao_abre_o_trace_sem_link() -> None:
    rastreador = Rastreador()

    with span_da_saga(rastreador.tracer)("saga cancelamento", None):
        pass

    (acao,) = rastreador.spans("saga cancelamento")
    assert (acao.parent, acao.links) == (None, ())
    assert acao.context.span_id != INVALID_SPAN_ID


def test_sem_span_nao_abre_nada() -> None:
    with sem_span("saga cancelamento", None) as aberto:
        assert aberto is None


def test_cancelamento_grava_o_comando_e_o_commit_dentro_do_span() -> None:
    cenario = CenarioDaSaga.em("aguardando_decisao")
    traceparent_da_saga = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    cenario.saga.registrar_contexto_de_trace(traceparent_da_saga)
    aberturas: list[tuple[str, str | None]] = []
    dentro: list[tuple[int, bool]] = []

    @contextmanager
    def span(nome: str, contexto: str | None) -> Iterator[None]:
        aberturas.append((nome, contexto))
        yield
        dentro.append((len(cenario.publicador.comandos), cenario.publicador.committed))

    antes = len(cenario.publicador.comandos)
    cenario.cancelar(span=span)

    # Aberto uma vez, com o contexto da saga; ao fechar, o comando ja estava na
    # outbox e o commit ja tinha saido.
    assert aberturas == [("saga cancelamento", traceparent_da_saga)]
    assert dentro == [(antes + 1, True)]
