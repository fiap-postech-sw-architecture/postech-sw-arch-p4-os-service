"""Gauges por consulta da API: o que so o banco sabe, calculado na raspagem (ADR-043).

Registrado so na API: relay, consumidor e prazos nao leem estas series, e a API
segue exportando com os outros processos parados, que e o que mantem de pe o
alerta de outbox parada. Cada replica repete o valor, e os paineis agregam com
``max``. As series (RFC-004 secao 9 e catalogo do ADR-043):

- saga: ``pytstop_saga_ativas``, ``pytstop_saga_etapa_mais_antiga_segundos`` e
  ``pytstop_saga_prazo_vencido_segundos``, mais ``pytstop_saga_coletor_disponivel``
  (``metricas_da_saga``);
- ``pytstop_outbox_mais_antiga_segundos``: idade da linha pendente mais antiga da
  outbox, 0 sem pendente;
- ``pytstop_os_por_status`` e ``pytstop_os_tempo_medio_status_segundos``: a mesma
  leitura do ``GET /api/v1/ordens-de-servico/metricas`` (RF-027 da fase 3), com a
  janela de 30 dias, guardada por 30 s para a raspagem de 15 s de cada replica
  nao repetir a consulta.

A raspagem do ``/metrics`` nunca pode pendurar (o Prometheus desiste em 10 s e o
alvo vira ``up=0`` justo no incidente do banco). Por isso uma raspagem abre UMA
conexao (``connect_timeout`` de 1 s), roda as consultas nela, cada uma com
``statement_timeout`` de 2 s e ``lock_timeout`` de 1 s, e a espera do resultado
tem um prazo total (``ORCAMENTO_S``): o trabalho roda numa thread, porque o
prazo do banco nao corta um socket que emudece depois de aberto. Passado o
prazo a raspagem sai com o que ja leu, e a que sobrou fica ausente, nunca em
zero. Com a thread anterior ainda presa, a raspagem seguinte nem tenta (uma
thread presa por vez) e sai sem os gauges, com ``pytstop_saga_coletor_disponivel``
em 0.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from src.compartilhado.infraestrutura.mensageria.outbox import (
    idade_da_pendente_mais_antiga,
)
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.consultas import ler_metricas_das_ordens
from src.ordem_servico.infraestrutura.metricas_da_saga import (
    FALHAS_DO_COLETOR,
    LeituraDaSaga,
    descrever_a_saga,
    disponibilidade,
    familias_da_saga,
    ler_sagas,
)
from src.ordem_servico.infraestrutura.prazo_da_saga import prazo_de_resposta_da_saga

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import timedelta

    from prometheus_client.registry import Collector
    from sqlalchemy import Connection

    from src.ordem_servico.aplicacao.dtos import MetricasDasOrdensDTO

_log = structlog.get_logger(__name__)

# Tempo maximo que a raspagem espera pelo banco, no total. Fica a metade do
# ``scrape_timeout`` de 10 s do Prometheus do platform.
ORCAMENTO_S: Final = 5.0
# Quanto a leitura das OS por status vale antes de o banco ser consultado de novo.
VALIDADE_DAS_ORDENS_S: Final = 30.0
# Cada consulta: 2 s de execucao e 1 s esperando um lock.
_PRAZO_DA_CONSULTA: Final = text("SET LOCAL statement_timeout = '2s'")
_PRAZO_DO_LOCK: Final = text("SET LOCAL lock_timeout = '1s'")


def _idade_da_outbox(valor: float | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_outbox_mais_antiga_segundos",
        "Idade da linha pendente mais antiga da outbox (0 sem pendente).",
        value=valor,
    )


def _os_por_status() -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_os_por_status",
        "Ordens de servico em cada status.",
        labels=["status"],
    )


def _tempo_medio_por_status() -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_os_tempo_medio_status_segundos",
        "Permanencia media da OS em cada status, da entrada ate a transicao "
        "seguinte, nos ultimos 30 dias (so status com permanencia concluida).",
        labels=["status"],
    )


def _familias_das_ordens(leitura: MetricasDasOrdensDTO) -> Iterator[GaugeMetricFamily]:
    por_status, tempo_medio = _os_por_status(), _tempo_medio_por_status()
    for status in StatusOrdem:
        por_status.add_metric([status.value], leitura.por_status.get(status.value, 0))
        segundos = leitura.tempo_medio_por_status_segundos.get(status.value)
        if segundos is not None:
            tempo_medio.add_metric([status.value], segundos)
    yield por_status
    yield tempo_medio


class _Leitura:
    """O que uma raspagem leu; cada campo e atribuido inteiro, de uma vez so."""

    def __init__(self) -> None:
        self.pronta = threading.Event()
        self.saga: LeituraDaSaga | None = None
        self.outbox: float | None = None
        self.ordens: MetricasDasOrdensDTO | None = None


class ColetorDaApi:
    """Os gauges por consulta da API, lidos a cada raspagem do ``/metrics``."""

    def __init__(  # noqa: PLR0913  # relogios e prazos injetados para os testes
        self,
        abrir_conexao: Callable[[], Connection],
        prazo: Callable[[], timedelta] = prazo_de_resposta_da_saga,
        *,
        relogio: Callable[[], float] = time.monotonic,
        agora: Callable[[], datetime] = lambda: datetime.now(UTC),
        orcamento_s: float = ORCAMENTO_S,
        validade_das_ordens_s: float = VALIDADE_DAS_ORDENS_S,
    ) -> None:
        self._abrir_conexao = abrir_conexao
        self._prazo = prazo
        self._relogio = relogio
        self._agora = agora
        self._orcamento_s = orcamento_s
        self._validade_das_ordens_s = validade_das_ordens_s
        # Uma thread de leitura por vez: so ela mexe no cache.
        self._ocupado = threading.Lock()
        self._ordens_em_cache: tuple[float, MetricasDasOrdensDTO] | None = None

    def describe(self) -> Iterator[GaugeMetricFamily]:
        """Os nomes, sem consultar o banco (o registro roda antes do boot terminar)."""
        yield from descrever_a_saga()
        yield _idade_da_outbox()
        yield _os_por_status()
        yield _tempo_medio_por_status()

    def collect(self) -> Iterator[GaugeMetricFamily]:
        leitura = _Leitura()
        if self._ocupado.acquire(blocking=False):
            if self._iniciar(leitura) and not leitura.pronta.wait(self._orcamento_s):
                _log.warning("api gauges unavailable", motivo="prazo_esgotado")
        else:
            _log.warning("api gauges unavailable", motivo="raspagem_anterior_presa")
        # Uma copia de cada campo: a thread que passou do prazo pode atribuir
        # o que falta, mas nunca muda o que esta raspagem ja leu.
        saga, outbox, ordens = leitura.saga, leitura.outbox, leitura.ordens
        if saga is None:
            FALHAS_DO_COLETOR.inc()
            yield disponibilidade(0)
        else:
            yield from familias_da_saga(saga)
            yield disponibilidade(1)
        if outbox is not None:
            yield _idade_da_outbox(outbox)
        if ordens is not None:
            yield from _familias_das_ordens(ordens)

    def _iniciar(self, leitura: _Leitura) -> bool:
        """Abre a thread de leitura; sem thread (limite de pids), devolve a vez.

        Sem isto, o ``start()`` que levanta deixaria a trava tomada para sempre: o
        ``release`` mora no ``finally`` de ``_ler``, que nunca chegaria a rodar.
        """
        try:
            threading.Thread(
                target=self._ler, args=(leitura,), daemon=True, name="coletor-da-api"
            ).start()
        except Exception as exc:  # noqa: BLE001  # RuntimeError "can't start new thread"
            self._ocupado.release()
            _log.warning("api gauges unavailable", motivo=type(exc).__name__)
            return False
        return True

    def _ler(self, leitura: _Leitura) -> None:
        """Corpo da thread de leitura: nunca levanta e sempre devolve a vez."""
        try:
            self._ler_tudo(leitura)
        except Exception as exc:  # noqa: BLE001  # a raspagem nunca cai pelo coletor
            _log.warning("api gauges unavailable", motivo=type(exc).__name__)
        finally:
            leitura.pronta.set()
            self._ocupado.release()

    def _ler_tudo(self, leitura: _Leitura) -> None:
        """Uma conexao, uma consulta por gauge, cada campo atribuido ao ficar pronto."""
        with self._abrir_conexao() as conexao:
            leitura.saga = self._consultar(
                conexao, "saga", lambda c: ler_sagas(c, self._prazo())
            )
            leitura.outbox = self._consultar(
                conexao, "outbox", idade_da_pendente_mais_antiga
            )
            leitura.ordens = self._ordens(conexao)

    def _ordens(self, conexao: Connection) -> MetricasDasOrdensDTO | None:
        agora = self._relogio()
        if (
            self._ordens_em_cache is not None
            and agora - self._ordens_em_cache[0] < self._validade_das_ordens_s
        ):
            return self._ordens_em_cache[1]
        leitura = self._consultar(
            conexao, "ordens", lambda c: ler_metricas_das_ordens(c, self._agora())
        )
        if leitura is not None:
            self._ordens_em_cache = (agora, leitura)
        return leitura

    @staticmethod
    def _consultar[T](
        conexao: Connection, nome: str, ler: Callable[[Connection], T]
    ) -> T | None:
        """O que ``ler`` devolve, ou ``None`` se a consulta falhou (gauge ausente).

        Cada consulta tem a transacao dela: a que falha desfaz so a sua, e a
        conexao segue boa para as outras.
        """
        try:
            with conexao.begin():
                conexao.execute(_PRAZO_DA_CONSULTA)
                conexao.execute(_PRAZO_DO_LOCK)
                return ler(conexao)
        except Exception as exc:  # noqa: BLE001  # a raspagem nunca cai pelo coletor
            _log.warning("api gauge unavailable", gauge=nome, erro=type(exc).__name__)
            return None


_coletor: Collector | None = None


def registrar_coletor(abrir_conexao: Callable[[], Connection]) -> Collector | None:
    """Registra o ``ColetorDaApi`` no registro padrao.

    Devolve o coletor registrado, ou ``None`` se o processo ja tem um (dois apps
    no mesmo processo, os testes): quem recebe o coletor e quem o tira com
    ``desregistrar_coletor``.
    """
    global _coletor  # noqa: PLW0603  # um coletor por processo
    if _coletor is not None:
        return None
    _coletor = ColetorDaApi(abrir_conexao)
    REGISTRY.register(_coletor)
    return _coletor


def desregistrar_coletor(coletor: Collector) -> None:
    """Tira do registro o coletor que ``registrar_coletor`` devolveu."""
    global _coletor  # noqa: PLW0603  # um coletor por processo
    REGISTRY.unregister(coletor)
    _coletor = None
