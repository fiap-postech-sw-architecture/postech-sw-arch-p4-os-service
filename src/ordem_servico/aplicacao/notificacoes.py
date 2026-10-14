"""Quando o cliente recebe e-mail e de qual modelo (RFC-004 secoes 3, 4.8 e 7.2).

Cada mudanca de status da OS pede um e-mail, no modelo do novo status, menos a
abertura (``recebida``) e o ``aguardando_pagamento``: o e-mail do checkout vem
em seguida, quando o Billing informa o link (``PagamentoSolicitado``), e esse
fato, que nao muda o status, tem o modelo proprio ``pagamento``. O pedido e
gravado na transacao da transicao (``PublicadorDeNotificacoes``) e entregue
depois, pelo processo ``notificador``: SMTP lento ou fora nao atrasa a saga.

O pedido guarda so ``{ordem_id, modelo, status, motivo}``: o destinatario, o
nome e os links saem da OS e do cadastro na hora da entrega (``emails``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.ordem_servico.dominio.status import StatusOrdem

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from src.ordem_servico.aplicacao.ports import PublicadorDeNotificacoes
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


class ModeloDeNotificacao(StrEnum):
    """O texto do e-mail; o valor e a coluna ``notificacoes.modelo``."""

    SITUACAO = "situacao"
    ORCAMENTO = "orcamento"
    PAGAMENTO = "pagamento"
    CANCELAMENTO = "cancelamento"


@dataclass(frozen=True, slots=True)
class Notificacao:
    """Um e-mail a enviar: sem dado pessoal nem token.

    ``motivo`` e o codigo da compensacao do modelo ``cancelamento``, nunca o
    texto do atendente.
    """

    ordem_id: UUID
    modelo: ModeloDeNotificacao
    status: StatusOrdem
    motivo: str | None = None


_M = ModeloDeNotificacao

# O modelo do e-mail de cada status em que a OS entra; ``None`` = sem e-mail.
# Todo status esta aqui (o teste confere): um status novo pede a decisao.
MODELO_DO_STATUS: Final[Mapping[StatusOrdem, ModeloDeNotificacao | None]] = (
    MappingProxyType(
        {
            StatusOrdem.RECEBIDA: None,
            StatusOrdem.EM_DIAGNOSTICO: _M.SITUACAO,
            StatusOrdem.AGUARDANDO_APROVACAO: _M.ORCAMENTO,
            StatusOrdem.AGUARDANDO_PAGAMENTO: None,
            StatusOrdem.AGUARDANDO_EXECUCAO: _M.SITUACAO,
            StatusOrdem.EM_EXECUCAO: _M.SITUACAO,
            StatusOrdem.FINALIZADA: _M.SITUACAO,
            StatusOrdem.ENTREGUE: _M.SITUACAO,
            StatusOrdem.CANCELADA: _M.CANCELAMENTO,
        }
    )
)


def registrar_transicao(
    publicador: PublicadorDeNotificacoes,
    ordem: OrdemDeServico,
    *,
    status_antes: StatusOrdem,
    abriu_o_checkout: bool = False,
    motivo: str | None = None,
) -> None:
    """Grava o e-mail da transicao que a ``ordem`` acabou de fazer, se ela pede um.

    ``status_antes`` e o status de antes do fato; ``abriu_o_checkout`` diz que o
    fato foi o ``PagamentoSolicitado`` (muda o resumo do pagamento, nao o
    status). ``motivo`` e o da compensacao que cancelou a OS.
    """
    if abriu_o_checkout:
        modelo: ModeloDeNotificacao | None = _M.PAGAMENTO
    elif ordem.status is status_antes:
        return
    else:
        modelo = MODELO_DO_STATUS[ordem.status]
    if modelo is None:
        return
    publicador.registrar(
        Notificacao(
            ordem_id=ordem.id,
            modelo=modelo,
            status=ordem.status,
            motivo=motivo if modelo is _M.CANCELAMENTO else None,
        )
    )
