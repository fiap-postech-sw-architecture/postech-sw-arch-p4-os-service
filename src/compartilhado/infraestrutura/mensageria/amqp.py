"""Conexao AMQP (pika) do relay e do consumidor (ADR-036).

Parametros e propriedades do contrato, e o ciclo de vida da conexao de cada
processo: conectar com as declaracoes passivas, desconectar e esperar antes de
reconectar, com backoff exponencial e *full jitter*.
"""

from __future__ import annotations

import contextlib
import random
import time
from typing import TYPE_CHECKING, Any, Final

import pika
import structlog
from pika.exceptions import (
    AMQPConnectionError,
    ChannelClosedByBroker,
    ChannelWrongStateError,
)
from prometheus_client import Counter

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Mapping

    from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador

_log = structlog.get_logger(__name__)

# Queda do broker (e nao falha de uma mensagem): conexao recusada ou perdida,
# heartbeat vencido, canal usado depois que a conexao caiu.
ERROS_DE_CONEXAO: Final[tuple[type[Exception], ...]] = (
    AMQPConnectionError,
    ChannelWrongStateError,
    OSError,
)

# Heartbeat de 30 s: o pika e o broker dao a conexao por perdida depois de dois
# heartbeats sem resposta, entao o broker mudo e notado em cerca de 70 s (com o
# broker pausado, o relay ficou ate 67 s preso num publish). A sonda de liveness
# tolera 90 s sem heartbeat do processo.
_HEARTBEAT_S: Final = 30
# Broker com alarme de memoria bloqueia a conexao: depois disso, ela cai e o
# processo reconecta, em vez de ficar parado num publish.
_BLOQUEIO_MAXIMO_S: Final = 30
_TIMEOUT_DE_SOCKET_S: Final = 5

RECONEXAO_BASE_S: Final = 1.0
RECONEXAO_TETO_S: Final = 30.0
# Conexao que ficou de pe esse tempo era estavel: a queda seguinte recomeca o
# backoff do minimo. Um canal que o broker fecha a cada mensagem cai antes.
CONEXAO_ESTAVEL_S: Final = 60.0

# Metrica do alerta de processo preso em reconexao (mensagem que o pika nao
# decodifica, permissao revogada, broker fora): uma por espera antes de
# reconectar, no /metrics do proprio processo.
RECONEXOES: Final = Counter(
    "pytstop_reconexoes_ao_broker_total",
    "Esperas antes de reconectar ao RabbitMQ (conexao perdida ou recusada).",
    ["processo"],
)


def parametros(url: str, processo: str) -> pika.URLParameters:
    """Parametros da conexao a partir do ``RABBITMQ_URL`` do servico."""
    params = pika.URLParameters(url)
    params.heartbeat = _HEARTBEAT_S
    params.blocked_connection_timeout = _BLOQUEIO_MAXIMO_S
    params.socket_timeout = _TIMEOUT_DE_SOCKET_S
    params.connection_attempts = 1
    params.client_properties = {"connection_name": f"pytstop-os-service {processo}"}
    return params


def usuario(params: pika.ConnectionParameters) -> str:
    """Usuario da conexao: o ``user_id`` que o broker exige em toda publicacao."""
    nome: str = params.credentials.username
    return nome


def conectar(params: pika.ConnectionParameters) -> tuple[Any, Any]:
    """Abre a conexao e um canal com publisher confirms.

    Devolve ``(BlockingConnection, BlockingChannel)`` como ``Any``: o pika nao
    publica anotacoes de tipo.
    """
    conexao = pika.BlockingConnection(params)
    try:
        canal = conexao.channel()
        canal.confirm_delivery()
    except Exception:
        fechar(conexao)
        raise
    return conexao, canal


def fechar(conexao: Any) -> None:  # noqa: ANN401  # BlockingConnection (pika sem tipos)
    """Fecha a conexao sem propagar erro (ela pode ja estar morta)."""
    # Best-effort: no encerramento ou na reconexao a conexao costuma ja ter caido.
    with contextlib.suppress(Exception):
        if conexao.is_open:
            conexao.close()


def propriedades(
    envelope: Mapping[str, Any], *, usuario: str, cabecalhos: Mapping[str, Any]
) -> pika.BasicProperties:
    """Propriedades AMQP do envelope (RFC-004 secao 5.2).

    ``user_id`` e o usuario da conexao, que o broker confere (406 se outro).
    """
    return pika.BasicProperties(
        message_id=envelope["id"],
        correlation_id=envelope["correlation_id"],
        type=envelope["tipo"],
        user_id=usuario,
        content_type="application/json",
        delivery_mode=pika.DeliveryMode.Persistent,
        headers=dict(cabecalhos),
    )


