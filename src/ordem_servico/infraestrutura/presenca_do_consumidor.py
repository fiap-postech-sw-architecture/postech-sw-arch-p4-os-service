"""Presenca do consumidor do OS no RabbitMQ (RFC-004 secao 4.6, ADR-036).

O processo ``prazos`` confere, antes de cada saga, a fila ``os.eventos``: uma
declaracao passiva devolve ``consumer_count`` e ``message_count`` (as mensagens
prontas) sem criar nem mudar nada (o usuario ``os`` ja le a fila). Sem consumidor,
com resposta esperando na fila ou com o broker fora, o ciclo nao reenvia nem
esgota prazo. A conexao e a do processo (``ConexaoDoProcesso``): conectado, o
processo fica pronto; sem o broker, fora de pronto, sem reinicio. Ela reconecta
a cada consulta que acha o canal fechado, sem backoff: o intervalo do ciclo e o
espacamento das tentativas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pika.exceptions import ChannelClosedByBroker

from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.contratos import FILA
from src.ordem_servico.aplicacao.ports import SituacaoDaFila

if TYPE_CHECKING:
    import pika

    from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador

_log = structlog.get_logger(__name__)


class PresencaPikaDoConsumidor:
    """``PresencaDoConsumidor`` pela declaracao passiva de ``os.eventos``."""

    def __init__(
        self, parametros: pika.ConnectionParameters, sinal: Sinalizador
    ) -> None:
        self._broker = amqp.ConexaoDoProcesso(
            parametros, processo="prazos", sinal=sinal, declarar=self._declarar
        )

    def situacao_da_fila(self) -> SituacaoDaFila | None:
        """Consumidores e mensagens prontas de ``os.eventos``.

        Conecta se preciso. Sem o broker, a fila ou a permissao nela, ``None``:
        o proximo ciclo tenta de novo, depois de o laco esperar o intervalo.
        """
        if self._broker.canal is None and not self._broker.conectar():
            return None
        try:
            fila = self._broker.canal.queue_declare(FILA, passive=True).method
        except (*amqp.ERROS_DE_CONEXAO, ChannelClosedByBroker) as exc:
            _log.warning(
                "broker connection lost while checking the consumer",
                erro=type(exc).__name__,
            )
            self._broker.desconectar()
            return None
        return SituacaoDaFila(
            consumidores=fila.consumer_count, prontas=fila.message_count
        )

    def manter(self) -> None:
        """Atende o heartbeat do broker entre os ciclos; a queda desconecta."""
        if self._broker.conexao is None:
            return
        try:
            self._broker.atender()
        except amqp.ERROS_DE_CONEXAO:
            _log.warning("broker connection lost while idle")
            self._broker.desconectar()

    def fechar(self) -> None:
        """Fecha a conexao, sem propagar erro."""
        self._broker.desconectar()

    def _declarar(self, canal: Any) -> None:  # noqa: ANN401  # BlockingChannel (pika sem tipos)
        # Declaracao passiva so do que o usuario `os` alcanca: a fila que ele le.
        canal.queue_declare(FILA, passive=True)
