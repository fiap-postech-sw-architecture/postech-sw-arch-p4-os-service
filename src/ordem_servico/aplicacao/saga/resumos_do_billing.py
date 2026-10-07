"""Resumos do orcamento e do pagamento a partir dos eventos do Billing.

O OS guarda so o resumo do que vive no Billing (RFC-004 secao 7.1). Os campos
vem do contrato (RFC-004 secao 5.3), ja validados pelo schema no consumidor;
os VOs conferem o resto (URL http(s) sem usuario, instante com fuso).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.ordem_servico.dominio.resumos import (
    ResumoOrcamento,
    ResumoPagamento,
    StatusPagamento,
)

if TYPE_CHECKING:
    from src.ordem_servico.aplicacao.saga.modelo import DadosDoContrato


def resumo_do_orcamento(dados: DadosDoContrato) -> ResumoOrcamento:
    """Resumo do ``OrcamentoGerado``: id, total, link de decisao e validade."""
    return ResumoOrcamento(
        orcamento_id=UUID(dados["orcamento_id"]),
        total=Dinheiro(valor=Decimal(dados["total"]), moeda=dados["moeda"]),
        link_decisao=dados["link_decisao"],
        valido_ate=datetime.fromisoformat(dados["valido_ate"]),
    )


def resumo_do_pagamento(dados: DadosDoContrato) -> ResumoPagamento:
    """Resumo do ``PagamentoSolicitado``: id, valor, checkout e expiracao."""
    return ResumoPagamento(
        pagamento_id=UUID(dados["pagamento_id"]),
        status=StatusPagamento.SOLICITADO,
        valor=Dinheiro(valor=Decimal(dados["valor"]), moeda=dados["moeda"]),
        checkout_url=dados["checkout_url"],
        expira_em=datetime.fromisoformat(dados["expira_em"]),
    )
