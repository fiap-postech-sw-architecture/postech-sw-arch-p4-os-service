from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from src.compartilhado.infraestrutura.outbox_mapping import gravar_comando

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import TracebackType
    from uuid import UUID

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.mensageria import Comando


class SQLAlchemyUnitOfWork:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            msg = "UnitOfWork nao foi iniciado. Use 'with' para iniciar."
            raise RuntimeError(msg)
        return self._session

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollback()
        self._fechar_sessao()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def publicar_comando(
        self,
        tipo: Comando,
        dados: Mapping[str, Any],
        *,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> UUID:
        """Grava o comando na outbox desta transacao (ver ``PublicadorDeComandos``)."""
        return gravar_comando(
            self.session,
            tipo,
            dados,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def _fechar_sessao(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


class TransacaoDaMensagem:
    """A unidade de trabalho que o consumidor entrega ao handler de uma mensagem.

    Presa a transacao da mensagem: o handler monta os repositorios sobre
    ``session``, grava o efeito e publica os comandos (``publicar_comando``)
    nela, e o consumidor comita tudo uma vez, junto com
    ``mensagens_processadas``. Nao ha ``commit`` aqui, e o consumidor recusa o
    commit e o fim da transacao pela ``session`` enquanto o handler roda.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def publicar_comando(
        self,
        tipo: Comando,
        dados: Mapping[str, Any],
        *,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> UUID:
        """Grava o comando na outbox da transacao da mensagem."""
        return gravar_comando(
            self.session,
            tipo,
            dados,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
