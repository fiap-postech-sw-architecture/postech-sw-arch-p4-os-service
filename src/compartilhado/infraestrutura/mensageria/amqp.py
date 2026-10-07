"""Conexao e propriedades AMQP (pika) do relay e do consumidor (ADR-036)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Final

import pika
from pika.exceptions import AMQPConnectionError, ChannelWrongStateError

if TYPE_CHECKING:
    from collections.abc import Mapping

# Queda do broker (e nao falha de uma mensagem): conexao recusada ou perdida,
# heartbeat vencido, canal usado depois que a conexao caiu.
ERROS_DE_CONEXAO: Final[tuple[type[Exception], ...]] = (
    AMQPConnectionError,
    ChannelWrongStateError,
    OSError,
)

# Heartbeat curto o bastante para notar o broker fora em menos de um minuto; o
# relay atende o heartbeat a cada volta do laco (poll de 5 s por padrao).
_HEARTBEAT_S: Final = 30
# Broker com alarme de memoria bloqueia a conexao: depois disso, ela cai e o
# processo reconecta, em vez de ficar parado num publish.
_BLOQUEIO_MAXIMO_S: Final = 30
_TIMEOUT_DE_SOCKET_S: Final = 5


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
    """Abre a conexao e um canal com publisher confirms."""
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
