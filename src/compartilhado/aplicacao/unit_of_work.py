from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType
    from uuid import UUID


class UnitOfWork(Protocol):
    # corpos `pass` (nao `...`) evitam o FP CodeQL py/ineffectual-statement
    def __enter__(self) -> Self:
        """Abre a unidade de trabalho (nova sessao) e devolve a si mesma."""
        pass

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Faz rollback se a transacao terminou em excecao e fecha a sessao."""
        pass

    def commit(self) -> None:
        """Comita a transacao, com os comandos publicados nela."""
        pass

    def rollback(self) -> None:
        """Desfaz as alteracoes pendentes da transacao atual."""
        pass

    def publicar_comando(
        self,
        tipo: str,
        dados: Mapping[str, Any],
        *,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> UUID:
        """Grava o comando na outbox, na transacao desta unidade de trabalho.

        O envelope (RFC-004 secao 5.2) e validado contra o contrato na hora:
        ``tipo`` que o OS nao publica ou ``dados`` fora do schema sao bug e
        levantam ``ContratoInvalidoError``. O relay publica depois do commit.

        Args:
            tipo: nome do comando no catalogo (ex.: ``SolicitarDiagnostico``).
            dados: campo ``dados`` do envelope; UUID e Enum viram texto.
            correlation_id: id da OS (o da saga); fora da saga, o do agregado
                tratado (``AnonimizarVeiculo``: o ``veiculo_id``).
            causation_id: id da mensagem que causou o comando; ``None`` quando
                a causa e uma requisicao HTTP ou um prazo.

        Returns:
            O ``id`` do envelope, tambem a chave de idempotencia do destino.
        """
        pass
