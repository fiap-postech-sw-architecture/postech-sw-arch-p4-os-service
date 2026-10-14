"""Laco do processo ``prazos`` (RFC-004 secao 4.6): um ciclo a cada intervalo.

O ``VencerPrazos`` faz o ciclo; aqui ficam o ritmo, a saude e o encerramento,
como no relay e no consumidor: o heartbeat e tocado a cada volta, a cada saga
tratada e a cada fatia da espera (a liveness do Kubernetes olha a idade dele), e
o arquivo de pronto existe enquanto o broker esta de pe. O SIGTERM e visto entre
duas sagas e entre as fatias da espera: o processo termina a saga em curso, que
comita ou desfaz inteira, e sai. Banco fora nao derruba o
processo: o ciclo falha, vai para o log, e o seguinte tenta de novo. O laco
ocioso nao abre span: so a saga tratada abre o dela (ADR-043).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

import structlog
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    import threading

    from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador
    from src.ordem_servico.aplicacao.saga.vencer_prazos import (
        ResultadoDoCiclo,
        VencerPrazos,
    )
    from src.ordem_servico.infraestrutura.presenca_do_consumidor import (
        PresencaPikaDoConsumidor,
    )

_log = structlog.get_logger(__name__)

# Fatia da espera entre ciclos: a cada uma o laco toca o heartbeat e atende o
# heartbeat AMQP (30 s no pika), e o SIGTERM interrompe a espera.
_FATIA_S: Final = 5.0


class ProcessoDosPrazos:
    """Roda o ``VencerPrazos`` a cada ``intervalo_s`` ate o ``parar`` (SIGTERM)."""

    def __init__(
        self,
        *,
        vencer: VencerPrazos,
        presenca: PresencaPikaDoConsumidor,
        sinal: Sinalizador,
        intervalo_s: float,
    ) -> None:
        self._vencer = vencer
        self._presenca = presenca
        self._sinal = sinal
        self._intervalo_s = intervalo_s

    def executar(self, parar: threading.Event) -> None:
        """Laco principal; broker ou banco fora nao derrubam o processo."""
        _log.info("prazos started", intervalo_s=self._intervalo_s)
        try:
            while not parar.is_set():
                self._sinal.bater()
                try:
                    ciclo = self._vencer.executar(
                        self._depois_de_cada_saga, parar=parar.is_set
                    )
                except SQLAlchemyError:
                    # Banco fora (failover, blip do pool): o proximo ciclo
                    # tenta de novo; derrubar o processo so reiniciaria o pod.
                    _log.exception("deadline cycle failed")
                else:
                    _registrar(ciclo)
                self._esperar(parar)
        finally:
            self._presenca.fechar()
            _log.info("prazos stopped")

    def _depois_de_cada_saga(self) -> None:
        """Heartbeat do processo e do broker a cada saga tratada.

        Um ciclo longo (muitas sagas vencidas de uma vez, depois de uma parada)
        nao pode passar dos dois heartbeats AMQP sem atender o broker.
        """
        self._sinal.bater()
        self._presenca.manter()

    def _esperar(self, parar: threading.Event) -> None:
        """Espera o intervalo em fatias, com a saude e o broker em dia."""
        fim = time.monotonic() + self._intervalo_s
        while not parar.is_set() and (restante := fim - time.monotonic()) > 0:
            parar.wait(min(_FATIA_S, restante))
            self._sinal.bater()
            self._presenca.manter()


def _registrar(ciclo: ResultadoDoCiclo) -> None:
    """O ciclo com trabalho (ou recusa) vai para o log; o ocioso, nao."""
    if ciclo.reenviadas or ciclo.esgotadas or ciclo.recusadas:
        _log.info(
            "deadline cycle done",
            reenviadas=ciclo.reenviadas,
            esgotadas=ciclo.esgotadas,
            recusadas=ciclo.recusadas,
        )
