"""Fabricas de ``OrdemDeServico`` para testes: sempre pelos metodos de dominio.

Nunca monta o agregado por campos privados: cada estado e alcancado pela
sequencia legal de fatos, entao a fabrica quebra se a maquina mudar.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem

LINK_DECISAO = "https://billing.pytstop.local/publico/orcamentos/tok123"
CHECKOUT_URL = "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=1"
TOTAL = Dinheiro(Decimal("350.00"))

# Ordem do fluxo feliz (brief secao 2) e o fato que leva a cada estado.
FLUXO = (
    StatusOrdem.RECEBIDA,
    StatusOrdem.EM_DIAGNOSTICO,
    StatusOrdem.AGUARDANDO_APROVACAO,
    StatusOrdem.AGUARDANDO_PAGAMENTO,
    StatusOrdem.AGUARDANDO_EXECUCAO,
    StatusOrdem.EM_EXECUCAO,
    StatusOrdem.FINALIZADA,
    StatusOrdem.ENTREGUE,
)


def abrir_ordem(
    *,
    cliente_id: UUID | None = None,
    veiculo_id: UUID | None = None,
    descricao_problema: str = "Barulho na suspensao dianteira",
) -> OrdemDeServico:
    return OrdemDeServico.abrir(
        cliente_id=cliente_id or uuid4(),
        veiculo_id=veiculo_id or uuid4(),
        descricao_problema=descricao_problema,
    )


def aplicar_fato(ordem: OrdemDeServico, para: StatusOrdem) -> None:
    """Aplica o fato que leva a ``para`` a partir do estado anterior do fluxo."""
    match para:
        case StatusOrdem.EM_DIAGNOSTICO:
            ordem.registrar_diagnostico_iniciado()
        case StatusOrdem.AGUARDANDO_APROVACAO:
            ordem.registrar_orcamento_gerado(
                orcamento_id=uuid4(), total=TOTAL, link_decisao=LINK_DECISAO
            )
        case StatusOrdem.AGUARDANDO_PAGAMENTO:
            ordem.registrar_pagamento_solicitado(
                pagamento_id=uuid4(), checkout_url=CHECKOUT_URL
            )
        case StatusOrdem.AGUARDANDO_EXECUCAO:
            ordem.registrar_aguardando_execucao()
        case StatusOrdem.EM_EXECUCAO:
            ordem.registrar_execucao_iniciada()
        case StatusOrdem.FINALIZADA:
            ordem.finalizar()
        case StatusOrdem.ENTREGUE:
            ordem.registrar_entrega()
        case _:
            msg = f"sem fato que leve a {para}"
            raise ValueError(msg)


def ordem_em(status: StatusOrdem, **kwargs: object) -> OrdemDeServico:
    """OS no ``status`` pedido (CANCELADA = cancelada logo apos a abertura)."""
    ordem = abrir_ordem(**kwargs)  # type: ignore[arg-type]
    if status is StatusOrdem.CANCELADA:
        ordem.cancelar("cliente desistiu", OrigemMudanca.ATENDIMENTO)
        return ordem
    for proximo in FLUXO[1 : FLUXO.index(status) + 1]:
        aplicar_fato(ordem, proximo)
    return ordem
