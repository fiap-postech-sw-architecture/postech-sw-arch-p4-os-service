"""Schemas Pydantic (request/response) do router Ordem de Servico.

Contrato HTTP separado dos DTOs da aplicacao: o router traduz com
``model_validate(dto)`` (``from_attributes``). Requests com ``extra='forbid'``.
Dinheiro sai como string decimal (``"350.00"``) + ``moeda``, o mesmo formato
das mensagens da saga (RFC-004 secao 5.3).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from src.ordem_servico.dominio.ordem_de_servico import (
    TAMANHO_MAXIMO_DESCRICAO,
    TAMANHO_MAXIMO_MOTIVO,
)
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.interfaces.situacoes import situacao_de

_DESCRICAO_STATUS = (
    "Status tecnico (snake_case): recebida, em_diagnostico, aguardando_aprovacao, "
    "aguardando_pagamento, aguardando_execucao, em_execucao, finalizada, "
    "entregue ou cancelada."
)


class _ComSituacao(BaseModel):
    """Base dos responses que derivam o rotulo ``situacao`` de ``status``."""

    status: str = Field(description=_DESCRICAO_STATUS)

    @computed_field(  # type: ignore[prop-decorator]
        description="Rotulo de apresentacao do status (ex.: 'Em diagnóstico')."
    )
    @property
    def situacao(self) -> str:
        return situacao_de(StatusOrdem(self.status))


class AbrirOrdemRequest(BaseModel):
    """Corpo de ``POST /api/v1/ordens-de-servico``."""

    model_config = ConfigDict(extra="forbid")

    cliente_id: UUID = Field(description="Cliente dono do veiculo.")
    veiculo_id: UUID = Field(description="Veiculo a ser atendido.")
    descricao_problema: str = Field(
        min_length=1,
        max_length=TAMANHO_MAXIMO_DESCRICAO,
        description="Problema relatado pelo cliente na recepcao.",
    )


class CancelarOrdemRequest(BaseModel):
    """Corpo de ``POST /api/v1/ordens-de-servico/{id}/cancelamento``."""

    model_config = ConfigDict(extra="forbid")

    motivo: str = Field(
        min_length=1,
        max_length=TAMANHO_MAXIMO_MOTIVO,
        description="Motivo do cancelamento (obrigatorio).",
    )


class ResumoOrcamentoResponse(BaseModel):
    """Resumo do orcamento gerado pelo Billing (copiado do ``OrcamentoGerado``)."""

    model_config = ConfigDict(from_attributes=True)

    orcamento_id: UUID
    total: Decimal = Field(description="Total em string decimal, ex.: '350.00'.")
    moeda: str = Field(description="ISO 4217, ex.: 'BRL'.")
    link_decisao: str = Field(description="Link assinado para o cliente decidir.")
    valido_ate: datetime = Field(description="Fim da validade do orcamento (UTC).")


class ResumoPagamentoResponse(BaseModel):
    """Resumo do pagamento no Billing (``PagamentoSolicitado`` e seguintes)."""

    model_config = ConfigDict(from_attributes=True)

    pagamento_id: UUID
    status: str = Field(
        description=(
            "solicitado, confirmado, recusado, expirado, cancelado ou estornado."
        )
    )
    valor: Decimal = Field(description="Valor em string decimal, ex.: '350.00'.")
    moeda: str = Field(description="ISO 4217, ex.: 'BRL'.")
    checkout_url: str = Field(description="URL do checkout (Mercado Pago).")
    expira_em: datetime = Field(description="Expiracao do checkout (UTC).")


class OrdemDeServicoResponse(_ComSituacao):
    """Projecao da ordem (sem o historico, que tem rota propria)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    cliente_id: UUID
    veiculo_id: UUID
    descricao_problema: str
    orcamento: ResumoOrcamentoResponse | None = Field(
        description="Resumo do orcamento gerado pelo Billing (None antes dele)."
    )
    pagamento: ResumoPagamentoResponse | None = Field(
        description="Resumo do pagamento solicitado ao Billing (None antes dele)."
    )
    motivo_cancelamento: str | None
    versao: int = Field(description="Versao do lock otimista (muda a cada escrita).")
    criado_em: datetime
    atualizado_em: datetime


class OrdemResumoResponse(_ComSituacao):
    """Item da fila de ordens: ids, status e timestamps, sem o detalhe da ordem."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    cliente_id: UUID
    veiculo_id: UUID
    criado_em: datetime
    atualizado_em: datetime


class OrdemListaResponse(BaseModel):
    """Pagina da fila de ordens, com o total do universo filtrado."""

    items: list[OrdemResumoResponse]
    total: int = Field(description="Total do universo listado (acompanha o filtro).")
    offset: int
    limit: int


class MudancaDeStatusResponse(BaseModel):
    """Uma linha do historico: a transicao de status, quem a originou e quando."""

    model_config = ConfigDict(from_attributes=True)

    sequencia: int
    de: str | None = Field(description="Status anterior (None na abertura).")
    para: str
    origem: str = Field(description="atendimento, execucao, billing ou saga.")
    motivo: str | None
    ator: str | None = Field(
        description=(
            "Quem provocou a mudanca: o sub do JWT do usuario ou o processo "
            "(consumidor, prazos)."
        )
    )
    ocorrido_em: datetime


class HistoricoResponse(BaseModel):
    """Linha do tempo da ordem, da abertura ate a ultima mudanca de status."""

    ordem_id: UUID
    mudancas: list[MudancaDeStatusResponse]


class AcompanhamentoRequest(BaseModel):
    """Corpo da consulta publica (placa e documento sao PII: no corpo, nao na URL)."""

    model_config = ConfigDict(extra="forbid")

    placa: str = Field(
        min_length=7,
        max_length=8,
        description="Placa do veiculo (7 caracteres, ou 8 com hifen).",
    )
    documento: str = Field(
        min_length=11,
        max_length=18,
        description="CPF (11 digitos) ou CNPJ (14 digitos), com ou sem mascara.",
    )


class AcompanhamentoResponse(_ComSituacao):
    """Projecao publica: status e timestamps, nada do cliente (LGPD)."""

    model_config = ConfigDict(from_attributes=True)

    criado_em: datetime
    atualizado_em: datetime
