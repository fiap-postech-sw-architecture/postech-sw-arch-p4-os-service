from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from sqlalchemy import Connection, Engine
    from sqlalchemy.orm import Session

_session_factory: Callable[[], Session] | None = None
_engine_de_metricas: Engine | None = None


def configurar_session_factory(factory: Callable[[], Session]) -> None:
    """Registra a factory global de sessao SQLAlchemy usada por `obter_session`.

    Chamada uma vez durante o startup do app com a sessionmaker apropriada.
    """
    global _session_factory  # noqa: PLW0603  # DI singleton configurado no startup
    _session_factory = factory


def abrir_session() -> Session:
    """Nova sessao da factory configurada; quem abre fecha (``with``).

    Levanta RuntimeError se a factory nao foi configurada.
    """
    if _session_factory is None:
        msg = "Session factory nao configurada"
        raise RuntimeError(msg)
    return _session_factory()


def obter_session() -> Generator[Session]:
    """Dependency FastAPI que abre uma sessao por request e a fecha no teardown."""
    session = abrir_session()
    try:
        yield session
    finally:
        session.close()


def configurar_engine_de_metricas(engine: Engine) -> None:
    """Registra a engine das consultas do ``/metrics``, fora do pool das requisicoes."""
    global _engine_de_metricas  # noqa: PLW0603  # DI singleton configurado no startup
    _engine_de_metricas = engine


def abrir_conexao_de_metricas() -> Connection:
    """Conexao da engine das metricas; quem abre fecha (``with``).

    Levanta RuntimeError se a engine nao foi configurada (raspagem antes do boot).
    """
    if _engine_de_metricas is None:
        msg = "Engine de metricas nao configurada"
        raise RuntimeError(msg)
    return _engine_de_metricas.connect()
