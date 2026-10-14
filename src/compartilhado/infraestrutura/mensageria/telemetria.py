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
from typing import TYPE_CHECKING, Any, Final

from opentelemetry.propagators.textmap import Getter, default_getter
from opentelemetry.trace import StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from opentelemetry.context import Context
    from opentelemetry.propagators.textmap import CarrierT
    from opentelemetry.trace import Span, SpanKind, Tracer
    from opentelemetry.util.types import AttributeValue

# Tetos do que vem de fora (header AMQP ou HTTP): o traceparent da versao 00 tem
# 55 caracteres e o W3C pede suporte a 512 no tracestate (o mesmo teto da coluna
# da outbox). Acima disso o valor e ignorado, e o SDK nao o repete no log.
_TETOS: Final = {"traceparent": 128, "tracestate": 512}


class _LeitorComTeto(Getter[Any]):
    """Esconde o ``tracestate`` acima do teto: o contexto segue so pelo traceparent."""

    def __init__(self, leitor: Getter[Any]) -> None:
        self._leitor = leitor

    def get(self, carrier: CarrierT, key: str) -> list[str] | None:
        valores: list[str] | None = self._leitor.get(carrier, key)
        if (
            key == "tracestate"
            and valores is not None
            and len(",".join(valores)) > _TETOS["tracestate"]
        ):
            return None
        return valores

    def keys(self, carrier: CarrierT) -> list[str]:
        return self._leitor.keys(carrier)


class PropagadorW3C(TraceContextTextMapPropagator):
    """Contexto W3C so com ``traceparent`` e ``tracestate`` (sem ``baggage``).

    E o propagador global da API (``observability.configurar_otel``): o ``baggage``
    do chamador nao entra no processo nem sai dele, e o ``tracestate`` de mais de
    512 caracteres e descartado na entrada HTTP, como na leitura do AMQP e da
    outbox (``contexto_dos_cabecalhos``).
    """

    def extract(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        return super().extract(carrier, context, _LeitorComTeto(getter))


_PROPAGADOR = PropagadorW3C()


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


def marcar_falha(span: Span, motivo: str) -> None:
    """Marca o span com erro; o motivo vai no atributo ``pytstop.falha``.

    O ``motivo`` e o nome da classe da excecao ou um codigo fixo do servico, nunca
    o texto dela: a mensagem de uma excecao pode trazer dado da mensagem (placa,
    texto livre). A descricao do status nao leva o motivo, porque o provider a
    tira de todo span (``observability._SemTextoDeErro``).
    """
    span.set_status(StatusCode.ERROR)
    span.set_attribute("pytstop.falha", motivo)


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

    Erro que escapa marca o span so com o tipo da excecao (``marcar_falha``), sem
    evento de excecao: a mensagem dela pode trazer dado da mensagem (placa, texto
    livre).
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
            marcar_falha(span, type(exc).__name__)
            raise
