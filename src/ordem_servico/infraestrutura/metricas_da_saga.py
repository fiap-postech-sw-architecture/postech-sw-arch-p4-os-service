"""Metricas da saga (RFC-004 secao 9, ADR-043), no registro do prometheus_client.

Contadores e histograma: a ``Saga`` registra os fatos (``SagaIniciadaEvent``,
``EtapaDaSagaAlteradaEvent``), o repositorio os entrega a sessao no ``salvar``
e eles so viram metrica no ``after_commit`` da transacao raiz. Rollback,
conflito de versao ou commit que falha os descartam: a mensagem que volta pela
retry nao conta duas vezes. Quem emite e
o processo que tira a saga da etapa (API na abertura, consumidor nos eventos).

Gauges: o ``ColetorDaSaga``, registrado so na API, consulta ``sagas`` na hora
da raspagem, numa conexao propria e com prazo curto, e continua certo com os
outros processos fora do ar; as replicas repetem o valor, e os paineis agregam
com ``max``. A serie ``pytstop_saga_coletor_disponivel`` diz se a leitura deu
certo.

Series de rotulo fechado comecam em zero no boot de cada processo, para o
``increase()`` dos paineis nao perder o primeiro evento.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Final

import structlog
from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import String, event, func, select, text, type_coerce
from sqlalchemy.orm import Session

from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaDaSagaAlteradaEvent,
    SagaIniciadaEvent,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    ETAPAS_FINAIS,
    ETAPAS_NAO_FINAIS,
)
from src.ordem_servico.infraestrutura.mapping import sagas_table

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence
    from typing import Any

    from prometheus_client.registry import Collector
    from sqlalchemy import Connection, Row
    from sqlalchemy.orm import SessionTransaction

    from src.compartilhado.dominio.events import DomainEvent

_log = structlog.get_logger(__name__)

SAGAS_INICIADAS: Final = Counter(
    "pytstop_saga_iniciadas_total", "Sagas abertas junto com a OS."
)
FALHAS_DO_COLETOR: Final = Counter(
    "pytstop_saga_coletor_falhas_total",
    "Raspagens em que o coletor dos gauges da saga nao leu as sagas.",
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
for _etapa in ETAPAS_FINAIS:
    SAGAS_FINALIZADAS.labels(resultado=_etapa.value)
for _etapa in ETAPAS_NAO_FINAIS:
    DURACAO_DA_ETAPA.labels(etapa=_etapa.value)

# Fatos da transacao em curso, na sessao: so valem depois do commit.
_FATOS: Final = "pytstop_fatos_da_saga"
_sessoes_instrumentadas = False


def anotar(sessao: Session, fatos: Iterable[DomainEvent]) -> None:
    """Guarda a metrica de cada fato da saga ate o commit da transacao da ``sessao``.

    Raises:
        TypeError: fato sem metrica (um fato novo da saga sem a observacao
            dele), antes do commit: nada se perde em silencio.
    """
    observacoes = [_observacao(fato) for fato in fatos]
    _instrumentar_sessoes()
    sessao.info.setdefault(_FATOS, []).extend(observacoes)


def _observacao(fato: DomainEvent) -> Callable[[], None]:
    """A metrica do fato, para rodar depois do commit."""
    match fato:
        case SagaIniciadaEvent():
            return SAGAS_INICIADAS.inc
        case EtapaDaSagaAlteradaEvent():
            return partial(_etapa_alterada, fato)
    msg = f"fato da saga sem metrica: {type(fato).__name__}"
    raise TypeError(msg)


def _etapa_alterada(fato: EtapaDaSagaAlteradaEvent) -> None:
    DURACAO_DA_ETAPA.labels(etapa=fato.etapa_anterior.value).observe(
        fato.permanencia.total_seconds()
    )
    if fato.etapa_nova in ETAPAS_FINAIS:
        SAGAS_FINALIZADAS.labels(resultado=fato.etapa_nova.value).inc()


def _instrumentar_sessoes() -> None:
    global _sessoes_instrumentadas  # noqa: PLW0603  # init-once flag
    if _sessoes_instrumentadas:
        return
    _sessoes_instrumentadas = True
    event.listen(Session, "after_commit", _ao_comitar)
    event.listen(Session, "after_transaction_end", _ao_encerrar)


def _ao_comitar(sessao: Session) -> None:
    """Commit da transacao raiz: as metricas dos fatos valem.

    Liberar um SAVEPOINT tambem dispara o ``after_commit``; a transacao raiz
    ainda pode ser desfeita, entao ali nada conta.
    """
    if sessao.in_nested_transaction():
        return
    for observar in sessao.info.pop(_FATOS, ()):
        observar()


def _ao_encerrar(sessao: Session, transacao: SessionTransaction) -> None:
    """Transacao raiz encerrada sem commit (rollback ou close): descarta os fatos."""
    if transacao.parent is None:
        sessao.info.pop(_FATOS, None)


class ColetorDaSaga:
    """Gauges por consulta: ``pytstop_saga_ativas``, ``..._etapa_mais_antiga_segundos``.

    Uma consulta por raspagem, pelo indice parcial ``ix_sagas_ativas``, numa
    conexao propria (fora do pool das requisicoes) e com prazo de 2 s para a
    consulta e de 1 s para esperar um lock. Qualquer falha (banco fora, tabela
    travada, prazo esgotado) omite os dois gauges, que ficam ausentes e nunca
    zero, conta em ``pytstop_saga_coletor_falhas_total`` e publica
    ``pytstop_saga_coletor_disponivel`` em 0: o alerta "Saga parada" nao fica
    mudo com o coletor quebrado. Etapa que esta versao nao conhece (rollout com
    versoes misturadas) fica fora da contagem, sem derrubar a raspagem.
    """

    def __init__(self, abrir_conexao: Callable[[], Connection]) -> None:
        self._abrir_conexao = abrir_conexao

    def describe(self) -> Iterator[GaugeMetricFamily]:
        """Nomes dos gauges sem consultar o banco (registro antes do boot)."""
        yield from _gauges()
        yield _disponibilidade()

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            linhas = self._consultar()
        except Exception as exc:  # noqa: BLE001  # a raspagem nunca cai pelo coletor
            FALHAS_DO_COLETOR.inc()
            _log.warning("saga gauges unavailable", erro=type(exc).__name__)
            yield _disponibilidade(0)
            return
        por_etapa = {etapa: (total, float(idade)) for etapa, total, idade in linhas}
        ativas, mais_antiga = _gauges()
        for etapa in ETAPAS_NAO_FINAIS:
            total, idade = por_etapa.get(etapa.value, (0, 0.0))
            ativas.add_metric([etapa.value], total)
            mais_antiga.add_metric([etapa.value], idade)
        yield ativas
        yield mais_antiga
        yield _disponibilidade(1)

    def _consultar(self) -> Sequence[Row[Any]]:
        t = sagas_table
        consulta = (
            select(
                # Texto, nao o enum: uma etapa de outra versao nao quebra a leitura.
                type_coerce(t.c.etapa, String),
                func.count(),
                func.extract("epoch", func.now() - func.min(t.c.etapa_desde)),
            )
            .where(t.c.etapa.not_in(ETAPAS_FINAIS))
            .group_by(t.c.etapa)
        )
        with self._abrir_conexao() as conexao, conexao.begin():
            conexao.execute(text("SET LOCAL statement_timeout = '2s'"))
            conexao.execute(text("SET LOCAL lock_timeout = '1s'"))
            return conexao.execute(consulta).all()


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


def _disponibilidade(valor: int | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_saga_coletor_disponivel",
        "1 se o coletor leu as sagas nesta raspagem; 0 se falhou.",
        value=valor,
    )


_coletor: Collector | None = None


def registrar_coletor(abrir_conexao: Callable[[], Connection]) -> None:
    """Registra o ``ColetorDaSaga`` no registro padrao (uma vez por processo)."""
    global _coletor  # noqa: PLW0603  # init-once flag
    if _coletor is not None:
        return
    _coletor = ColetorDaSaga(abrir_conexao)
    REGISTRY.register(_coletor)
