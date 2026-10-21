"""Value Objects com o resumo do orcamento e do pagamento guardado na OS.

O orcamento e o pagamento pertencem ao Billing; a OS guarda so o resumo que
chega pelos fatos da saga (``OrcamentoGerado``, ``PagamentoSolicitado`` e os
estados seguintes do pagamento), para a consulta de status nao depender de
outro servico (RFC-004 secao 7.1). O ciclo do resumo do pagamento (o primeiro
e o solicitado; os outros estados partem dele) fica nos metodos do VO e nas
funcoes do fim do modulo.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException
from src.compartilhado.dominio.value_object import ValueObject

# Tamanho da coluna; URLs do Billing/Mercado Pago ficam bem abaixo disso.
TAMANHO_MAXIMO_URL: Final = 2048


def _exigir_url_http(valor: object, rotulo: str) -> None:
    """URL absoluta http(s) que vai para o cliente (e-mail, tela).

    So ASCII visivel (sem espaco, quebra de linha ou NUL), host obrigatorio e
    sem userinfo: ``https://billing.local@evil.com/`` levaria o cliente a
    outro host. Nada de ``javascript:``/``data:``.
    """
    if not isinstance(valor, str) or not valor:
        msg = f"{rotulo} e obrigatorio"
        raise ValueError(msg)
    if len(valor) > TAMANHO_MAXIMO_URL:
        msg = f"{rotulo} excede {TAMANHO_MAXIMO_URL} caracteres"
        raise ValueError(msg)
    if not all("!" <= c <= "~" for c in valor):
        msg = f"{rotulo} tem caractere invalido"
        raise ValueError(msg)
    partes = urlsplit(valor)
    if (
        partes.scheme not in {"http", "https"}
        or not partes.hostname
        or partes.username is not None
        or partes.password is not None
    ):
        msg = f"{rotulo} deve ser uma URL http(s) absoluta, sem usuario"
        raise ValueError(msg)


def _exigir_tipo(valor: object, tipo: type, rotulo: str) -> None:
    """Guarda contra ``None`` (ou tipo errado) vindo de mensagem malformada."""
    if not isinstance(valor, tipo):
        msg = f"{rotulo} e obrigatorio"
        raise ValueError(msg)


def _exigir_instante(valor: object, rotulo: str) -> None:
    """Data e hora com fuso (UTC na saga): ``datetime`` ingenuo e ambiguo."""
    if not isinstance(valor, datetime) or valor.tzinfo is None:
        msg = f"{rotulo} deve ser data e hora com fuso horario"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ResumoOrcamento(ValueObject):
    """Orcamento gerado pelo Billing: id, total, link de decisao e validade."""

    orcamento_id: UUID
    total: Dinheiro
    # Link assinado: credencial de quem o tiver (decide o orcamento). Fora do
    # repr para nao vazar em log ou traceback.
    link_decisao: str = field(repr=False)
    valido_ate: datetime

    def __post_init__(self) -> None:
        _exigir_tipo(self.orcamento_id, UUID, "orcamento_id")
        _exigir_tipo(self.total, Dinheiro, "total do orcamento")
        _exigir_url_http(self.link_decisao, "link de decisao do orcamento")
        _exigir_instante(self.valido_ate, "validade do orcamento")


class StatusPagamento(StrEnum):
    """Estado do pagamento visto pela OS (espelha o Billing, que o decide)."""

    SOLICITADO = "solicitado"
    CONFIRMADO = "confirmado"
    RECUSADO = "recusado"
    EXPIRADO = "expirado"
    CANCELADO = "cancelado"
    ESTORNADO = "estornado"


@dataclass(frozen=True, slots=True)
class ResumoPagamento(ValueObject):
    """Pagamento solicitado ao Billing: id, estado, valor, checkout e expiracao."""

    pagamento_id: UUID
    status: StatusPagamento
    valor: Dinheiro
    # URL do checkout do cliente: fora do repr, como o link de decisao.
    checkout_url: str = field(repr=False)
    expira_em: datetime

    def __post_init__(self) -> None:
        _exigir_tipo(self.pagamento_id, UUID, "pagamento_id")
        _exigir_tipo(self.status, StatusPagamento, "status do pagamento")
        _exigir_tipo(self.valor, Dinheiro, "valor do pagamento")
        _exigir_url_http(self.checkout_url, "checkout_url do pagamento")
        _exigir_instante(self.expira_em, "expiracao do pagamento")

    def confirmado(self) -> ResumoPagamento:
        """O mesmo pagamento, confirmado pelo Billing."""
        return copy.replace(self, status=StatusPagamento.CONFIRMADO)

    def recusado(self) -> ResumoPagamento:
        """O mesmo pagamento, recusado: so o solicitado passa a recusado.

        Em outro estado o recusado e antigo (o estorno da aprovacao tardia pode
        chegar antes dele): o resumo segue como esta.
        """
        return self._se_solicitado(StatusPagamento.RECUSADO)

    def expirado(self) -> ResumoPagamento:
        """O mesmo pagamento, expirado: so o solicitado passa a expirado."""
        return self._se_solicitado(StatusPagamento.EXPIRADO)

    def cancelado(self) -> ResumoPagamento:
        """O mesmo pagamento, com a cobranca cancelada (``EstornarPagamento``).

        So a cobranca em aberto passa a cancelada. Em outro estado a resposta do
        Billing chegou depois de um estado mais novo (o estorno automatico da
        aprovacao tardia, ADR-040) ou e repetida: o resumo segue como esta, para
        a resposta ao comando concluir a compensacao em qualquer estado dele.
        """
        return self._se_solicitado(StatusPagamento.CANCELADO)

    def estornado(self) -> ResumoPagamento:
        """O mesmo pagamento, estornado: de qualquer estado, e idempotente.

        Serve ao estorno da compensacao (origem confirmado) e ao estorno tardio,
        que o Billing faz sozinho com a aprovacao que chega depois do
        encerramento (ADR-040).
        """
        return copy.replace(self, status=StatusPagamento.ESTORNADO)

    def _se_solicitado(self, novo: StatusPagamento) -> ResumoPagamento:
        if self.status is not StatusPagamento.SOLICITADO:
            return self
        return copy.replace(self, status=novo)


def pagamento_solicitado(
    atual: ResumoPagamento | None, novo: ResumoPagamento
) -> ResumoPagamento:
    """O resumo do ``PagamentoSolicitado``: o primeiro do pagamento, solicitado.

    Raises:
        ViolacaoRegraDeNegocioException: pagamento ja solicitado, ou ``novo``
            em outro estado.
    """
    if atual is not None:
        msg = "Pagamento ja solicitado para esta ordem"
        raise ViolacaoRegraDeNegocioException(msg)
    if novo.status is not StatusPagamento.SOLICITADO:
        msg = f"Resumo do pagamento solicitado em {novo.status.value}"
        raise ViolacaoRegraDeNegocioException(msg)
    return novo


def pagamento_exigido(atual: ResumoPagamento | None) -> ResumoPagamento:
    """O resumo do pagamento, de onde parte todo estado depois do solicitado.

    Raises:
        ViolacaoRegraDeNegocioException: pagamento ainda nao solicitado.
    """
    if atual is None:
        msg = "Ordem sem pagamento solicitado"
        raise ViolacaoRegraDeNegocioException(msg)
    return atual


def pagamento_no_estado(
    atual: ResumoPagamento | None, status: StatusPagamento
) -> ResumoPagamento:
    """O resumo no estado que o Billing informou depois do checkout.

    So os estados que nao mexem no status da OS: recusado, expirado, cancelado
    e estornado (o confirmado tem a transicao propria, ``pagamento_confirmado``).
    Um estado antigo que chega depois de um mais novo nao o desfaz.

    Raises:
        ViolacaoRegraDeNegocioException: pagamento ainda nao solicitado, ou
            estado que esta funcao nao aplica.
    """
    resumo = pagamento_exigido(atual)
    transicao = _TRANSICOES_DO_PAGAMENTO.get(status)
    if transicao is None:
        msg = f"Estado de pagamento {status.value} nao e aplicado aqui"
        raise ViolacaoRegraDeNegocioException(msg)
    return transicao(resumo)


def pagamento_confirmado(atual: ResumoPagamento | None) -> ResumoPagamento:
    """O resumo do ``PagamentoConfirmado``: o solicitado, agora confirmado.

    Raises:
        ViolacaoRegraDeNegocioException: pagamento ainda nao solicitado.
    """
    return pagamento_exigido(atual).confirmado()


_TRANSICOES_DO_PAGAMENTO: Final = MappingProxyType(
    {
        StatusPagamento.RECUSADO: ResumoPagamento.recusado,
        StatusPagamento.EXPIRADO: ResumoPagamento.expirado,
        StatusPagamento.CANCELADO: ResumoPagamento.cancelado,
        StatusPagamento.ESTORNADO: ResumoPagamento.estornado,
    }
)
