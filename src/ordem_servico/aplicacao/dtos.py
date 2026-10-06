"""DTOs da camada de aplicacao do contexto Ordem de Servico.

Contratos imutaveis de entrada e saida dos casos de uso: tipos primitivos,
``Decimal`` para dinheiro (nunca float) e tuplas. Nenhum VO de dominio vaza.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class AbrirOrdemDTO:
    """Comando de abertura da OS."""

    cliente_id: UUID
    veiculo_id: UUID
    # Texto livre: fora do repr (pode conter PII).
    descricao_problema: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MudancaDeStatusDTO:
    """Linha da linha do tempo de status (``de`` e ``None`` na abertura)."""

    sequencia: int
    de: str | None
    para: str
    origem: str
    motivo: str | None = field(repr=False)
    ocorrido_em: datetime


@dataclass(frozen=True, slots=True)
class ResumoOrcamentoDTO:
    """Resumo do orcamento do Billing guardado na OS."""

    orcamento_id: UUID
    total: Decimal
    moeda: str
    # Link assinado (credencial de quem o tiver): fora do repr.
    link_decisao: str = field(repr=False)
    valido_ate: datetime


@dataclass(frozen=True, slots=True)
class ResumoPagamentoDTO:
    """Resumo do pagamento do Billing guardado na OS."""

    pagamento_id: UUID
    status: str
    valor: Decimal
    moeda: str
    checkout_url: str = field(repr=False)
    expira_em: datetime


@dataclass(frozen=True, slots=True)
class OrdemDeServicoDTO:
    """Projecao completa da ordem (retorno dos casos de uso)."""

    id: UUID
    cliente_id: UUID
    veiculo_id: UUID
    descricao_problema: str = field(repr=False)
    status: str
    orcamento: ResumoOrcamentoDTO | None
    pagamento: ResumoPagamentoDTO | None
    motivo_cancelamento: str | None = field(repr=False)
    versao: int
    criado_em: datetime
    atualizado_em: datetime
    historico: tuple[MudancaDeStatusDTO, ...]


@dataclass(frozen=True, slots=True)
class OrdemResumoDTO:
    """Projecao enxuta para a listagem paginada."""

    id: UUID
    cliente_id: UUID
    veiculo_id: UUID
    status: str
    criado_em: datetime
    atualizado_em: datetime


@dataclass(frozen=True, slots=True)
class AcompanhamentoDTO:
    """Projecao publica: so status e timestamps (sem dado do cliente)."""

    status: str
    criado_em: datetime
    atualizado_em: datetime
