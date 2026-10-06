"""Portas de saida (Protocol) que a aplicacao OrdemDeServico consome.

Definidas no contexto consumidor e implementadas em
``infraestrutura/adapters.py`` (Anti-Corruption Layer): o adapter consulta o
contexto Cliente+Veiculo sem que este contexto importe o agregado vizinho.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID


class ClientePort(Protocol):
    """Porta para validar cliente e veiculo no contexto Cliente+Veiculo."""

    # corpos `pass` (nao `...`) evitam o FP CodeQL py/ineffectual-statement
    def cliente_existe(self, cliente_id: UUID) -> bool:
        """Indica se o cliente existe e esta ativo."""
        pass

    def veiculo_pertence_ao_cliente(self, cliente_id: UUID, veiculo_id: UUID) -> bool:
        """Indica se o veiculo existe e pertence ao cliente informado."""
        pass
