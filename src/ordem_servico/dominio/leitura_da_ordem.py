"""Leitura do estado da OS: as propriedades de consulta do agregado.

Ficam fora da classe ``OrdemDeServico`` para ela ter so o comportamento (os
fatos e as regras): nenhuma muda estado. Os campos ficam na ``OrdemDeServico``,
que os persiste; aqui so se declaram, para a leitura ser tipada.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.ordem_servico.dominio.historico import MudancaDeStatus
    from src.ordem_servico.dominio.resumos import ResumoOrcamento, ResumoPagamento
    from src.ordem_servico.dominio.status import StatusOrdem


class LeituraDaOrdem:
    """Propriedades de leitura da ``OrdemDeServico`` sobre os campos dela."""

    _cliente_id: UUID
    _veiculo_id: UUID
    _descricao_problema: str
    _status: StatusOrdem
    _historico: list[MudancaDeStatus]
    _resumo_orcamento: ResumoOrcamento | None
    _resumo_pagamento: ResumoPagamento | None
    _motivo_cancelamento: str | None
    _versao: int
    _criado_em: datetime
    _atualizado_em: datetime

    @property
    def cliente_id(self) -> UUID:
        """Cliente dono do veiculo atendido."""
        return self._cliente_id

    @property
    def veiculo_id(self) -> UUID:
        """Veiculo recebido para o servico."""
        return self._veiculo_id

    @property
    def descricao_problema(self) -> str:
        """Problema relatado no atendimento: texto livre ja aparado e validado."""
        return self._descricao_problema

    @property
    def status(self) -> StatusOrdem:
        """Status atual; so muda pelos fatos de dominio, que validam a maquina."""
        return self._status

    @property
    def historico(self) -> tuple[MudancaDeStatus, ...]:
        """Linha do tempo das mudancas de status, da abertura ate agora."""
        return tuple(self._historico)

    @property
    def resumo_orcamento(self) -> ResumoOrcamento | None:
        """Resumo do orcamento do Billing; ``None`` ate o ``OrcamentoGerado``."""
        return self._resumo_orcamento

    @property
    def resumo_pagamento(self) -> ResumoPagamento | None:
        """Resumo do pagamento do Billing; ``None`` ate o ``PagamentoSolicitado``."""
        return self._resumo_pagamento

    @property
    def motivo_cancelamento(self) -> str | None:
        """Texto do pedido de cancelamento (o motivo da OS cancelada) ou ``None``."""
        return self._motivo_cancelamento

    @property
    def versao(self) -> int:
        """Versao do lock otimista: comeca em 1 e sobe a cada gravacao."""
        return self._versao

    @property
    def criado_em(self) -> datetime:
        """Instante da abertura da ordem (UTC)."""
        return self._criado_em

    @property
    def atualizado_em(self) -> datetime:
        """Instante da ultima alteracao do agregado (UTC)."""
        return self._atualizado_em
