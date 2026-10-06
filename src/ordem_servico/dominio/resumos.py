"""Value Objects com o resumo do orcamento e do pagamento guardado na OS.

O orcamento e o pagamento pertencem ao Billing; a OS guarda so o resumo que
chega pelos fatos da saga, para a consulta de status nao depender de outro
servico (brief secao 5: consulta de status e leitura local).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from src.compartilhado.dominio.value_object import ValueObject

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro

# Tamanho da coluna; URLs do Billing/Mercado Pago ficam bem abaixo disso.
TAMANHO_MAXIMO_URL: Final = 2048


def _exigir_url_http(valor: str, rotulo: str) -> None:
    """URL absoluta http(s): o link vai para o cliente, nada de javascript:/data:."""
    partes = urlsplit(valor)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        msg = f"{rotulo} deve ser uma URL http(s) absoluta"
        raise ValueError(msg)
    if len(valor) > TAMANHO_MAXIMO_URL:
        msg = f"{rotulo} excede {TAMANHO_MAXIMO_URL} caracteres"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ResumoOrcamento(ValueObject):
    """Orcamento gerado pelo Billing: id, total e link de decisao do cliente."""

    orcamento_id: UUID
    total: Dinheiro
    link_decisao: str

    def __post_init__(self) -> None:
        _exigir_url_http(self.link_decisao, "link de decisao do orcamento")


class StatusPagamento(StrEnum):
    """Estado do pagamento visto pela OS.

    ponytail: so o estado que os fatos de dominio atuais produzem; confirmado,
    recusado, expirado e estornado entram com os handlers da saga.
    """

    SOLICITADO = "solicitado"


@dataclass(frozen=True, slots=True)
class ResumoPagamento(ValueObject):
    """Pagamento solicitado ao Billing: id, estado e URL do checkout."""

    pagamento_id: UUID
    status: StatusPagamento
    checkout_url: str

    def __post_init__(self) -> None:
        _exigir_url_http(self.checkout_url, "checkout_url do pagamento")
