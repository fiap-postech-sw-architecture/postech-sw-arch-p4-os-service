"""Tabelas da saga (RFC-004 secao 4.1): ordem das etapas, etapa esperada e linhas.

A ordem das etapas, a etapa em que cada evento e esperado, a linha de cada
evento do fluxo normal (etapa seguinte, comando enviado e passo concluido) e os
comandos com prazo. So dados: a classificacao do evento recebido esta em
``classificacao``, e as transicoes e conferencias que a ``Saga`` faz antes de
mudar, em ``transicoes``; as da compensacao vem de ``compensacao``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.compensacao import ORDEM_DAS_COMPENSACOES
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga, Transicao

if TYPE_CHECKING:
    from collections.abc import Mapping

_E = EtapaSaga

# Ordem linear do fluxo normal; compensando, compensada e falha_na_compensacao
# ficam fora dela (RFC-004 secao 4.1).
FLUXO_DA_SAGA: Final = (
    _E.AGUARDANDO_DIAGNOSTICO,
    _E.AGUARDANDO_ORCAMENTO,
    _E.AGUARDANDO_DECISAO,
    _E.AGUARDANDO_RESERVA,
    _E.AGUARDANDO_PAGAMENTO,
    _E.AGUARDANDO_AGENDAMENTO,
    _E.AGUARDANDO_INICIO,
    _E.EM_EXECUCAO,
    _E.CONCLUIDA,
)
# Etapas de onde o cancelamento passa pela saga: as de antes do pivot.
ANTES_DO_PIVOT: Final = FLUXO_DA_SAGA[: FLUXO_DA_SAGA.index(_E.EM_EXECUCAO)]
# Compensacao em curso, ou parada nela: a saga tem motivo e plano.
ETAPAS_EM_COMPENSACAO: Final = (_E.COMPENSANDO, _E.FALHA_NA_COMPENSACAO)
# Etapas de onde a saga nao sai mais, e as de onde ainda sai (os labels de
# etapa das metricas).
ETAPAS_FINAIS: Final = (_E.CONCLUIDA, _E.COMPENSADA)
ETAPAS_NAO_FINAIS: Final = tuple(e for e in EtapaSaga if e not in ETAPAS_FINAIS)

# Etapa em que cada evento consumido e esperado (tabela da RFC-004 secao 4.1;
# as falhas de negocio esperam a etapa do passo que falha, e as respostas de
# compensacao, compensando).
ETAPA_ESPERADA: Final[Mapping[str, EtapaSaga]] = MappingProxyType(
    {
        "DiagnosticoIniciado": _E.AGUARDANDO_DIAGNOSTICO,
        "DiagnosticoConcluido": _E.AGUARDANDO_DIAGNOSTICO,
        "OrcamentoGerado": _E.AGUARDANDO_ORCAMENTO,
        "GeracaoDeOrcamentoFalhou": _E.AGUARDANDO_ORCAMENTO,
        "OrcamentoAprovado": _E.AGUARDANDO_DECISAO,
        "OrcamentoRecusado": _E.AGUARDANDO_DECISAO,
        "OrcamentoExpirado": _E.AGUARDANDO_DECISAO,
        "PecasReservadas": _E.AGUARDANDO_RESERVA,
        "ReservaDePecasFalhou": _E.AGUARDANDO_RESERVA,
        "PagamentoSolicitado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoConfirmado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoRecusado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoExpirado": _E.AGUARDANDO_PAGAMENTO,
        "ExecucaoAgendada": _E.AGUARDANDO_AGENDAMENTO,
        "ExecucaoIniciada": _E.AGUARDANDO_INICIO,
        "ExecucaoFinalizada": _E.EM_EXECUCAO,
        "DiagnosticoDescartado": _E.COMPENSANDO,
        "OrcamentoCancelado": _E.COMPENSANDO,
        "ReservaLiberada": _E.COMPENSANDO,
        "PagamentoCancelado": _E.COMPENSANDO,
        "PagamentoEstornado": _E.COMPENSANDO,
        "EstornoDePagamentoFalhou": _E.COMPENSANDO,
        "ExecucaoCancelada": _E.COMPENSANDO,
    }
)

# Linha da tabela da RFC-004 secao 4.1 de cada evento do fluxo normal: etapa
# seguinte, comando enviado (com prazo tecnico se estiver em
# ``COMANDOS_COM_PRAZO``) e o passo T concluido, que o plano de compensacao
# desfaz (RFC-004 secao 4.4).
FLUXO_NORMAL: Final[Mapping[str, Transicao]] = MappingProxyType(
    {
        "DiagnosticoIniciado": Transicao(_E.AGUARDANDO_DIAGNOSTICO),
        "DiagnosticoConcluido": Transicao(
            _E.AGUARDANDO_ORCAMENTO, Comando.GERAR_ORCAMENTO
        ),
        "OrcamentoGerado": Transicao(_E.AGUARDANDO_DECISAO, concluido="T3"),
        "OrcamentoAprovado": Transicao(_E.AGUARDANDO_RESERVA, Comando.RESERVAR_PECAS),
        "PecasReservadas": Transicao(
            _E.AGUARDANDO_PAGAMENTO, Comando.SOLICITAR_PAGAMENTO, "T5"
        ),
        "PagamentoSolicitado": Transicao(_E.AGUARDANDO_PAGAMENTO, concluido="T6"),
        "PagamentoConfirmado": Transicao(
            _E.AGUARDANDO_AGENDAMENTO, Comando.AGENDAR_EXECUCAO
        ),
        "ExecucaoAgendada": Transicao(_E.AGUARDANDO_INICIO, concluido="T7"),
        "ExecucaoIniciada": Transicao(_E.EM_EXECUCAO),
        "ExecucaoFinalizada": Transicao(_E.CONCLUIDA),
    }
)
# Comandos com resposta automatica, que ganham prazo tecnico (RFC-004 secoes 4.3
# e 4.6). O SolicitarDiagnostico e as esperas humanas nao tem prazo.
COMANDOS_COM_PRAZO: Final = frozenset(
    {
        Comando.GERAR_ORCAMENTO,
        Comando.RESERVAR_PECAS,
        Comando.SOLICITAR_PAGAMENTO,
        Comando.AGENDAR_EXECUCAO,
        *ORDEM_DAS_COMPENSACOES,
    }
)
# O AgendarExecucao sai sempre com esta prioridade nesta fase: nenhuma regra
# escolhe `alta` (RFC-004 secao 5.3).
PRIORIDADE_DO_AGENDAMENTO: Final = "normal"
