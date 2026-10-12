"""Classificacao do evento recebido na etapa da saga (RFC-004 secao 4.5).

Decide ANTES de tocar no dominio se o evento e processado, ignorado, adiantado
ou recusado, pela etapa esperada de cada tipo (``tabela_da_saga``), pela
compensacao pendente e pelos marcos da OS.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.compensacao import RESPOSTA_DA_COMPENSACAO
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    ETAPA_ESPERADA,
    ETAPAS_EM_COMPENSACAO,
    ETAPAS_FINAIS,
    FLUXO_DA_SAGA,
)

if TYPE_CHECKING:
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem

_E = EtapaSaga


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
    # Resposta de compensacao fora de compensando ou que nao responde a
    # compensacao pendente (resposta atrasada na falha da compensacao inclusive).
    FORA_DA_COMPENSACAO = "fora_da_compensacao"
    # PagamentoEstornado que nao e a resposta casada do EstornarPagamento: o
    # estorno automatico do Billing, que so atualiza o resumo (RFC-004 4.4).
    ESTORNO_AUTOMATICO = "estorno_automatico"
    # Inicio ou fim da execucao com outra compensacao pendente que nao o
    # CancelarExecucao: contradiz o que a saga sabe, ignorado com log de erro.
    INCOERENTE = "incoerente"
    # OS cancelada ou entregue com a saga viva: o cancelamento recusa esse
    # estado, e nenhum evento o toca (nem comando para uma OS encerrada).
    ORDEM_ENCERRADA = "ordem_encerrada"


def classificar(
    etapa: EtapaSaga,
    tipo: str,
    marcos: MarcosDaOrdem,
    *,
    pendente: str | None = None,
    casada: bool = False,
) -> Classificacao:
    """Classifica o evento ``tipo`` ANTES de tocar no dominio (RFC-004 secao 4.5).

    ``pendente`` e a compensacao que espera resposta (a primeira do plano) e
    ``casada``, se o ``causation_id`` do evento e o id de um envio do comando
    em voo. ``PagamentoEstornado`` que nao e a resposta casada do
    ``EstornarPagamento`` e o estorno automatico, em qualquer etapa. Saga
    encerrada: ignorado. OS encerrada com a saga viva: recusado. Resposta de
    compensacao: processada em compensando quando responde a pendente (as do
    ``EstornarPagamento``, so casadas), ignorada fora disso. Com a compensacao
    em curso ou parada, o inicio e o fim da execucao seguem a corrida do pivot
    (``_na_corrida_do_pivot``) e o resto do fluxo normal e ignorado. No fluxo,
    o evento de etapa ja passada e ignorado e o de etapa a frente, adiantado;
    na mesma etapa, os marcos da OS desempatam: ``DiagnosticoConcluido`` antes
    do diagnostico iniciado e ``PagamentoConfirmado``, ``Recusado`` ou
    ``Expirado`` antes do checkout aberto sao adiantados;
    ``DiagnosticoIniciado`` com o diagnostico ja iniciado e
    ``PagamentoSolicitado`` com o checkout ja aberto, repetidos.
    """
    esperada = ETAPA_ESPERADA[tipo]
    responde = etapa is _E.COMPENSANDO and _responde(tipo, pendente, casada=casada)
    if tipo == "PagamentoEstornado" and not responde:
        return Classificacao.ESTORNO_AUTOMATICO
    if etapa in ETAPAS_FINAIS:
        if esperada is _E.COMPENSANDO:
            return Classificacao.FORA_DA_COMPENSACAO
        return Classificacao.FORA_DO_FLUXO
    if marcos.encerrada:
        return Classificacao.ORDEM_ENCERRADA
    if esperada is _E.COMPENSANDO:
        return (
            Classificacao.PROCESSAR if responde else Classificacao.FORA_DA_COMPENSACAO
        )
    if etapa in ETAPAS_EM_COMPENSACAO:
        return _na_corrida_do_pivot(tipo, pendente)
    return _no_fluxo(etapa, esperada, tipo, marcos)


def _na_corrida_do_pivot(tipo: str, pendente: str | None) -> Classificacao:
    """Evento do fluxo normal com a compensacao em curso ou parada (RFC-004 4.5).

    Com o ``CancelarExecucao`` pendente, a Execucao pode ter iniciado antes de
    recebe-lo: o ``ExecucaoIniciada`` decide a corrida, e o
    ``ExecucaoFinalizada`` que o ultrapassou e adiantado (volta pela retry ate
    o inicio chegar). Com outra pendente, a Execucao ja cancelou ou nem
    agendou: os dois contradizem o que a saga sabe. O resto, ignorado.
    """
    if tipo not in {"ExecucaoIniciada", "ExecucaoFinalizada"}:
        return Classificacao.FORA_DO_FLUXO
    if pendente != Comando.CANCELAR_EXECUCAO:
        return Classificacao.INCOERENTE
    if tipo == "ExecucaoIniciada":
        return Classificacao.PROCESSAR
    return Classificacao.ADIANTADO


def _responde(tipo: str, pendente: str | None, *, casada: bool) -> bool:
    """O ``tipo`` e a resposta da compensacao ``pendente``.

    As do ``EstornarPagamento`` so com o ``causation_id`` casado: o estorno
    automatico tambem e ``PagamentoEstornado`` (RFC-004 secao 4.4).
    """
    respondida = RESPOSTA_DA_COMPENSACAO.get(tipo)
    if respondida is None or respondida != pendente:
        return False
    return casada or respondida is not Comando.ESTORNAR_PAGAMENTO


def _no_fluxo(
    etapa: EtapaSaga, esperada: EtapaSaga, tipo: str, marcos: MarcosDaOrdem
) -> Classificacao:
    """Evento do fluxo normal com a saga no fluxo: pela ordem das etapas."""
    atual = FLUXO_DA_SAGA.index(etapa)
    alvo = FLUXO_DA_SAGA.index(esperada)
    if alvo < atual:
        return Classificacao.OBSOLETO
    if alvo > atual:
        return Classificacao.ADIANTADO
    return _na_mesma_etapa(tipo, marcos)


def _na_mesma_etapa(tipo: str, marcos: MarcosDaOrdem) -> Classificacao:
    """Fora de ordem dentro da etapa, pelos marcos da OS (RFC-004 secao 4.5)."""
    match tipo:
        case "DiagnosticoIniciado" if marcos.diagnostico_iniciado:
            return Classificacao.REPETIDO
        case "DiagnosticoConcluido" if not marcos.diagnostico_iniciado:
            return Classificacao.ADIANTADO
        case "PagamentoSolicitado" if marcos.checkout_aberto:
            return Classificacao.REPETIDO
        case "PagamentoConfirmado" | "PagamentoRecusado" | "PagamentoExpirado" if (
            not marcos.checkout_aberto
        ):
            return Classificacao.ADIANTADO
    return Classificacao.PROCESSAR