class ConexaoDoProcesso:
    """A conexao de um processo (relay ou consumidor) com o broker.

    ``conectar`` abre conexao e canal com confirms, roda ``declarar`` (as
    declaracoes passivas do que o processo usa) e marca o processo pronto. Na
    queda, ``desconectar`` fecha e tira o pronto, e ``esperar`` segura o laco
    antes da proxima tentativa: um sorteio entre 0 e o atraso (*full jitter*:
    replicas que perderam o broker juntas nao voltam juntas), com o atraso
    dobrando de 1 s ate 30 s. Ele volta ao minimo com ``sucesso`` (mensagem
    tratada ou entregue) ou quando a conexao que caiu tinha durado um minuto:
    um canal que o broker fecha a cada mensagem nao vira laco de reconexao, e
    uma queda depois de horas estavel nao herda o atraso da anterior.
    """

    def __init__(
        self,
        parametros: pika.ConnectionParameters,
        *,
        processo: str,
        sinal: Sinalizador,
        declarar: Callable[[Any], None],  # recebe o BlockingChannel
    ) -> None:
        self._parametros = parametros
        self._processo = processo
        self._sinal = sinal
        self._declarar = declarar
        # BlockingConnection e BlockingChannel (pika sem tipos).
        self.conexao: Any = None
        self.canal: Any = None
        # Connection.Blocked (alarme de memoria ou disco do broker): quem publica
        # para de reivindicar trabalho ate o Connection.Unblocked.
        self.bloqueada = False
        self._atraso = RECONEXAO_BASE_S
        self._conectada_em: float | None = None

    def conectar(self) -> bool:
        """Conecta, declara e marca pronto; com o broker falhando, devolve False."""
        try:
            self.conexao, self.canal = conectar(self._parametros)
            self.bloqueada = False
            self.conexao.add_on_connection_blocked_callback(self._bloqueada)
            self.conexao.add_on_connection_unblocked_callback(self._desbloqueada)
            self._declarar(self.canal)
        except (*ERROS_DE_CONEXAO, ChannelClosedByBroker) as exc:
            _log.warning(
                "broker unavailable",
                erro=type(exc).__name__,
                codigo=getattr(exc, "reply_code", None),
            )
            self.desconectar()
            return False
        self._conectada_em = _relogio()
        self._sinal.marcar_pronto()
        _log.info("broker connected", processo=self._processo)
        return True

    def reabrir_canal(self) -> None:
        """Canal novo, com confirms, na mesma conexao (o broker fechou o anterior)."""
        self.canal = self.conexao.channel()
        self.canal.confirm_delivery()

    def desconectar(self) -> None:
        """Fecha a conexao, sem propagar erro, e tira o processo de pronto."""
        if self.conexao is not None:
            fechar(self.conexao)
        self.conexao = None
        self.canal = None
        self._sinal.marcar_nao_pronto()

    def esperar(self, parar: threading.Event) -> None:
        """Espera antes de reconectar (o ``parar`` interrompe) e dobra o atraso."""
        conectada_em, self._conectada_em = self._conectada_em, None
        if conectada_em is not None and _relogio() - conectada_em >= CONEXAO_ESTAVEL_S:
            self._atraso = RECONEXAO_BASE_S
        RECONEXOES.labels(processo=self._processo).inc()
        parar.wait(_sortear(self._atraso))
        self._atraso = min(self._atraso * 2, RECONEXAO_TETO_S)

    def atender(self) -> None:
        """Atende o broker (heartbeat, Connection.Blocked) sem esperar."""
        self.conexao.process_data_events(time_limit=0)

    def sucesso(self) -> None:
        """A conexao fez trabalho: a proxima queda recomeca do minimo."""
        self._atraso = RECONEXAO_BASE_S

    def _bloqueada(self, _conexao: Any, _metodo: Any) -> None:  # noqa: ANN401  # pika sem tipos
        self.bloqueada = True
        _log.warning(
            "broker blocked the connection (resource alarm)", processo=self._processo
        )

    def _desbloqueada(self, _conexao: Any, _metodo: Any) -> None:  # noqa: ANN401  # pika sem tipos
        self.bloqueada = False
        _log.info("broker unblocked the connection", processo=self._processo)


def _sortear(teto: float) -> float:
    """Espera sorteada entre 0 e ``teto`` (full jitter)."""
    return random.uniform(0, teto)  # noqa: S311  # nosec B311  # jitter, nao segredo


def _relogio() -> float:
    return time.monotonic()
