"""Metricas da saga (RFC-004 secao 9, ADR-043), no registro do prometheus_client.

Contadores e histograma: a ``Saga`` registra os fatos (``SagaIniciadaEvent``,
``EtapaDaSagaAlteradaEvent``), o repositorio os entrega a sessao no ``salvar``
e eles so viram metrica no ``after_commit``. Rollback ou conflito de versao os
descartam: a mensagem que volta pela retry nao conta duas vezes. Quem emite e
o processo que tira a saga da etapa (API na abertura, consumidor nos eventos).

Gauges: o ``ColetorDaSaga``, registrado so na API, consulta ``sagas`` na hora
da raspagem e continua certo com os outros processos fora do ar; as replicas
repetem o valor, e os paineis agregam com ``max``.

Series de rotulo fechado comecam em zero no boot de cada processo, para o
``increase()`` dos paineis nao perder o primeiro evento.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    SagaIniciadaEvent,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import ETAPAS_NAO_FINAIS
from src.ordem_servico.infraestrutura.mapping import sagas_table

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from prometheus_client.registry import Collector
    from sqlalchemy.orm import SessionTransaction

    from src.compartilhado.dominio.events import DomainEvent

_log = structlog.get_logger(__name__)

SAGAS_INICIADAS: Final = Counter(
    "pytstop_saga_iniciadas_total", "Sagas abertas junto com a OS."
)
SAGAS_FINALIZADAS: Final = Counter(
    "pytstop_saga_finalizadas_total",
    "Sagas encerradas, por resultado (concluida ou compensada).",
    ["resultado"],
)
DURACAO_DA_ETAPA: Final = Histogram(
    "pytstop_saga_etapa_duracao_segundos",
    "Permanencia da saga em cada etapa, observada quando ela sai da etapa.",
    ["etapa"],
    # De segundos (resposta automatica) a 7 dias (espera humana).
    buckets=(5, 30, 120, 600, 3600, 14400, 86400, 259200, 604800),
)
_FINAIS: Final = (EtapaSaga.CONCLUIDA, EtapaSaga.COMPENSADA)
for _etapa in _FINAIS:
    SAGAS_FINALIZADAS.labels(resultado=_etapa.value)
for _etapa in ETAPAS_NAO_FINAIS:
    DURACAO_DA_ETAPA.labels(etapa=_etapa.value)

# Fatos da transacao em curso, na sessao: so valem depois do commit.
_FATOS: Final = "pytstop_fatos_da_saga"
_sessoes_instrumentadas = False


def anotar(sessao: Session, fatos: Iterable[DomainEvent]) -> None:
    """Guarda os fatos da saga ate o commit da transacao da ``sessao``."""
    _instrumentar_sessoes()
    sessao.info.setdefault(_FATOS, []).extend(fatos)


def _instrumentar_sessoes() -> None:
    global _sessoes_instrumentadas  # noqa: PLW0603  # init-once flag
    if _sessoes_instrumentadas:
        return
    _sessoes_instrumentadas = True
    event.listen(Session, "after_commit", _ao_comitar)
    event.listen(Session, "after_transaction_end", _ao_encerrar)


def _ao_comitar(sessao: Session) -> None:
    for fato in sessao.info.pop(_FATOS, ()):
        _observar(fato)


def _ao_encerrar(sessao: Session, transacao: SessionTransaction) -> None:
    """Transacao raiz encerrada sem commit (rollback ou close): descarta os fatos."""
    if transacao.parent is None:
        sessao.info.pop(_FATOS, None)


def _observar(fato: DomainEvent) -> None:
    if isinstance(fato, SagaIniciadaEvent):
        SAGAS_INICIADAS.inc()
    elif isinstance(fato, EtapaDaSagaAlteradaEvent):
        DURACAO_DA_ETAPA.labels(etapa=fato.etapa_anterior.value).observe(
            fato.permanencia.total_seconds()
        )
        if fato.etapa_nova in _FINAIS:
            SAGAS_FINALIZADAS.labels(resultado=fato.etapa_nova.value).inc()


class ColetorDaSaga:
    """``pytstop_saga_ativas`` e ``..._etapa_mais_antiga_segundos`` por etapa.

    Uma consulta por raspagem, pelo indice parcial ``ix_sagas_ativas``. Banco
    fora do ar: a raspagem segue sem os dois gauges (ausentes, nunca zero, que
    esconderia uma saga parada).
    """

    def __init__(self, abrir_sessao: Callable[[], Session]) -> None:
        self._abrir_sessao = abrir_sessao

    def describe(self) -> Iterator[GaugeMetricFamily]:
        """Nomes dos gauges sem consultar o banco (registro antes do boot)."""
        yield from _gauges()

    def collect(self) -> Iterator[GaugeMetricFamily]:
        t = sagas_table
        consulta = (
            select(
                t.c.etapa,
                func.count(),
                func.extract("epoch", func.now() - func.min(t.c.etapa_desde)),
            )
            .where(t.c.etapa.not_in(_FINAIS))
            .group_by(t.c.etapa)
        )
        try:
            with self._abrir_sessao() as sessao:
                linhas = sessao.execute(consulta).all()
        except (SQLAlchemyError, RuntimeError) as exc:
            # RuntimeError: raspagem antes de a API configurar a sessao.
            _log.warning("saga gauges unavailable", erro=type(exc).__name__)
            return
        por_etapa = {etapa: (total, float(idade)) for etapa, total, idade in linhas}
        ativas, mais_antiga = _gauges()
        for etapa in ETAPAS_NAO_FINAIS:
            total, idade = por_etapa.get(etapa, (0, 0.0))
            ativas.add_metric([etapa.value], total)
            mais_antiga.add_metric([etapa.value], idade)
        yield ativas
        yield mais_antiga


def _gauges() -> tuple[GaugeMetricFamily, GaugeMetricFamily]:
    return (
        GaugeMetricFamily(
            "pytstop_saga_ativas", "Sagas em cada etapa nao final.", labels=["etapa"]
        ),
        GaugeMetricFamily(
            "pytstop_saga_etapa_mais_antiga_segundos",
            "Idade, na etapa, da saga mais antiga de cada etapa nao final.",
            labels=["etapa"],
        ),
    )


_coletor: Collector | None = None


def registrar_coletor(abrir_sessao: Callable[[], Session]) -> None:
    """Registra o ``ColetorDaSaga`` no registro padrao (uma vez por processo)."""
    global _coletor  # noqa: PLW0603  # init-once flag
    if _coletor is not None:
        return
    _coletor = ColetorDaSaga(abrir_sessao)
    REGISTRY.register(_coletor)
