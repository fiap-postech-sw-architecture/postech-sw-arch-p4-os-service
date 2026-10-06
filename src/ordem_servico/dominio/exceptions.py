"""Excecoes de dominio do contexto Ordem de Servico.

Os ids na mensagem sao UUIDs (nao PII) e ajudam o diagnostico em log e
resposta.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import EntidadeNaoEncontradaException

if TYPE_CHECKING:
    from uuid import UUID


class OrdemNaoEncontradaException(EntidadeNaoEncontradaException):
    """A ``OrdemDeServico`` pedida nao existe no repositorio."""

    def __init__(self, ordem_id: UUID) -> None:
        super().__init__(mensagem=f"Ordem de servico {ordem_id} nao encontrada")


class ClienteNaoEncontradoException(EntidadeNaoEncontradaException):
    """Cliente referenciado na abertura da OS nao existe ou esta inativo."""

    def __init__(self, cliente_id: UUID) -> None:
        super().__init__(mensagem=f"Cliente {cliente_id} nao encontrado")


class VeiculoNaoEncontradoException(EntidadeNaoEncontradaException):
    """Veiculo referenciado na OS nao existe ou nao pertence ao cliente.

    Os dois casos sao indistinguiveis na resposta (defesa em profundidade,
    ver ``AbrirOrdem``): a mensagem nao revela qual deles ocorreu.
    """

    def __init__(self, veiculo_id: UUID) -> None:
        super().__init__(
            mensagem=f"Veiculo {veiculo_id} nao encontrado para o cliente informado"
        )
