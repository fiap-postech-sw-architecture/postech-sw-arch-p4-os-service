"""Portas de saida (Protocol) que a aplicacao OrdemDeServico consome.

Definidas no contexto consumidor e implementadas na infraestrutura:
``ClientePort`` em ``adapters.py`` (Anti-Corruption Layer: consulta o contexto
Cliente+Veiculo sem importar o agregado vizinho), ``ConsultaAcompanhamento`` e
``ConsultaDaOrdem`` em ``consultas.py`` (query services de leitura, fora do
repositorio do agregado) e ``SagaRepository`` em ``repository.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.dominio.documento import Documento
    from src.compartilhado.dominio.placa import Placa
    from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO, RetratoDoVeiculo
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


class ClientePort(Protocol):
    """Porta para validar cliente e veiculo no contexto Cliente+Veiculo."""

    # corpos `pass` (nao `...`) evitam o FP CodeQL py/ineffectual-statement
    def cliente_existe(self, cliente_id: UUID) -> bool:
        """Indica se o cliente existe e esta ativo."""
        pass

    def retrato_do_veiculo(
        self, cliente_id: UUID, veiculo_id: UUID
    ) -> RetratoDoVeiculo | None:
        """Placa, marca, modelo e ano do veiculo do cliente.

        ``None`` se o veiculo nao existe ou e de outro cliente (casos que a
        abertura nao distingue).
        """
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


class ConsultaDaOrdem(Protocol):
    """Leitura da OS com a saga dela numa consulta so (RFC-004 secao 4).

    O status e a etapa saem do mesmo instante: um commit do consumidor entre
    duas leituras mostraria o status velho com a etapa nova.
    """

    def com_saga(self, ordem_id: UUID) -> tuple[OrdemDeServico, Saga | None] | None:
        """A OS e a saga (``None`` sem saga), ou ``None`` sem a OS."""
        pass


class SagaRepository(Protocol):
    """Persistencia da saga (agregado proprio, RFC-004 secao 4), sob a transacao."""

    def obter(self, ordem_id: UUID) -> Saga | None:
        """A saga da OS ``ordem_id``, ou ``None``."""
        pass

    def salvar(self, saga: Saga) -> None:
        """Persiste a saga com lock otimista pela ``versao``.

        Raises:
            ConflitoDeConcorrenciaException: outra transacao gravou a mesma
                saga desde a leitura.
        """
        pass
