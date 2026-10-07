"""Tabela da saga (RFC-004 secao 4.1) e a classificacao dos eventos (secao 4.5).

A ordem das etapas, a etapa em que cada evento e esperado, a etapa seguinte
de cada evento do fluxo normal e os comandos com prazo; ``classificar`` decide
se o evento recebido e processado, ignorado ou adiantado.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.dominio.status import StatusOrdem

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

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
# Etapas de onde a saga ainda sai: os labels de etapa das metricas.
ETAPAS_NAO_FINAIS: Final = tuple(
    e for e in EtapaSaga if e not in {_E.CONCLUIDA, _E.COMPENSADA}
)

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
# Etapa seguinte de cada evento do fluxo normal (RFC-004 secao 4.1).
ETAPA_SEGUINTE: Final[Mapping[str, EtapaSaga]] = MappingProxyType(
    {
        "DiagnosticoIniciado": _E.AGUARDANDO_DIAGNOSTICO,
        "DiagnosticoConcluido": _E.AGUARDANDO_ORCAMENTO,
        "OrcamentoGerado": _E.AGUARDANDO_DECISAO,
        "OrcamentoAprovado": _E.AGUARDANDO_RESERVA,
        "PecasReservadas": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoSolicitado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoConfirmado": _E.AGUARDANDO_AGENDAMENTO,
        "ExecucaoAgendada": _E.AGUARDANDO_INICIO,
        "ExecucaoIniciada": _E.EM_EXECUCAO,
        "ExecucaoFinalizada": _E.CONCLUIDA,
    }
)
# Passo que cada evento conclui: o plano de compensacao desfaz os concluidos
# (RFC-004 secao 4.4).
PASSO_CONCLUIDO: Final = MappingProxyType(
    {
        "OrcamentoGerado": "T3",
        "PecasReservadas": "T5",
        "PagamentoSolicitado": "T6",
        "ExecucaoAgendada": "T7",
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
        Comando.CANCELAR_EXECUCAO,
        Comando.ESTORNAR_PAGAMENTO,
        Comando.LIBERAR_RESERVA,
        Comando.CANCELAR_ORCAMENTO,
        Comando.DESCARTAR_DIAGNOSTICO,
    }
)
GATILHO_ABERTURA: Final = "abertura"


class Classificacao(StrEnum):
    """Como um evento recebido se encaixa na etapa atual (RFC-004 secao 4.5)."""

    PROCESSAR = "processar"
    # Etapa a frente da atual: volta pela fila de retry ate a saga alcanca-lo.
    ADIANTADO = "adiantado"
    # Etapa ja passada, inclusive a resposta que o participante republica
    # quando o comando e reenviado.
    OBSOLETO = "obsoleto"
    # Mesma etapa, com o fato ja aplicado.
    REPETIDO = "repetido"
    # Evento do fluxo normal com a saga encerrada ou em compensacao.
    FORA_DO_FLUXO = "fora_do_fluxo"
    # Resposta de compensacao sem compensacao em curso.
    FORA_DA_COMPENSACAO = "fora_da_compensacao"


def classificar(etapa: EtapaSaga, tipo: str, ordem: OrdemDeServico) -> Classificacao:
    """Classifica o evento ``tipo`` ANTES de tocar no dominio (RFC-004 secao 4.5).

    Etapa ja passada e saga encerrada ou em compensacao: ignorado. Etapa a
    frente: adiantado. Na mesma etapa, o estado da OS desempata:
    ``DiagnosticoConcluido`` com a OS ainda ``recebida`` e
    ``PagamentoConfirmado``, ``Recusado`` ou ``Expirado`` sem o resumo do
    pagamento sao adiantados; ``DiagnosticoIniciado`` com a OS ja em
    diagnostico e ``PagamentoSolicitado`` com o resumo gravado, repetidos.
    """
    esperada = ETAPA_ESPERADA[tipo]
    if esperada is _E.COMPENSANDO:
        if etapa is _E.COMPENSANDO:
            return Classificacao.PROCESSAR
        return Classificacao.FORA_DA_COMPENSACAO
    if etapa not in FLUXO_DA_SAGA or etapa is _E.CONCLUIDA:
        return Classificacao.FORA_DO_FLUXO
    atual = FLUXO_DA_SAGA.index(etapa)
    alvo = FLUXO_DA_SAGA.index(esperada)
    if alvo < atual:
        return Classificacao.OBSOLETO
    if alvo > atual:
        return Classificacao.ADIANTADO
    return _na_mesma_etapa(tipo, ordem)


def _na_mesma_etapa(tipo: str, ordem: OrdemDeServico) -> Classificacao:
    """Fora de ordem dentro da etapa, pelo estado da OS (RFC-004 secao 4.5)."""
    pagamento_solicitado = ordem.resumo_pagamento is not None
    match tipo:
        case "DiagnosticoIniciado" if ordem.status is not StatusOrdem.RECEBIDA:
            return Classificacao.REPETIDO
        case "DiagnosticoConcluido" if ordem.status is StatusOrdem.RECEBIDA:
            return Classificacao.ADIANTADO
        case "PagamentoSolicitado" if pagamento_solicitado:
            return Classificacao.REPETIDO
        case "PagamentoConfirmado" | "PagamentoRecusado" | "PagamentoExpirado" if (
            not pagamento_solicitado
        ):
            return Classificacao.ADIANTADO
    return Classificacao.PROCESSAR
