"""Portas de saida (Protocol) que a aplicacao OrdemDeServico consome.

Definidas no contexto consumidor e implementadas na infraestrutura:
``ClientePort`` em ``adapters.py`` (Anti-Corruption Layer: consulta o contexto
Cliente+Veiculo sem importar o agregado vizinho) e ``ConsultaAcompanhamento``
em ``consultas.py`` (query service de leitura, fora do repositorio do
agregado).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.dominio.documento import Documento
    from src.compartilhado.dominio.placa import Placa
    from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO


class ClientePort(Protocol):
    """Porta para validar cliente e veiculo no contexto Cliente+Veiculo."""

    # corpos `pass` (nao `...`) evitam o FP CodeQL py/ineffectual-statement
    def cliente_existe(self, cliente_id: UUID) -> bool:
        """Indica se o cliente existe e esta ativo."""
        pass

    def veiculo_pertence_ao_cliente(self, cliente_id: UUID, veiculo_id: UUID) -> bool:
        """Indica se o veiculo existe e pertence ao cliente informado."""
        pass


class ConsultaAcompanhamento(Protocol):
    """Query service do acompanhamento publico (projecao, nao o agregado)."""

    def mais_recente(
        self, placa: Placa, documento: Documento
    ) -> AcompanhamentoDTO | None:
        """Status e timestamps da OS mais recente do par, ou ``None``.

        Recebe os VOs ja validados: quem chama garante que documento e placa
        invalidos nunca chegam ao banco.
        """
        pass
