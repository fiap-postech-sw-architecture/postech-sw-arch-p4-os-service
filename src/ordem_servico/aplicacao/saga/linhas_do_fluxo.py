"""Uma linha da tabela da RFC-004 secao 4.1 por evento do fluxo normal.

Cada linha aplica o fato na OS e devolve o comando que ela envia (ou ``None``);
quem grava o comando na outbox e o ``OrquestradorDaSaga``, e a ``Saga`` confere
o tipo, os dados e o prazo antes de mudar. O ``ExecucaoIniciada`` tambem e a
linha da corrida do pivot, com a compensacao em curso ou parada.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.modelo import (
    ATOR_CONSUMIDOR,
    itens_do_diagnostico,
    pecas_dos_itens,
)
from src.ordem_servico.aplicacao.saga.resumos_do_billing import (
    resumo_do_orcamento,
    resumo_do_pagamento,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import PRIORIDADE_DO_AGENDAMENTO
from src.ordem_servico.dominio.resumos import StatusPagamento

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.modelo import DadosDoContrato
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
    from src.ordem_servico.dominio.resumos import ResumoOrcamento

# O comando que a linha envia: tipo e ``dados`` (sem ele, ``None``).
type Pedido = tuple[Comando, DadosDoContrato] | None
type Linha = Callable[[MensagemRecebida, Saga, OrdemDeServico], Pedido]


def _diagnostico_iniciado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    ordem.registrar_diagnostico_iniciado(ator=ATOR_CONSUMIDOR)
    return None


def _diagnostico_concluido(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    itens = itens_do_diagnostico(evento.dados)
    return Comando.GERAR_ORCAMENTO, {"ordem_id": str(saga.ordem_id), "itens": itens}


def _orcamento_gerado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    ordem.registrar_orcamento_gerado(
        resumo_do_orcamento(evento.dados), ator=ATOR_CONSUMIDOR
    )
    return None


def _orcamento_aprovado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    pecas = pecas_dos_itens(saga.itens)
    return Comando.RESERVAR_PECAS, {"ordem_id": str(saga.ordem_id), "pecas": pecas}


def _pecas_reservadas(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    orcamento = ordem.resumo_orcamento
    # A OS recusa o fato sem o resumo do orcamento, antes de mudar.
    ordem.registrar_pecas_reservadas(ator=ATOR_CONSUMIDOR)
    orcamento_id = cast("ResumoOrcamento", orcamento).orcamento_id
    return Comando.SOLICITAR_PAGAMENTO, {
        "ordem_id": str(saga.ordem_id),
        "orcamento_id": str(orcamento_id),
    }


def _pagamento_solicitado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    ordem.registrar_estado_do_pagamento(
        StatusPagamento.SOLICITADO,
        ator=ATOR_CONSUMIDOR,
        resumo=resumo_do_pagamento(evento.dados),
    )
    return None


def _pagamento_confirmado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    ordem.registrar_estado_do_pagamento(
        StatusPagamento.CONFIRMADO, ator=ATOR_CONSUMIDOR
    )
    return Comando.AGENDAR_EXECUCAO, {
        "ordem_id": str(saga.ordem_id),
        "prioridade": PRIORIDADE_DO_AGENDAMENTO,
    }


def _execucao_agendada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    return None


def _execucao_iniciada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    # No fluxo e na corrida do pivot: a OS descarta o pedido de cancelamento.
    ordem.registrar_execucao_iniciada(ator=ATOR_CONSUMIDOR)
    return None


def _execucao_finalizada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> Pedido:
    ordem.finalizar(ator=ATOR_CONSUMIDOR)
    return None


LINHAS_DO_FLUXO: Final[Mapping[str, Linha]] = MappingProxyType(
    {
        "DiagnosticoIniciado": _diagnostico_iniciado,
        "DiagnosticoConcluido": _diagnostico_concluido,
        "OrcamentoGerado": _orcamento_gerado,
        "OrcamentoAprovado": _orcamento_aprovado,
        "PecasReservadas": _pecas_reservadas,
        "PagamentoSolicitado": _pagamento_solicitado,
        "PagamentoConfirmado": _pagamento_confirmado,
        "ExecucaoAgendada": _execucao_agendada,
        "ExecucaoIniciada": _execucao_iniciada,
        "ExecucaoFinalizada": _execucao_finalizada,
    }
)
