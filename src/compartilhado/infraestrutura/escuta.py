"""``LISTEN`` de um canal do PostgreSQL numa conexao dedicada, fora do pool.

O ``NOTIFY`` da gravacao acorda o processo na hora (a outbox acorda o relay, as
notificacoes acordam o notificador); o poll de seguranca cobre o que se perder.
Com o banco fora ou a conexao caida, o processo segue pelo poll e a conexao e
reaberta na espera seguinte.
"""

from __future__ import annotations

import contextlib
import select
from typing import TYPE_CHECKING, Any, Final

import structlog

from src.compartilhado.infraestrutura.database import tempo_de_conexao

if TYPE_CHECKING:
    import threading

    from sqlalchemy import Engine

_log = structlog.get_logger(__name__)

# Keepalives TCP da conexao dedicada de LISTEN: um peer que sumiu em silencio
# e detectado em cerca de 60 s, em vez de deixar o processo surdo ao NOTIFY.
_KEEPALIVES: Final = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


class EscutaDeCanal:
    """Conexao dedicada de ``LISTEN`` de um canal, fora do pool do engine."""

    def __init__(self, engine: Engine, canal: str) -> None:
        self._engine = engine
        self._canal = canal
        self._conexao: Any = None  # conexao psycopg2 (sem tipos)

    def esperar(self, parar: threading.Event, poll_s: float) -> None:
        """Espera um ``NOTIFY`` ou o poll de seguranca."""
        if self._conexao is None:
            self._conexao = self._abrir()
        if self._conexao is None:
            parar.wait(poll_s)
            return
        try:
            prontos, _, _ = select.select([self._conexao], [], [], poll_s)
            if prontos:
                self._conexao.poll()
                # Um drain cobre todas as notificacoes acumuladas.
                self._conexao.notifies.clear()
        except (self._engine.dialect.loaded_dbapi.Error, OSError):
            _log.warning("listen connection lost; polling until it is back")
            self.fechar()

    def fechar(self) -> None:
        """Fecha a conexao; best-effort, ela pode ja estar morta."""
        if self._conexao is not None:
            with contextlib.suppress(Exception):
                self._conexao.close()
        self._conexao = None

    def _abrir(self) -> Any:  # noqa: ANN401  # conexao psycopg2
        """Conexao de ``LISTEN`` com keepalives, ou None se o banco falhar."""
        try:
            argumentos, parametros = self._engine.dialect.create_connect_args(
                self._engine.url
            )
            parametros.update(_KEEPALIVES)
            # Sem o connect_timeout do engine, um banco inalcancavel prenderia o
            # laco no timeout de TCP do sistema e o heartbeat venceria.
            parametros.setdefault("connect_timeout", tempo_de_conexao())
            escuta = self._engine.dialect.loaded_dbapi.connect(
                *argumentos, **parametros
            )
            escuta.autocommit = True
            with escuta.cursor() as cursor:
                cursor.execute(f"LISTEN {self._canal}")
        except (self._engine.dialect.loaded_dbapi.Error, OSError):
            _log.warning("listen connection unavailable; polling")
            return None
        return escuta
