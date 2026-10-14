"""Linhas da compensacao (RFC-004 secoes 4.4 e 4.7): a falha de negocio e as respostas.

A falha de negocio inicia a compensacao pela primeira do plano; a resposta da
compensacao pendente envia a seguinte ou, na ultima, cancela a OS. Como as
linhas do fluxo (``linhas_do_fluxo``), aplicam o fato na OS e devolvem o
comando; a ``Saga`` confere antes de mudar.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

from src.ordem_servico.aplicacao.saga.compensacao import (
    MOTIVO_DA_FALHA,
    RESPOSTA_DA_COMPENSACAO,
    ROTULO_DO_MOTIVO,
    dados_da_compensacao,
    inicio_da_compensacao,
    resposta_da_compensacao,
)
from src.ordem_servico.aplicacao.saga.modelo import (
    ATOR_CONSUMIDOR,
    EtapaSaga,
    GatilhoDeProcesso,
    MotivoCompensacao,
)
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.resumos import StatusPagamento

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.linhas_do_fluxo import Linha, Pedido
    from src.ordem_servico.aplicacao.saga.modelo import Transicao
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

# O estado que cada evento do Billing deixa no resumo do pagamento: as falhas de
# negocio dele e as respostas ao EstornarPagamento (a falha do estorno nao muda).
ESTADO_DO_PAGAMENTO: Final[Mapping[str, StatusPagamento]] = MappingProxyType(
    {
        "PagamentoRecusado": StatusPagamento.RECUSADO,
        "PagamentoExpirado": StatusPagamento.EXPIRADO,
        "PagamentoCancelado": StatusPagamento.CANCELADO,
        "PagamentoEstornado": StatusPagamento.ESTORNADO,
    }
)


def _compensacao(transicao: Transicao, ordem: OrdemDeServico) -> Pedido:
    """A compensacao que a ``transicao`` envia, com os ``dados`` dela."""
    if transicao.comando is None:
        return None
    marcos = MarcosDaOrdem.da_ordem(ordem)
    dados = dados_da_compensacao(transicao.comando, ordem.id, transicao.motivo, marcos)
    return transicao.comando, dados


def _ator_do_cancelamento(saga: Saga) -> str:
    """Quem cancelou a OS: o atendente que pediu (o ``sub`` do registro do pedido).

    Sem o pedido de um atendente (as falhas de negocio), a decisao e do processo
    (ADR-039: o historico da OS guarda o ``sub`` de quem agiu).
    """
    pedido = GatilhoDeProcesso.CANCELAMENTO_SOLICITADO.value
    return next(
        (p["ator"] for p in saga.passos if p["gatilho"] == pedido), ATOR_CONSUMIDOR
    )


def _falha_de_negocio(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    """A compensacao comeca pela primeira do plano (RFC-004 secao 4.4).

    O pagamento recusado ou expirado tambem passa ao resumo da OS, que a falha
    exige (o checkout aberto).
    """
    estado = ESTADO_DO_PAGAMENTO.get(evento.tipo)
    if estado is not None:
        ordem.registrar_estado_do_pagamento(estado, ator=ATOR_CONSUMIDOR)
    transicao = inicio_da_compensacao(
        saga.passos_concluidos,
        saga.tipo_do_comando_em_voo,
        MOTIVO_DA_FALHA[evento.tipo],
    )
    return _compensacao(transicao, ordem)


def _resposta_da_compensacao(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    """A seguinte do plano sai; depois da ultima, a OS e cancelada.

    O cancelamento e o estorno do pagamento passam ao resumo da OS, quando ela
    o tem, e nunca recusam a resposta casada: se o estorno automatico chegou
    antes, o resumo segue estornado e a compensacao avanca (ADR-040). A OS
    cancelada leva o texto do pedido do atendente ou o rotulo do motivo
    (RFC-004 secao 4.4).
    """
    estado = ESTADO_DO_PAGAMENTO.get(evento.tipo)
    if estado is not None and ordem.resumo_pagamento is not None:
        ordem.registrar_estado_do_pagamento(estado, ator=ATOR_CONSUMIDOR)
    transicao = resposta_da_compensacao(
        evento.tipo, saga.plano_compensacao, saga.motivo
    )
    if transicao.seguinte is EtapaSaga.COMPENSADA:
        # Em compensacao a saga sempre tem o motivo (``conferir_estado``).
        rotulo = ROTULO_DO_MOTIVO[MotivoCompensacao(cast("str", saga.motivo))]
        motivo = ordem.motivo_cancelamento or rotulo
        ordem.cancelar(motivo, OrigemMudanca.SAGA, ator=_ator_do_cancelamento(saga))
    return _compensacao(transicao, ordem)


LINHAS_DA_COMPENSACAO: Final[Mapping[str, Linha]] = MappingProxyType(
    {
        **dict.fromkeys(MOTIVO_DA_FALHA, _falha_de_negocio),
        **dict.fromkeys(RESPOSTA_DA_COMPENSACAO, _resposta_da_compensacao),
    }
)
