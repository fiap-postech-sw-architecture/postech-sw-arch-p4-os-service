"""Tabela da saga (RFC-004 secao 4.1) e a classificacao dos eventos (secao 4.5).

A ordem das etapas, a etapa em que cada evento e esperado, a linha de cada
evento do fluxo normal (etapa seguinte, comando enviado e passo concluido) e os
comandos com prazo; ``classificar`` decide se o evento recebido e processado,
ignorado, adiantado ou recusado. ``transicao_do_evento``,
``transicao_do_cancelamento``, ``conferir_envio`` e ``conferir_estado`` sao as
conferencias que a ``Saga`` faz antes de mudar; as transicoes da compensacao
vem de ``compensacao``.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.compensacao import (
    CORRIDA_DO_PIVOT,
    MOTIVO_DA_FALHA,
    ORDEM_DAS_COMPENSACOES,
    RESPOSTA_DA_COMPENSACAO,
    inicio,
    resposta,
)
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    MotivoCompensacao,
    Transicao,
    TransicaoDaSagaInvalidaError,
    pecas_dos_itens,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.modelo import Envio, ItemDoDiagnostico
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem

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
GATILHO_ABERTURA: Final = "abertura"
GATILHO_CANCELAMENTO: Final = "cancelamento"


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
    o evento de etapa ja passada e ignorado
    e o de etapa a frente, adiantado; na mesma etapa, os marcos da OS
    desempatam: ``DiagnosticoConcluido`` antes do diagnostico iniciado e
    ``PagamentoConfirmado``, ``Recusado`` ou ``Expirado`` antes do checkout
    aberto sao adiantados; ``DiagnosticoIniciado`` com o diagnostico ja
    iniciado e ``PagamentoSolicitado`` com o checkout ja aberto, repetidos.
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


def transicao_do_evento(
    saga: Saga, evento: MensagemRecebida, marcos: MarcosDaOrdem, agora: datetime
) -> Transicao:
    """A transicao que o ``evento`` provoca na ``saga`` agora, se ele pode.

    O evento e da OS da saga e se classifica para processar na etapa dela (com
    os ``marcos`` da OS de antes do fato e o ``causation_id``), e o instante tem
    fuso e nao volta antes do ultimo registro. A transicao e a linha do fluxo
    normal, o inicio da compensacao pela falha de negocio, a resposta da
    compensacao pendente ou a corrida do pivot (RFC-004 secoes 4.1, 4.4 e 4.5).

    Raises:
        TransicaoDaSagaInvalidaError: alguma das tres conferencias falhou.
    """
    tipo = evento.tipo
    classificacao = saga.classificar(tipo, marcos, causation_id=evento.causation_id)
    if evento.correlation_id != saga.id:
        msg = f"{tipo} de outra ordem"
    elif classificacao is not Classificacao.PROCESSAR:
        msg = f"{tipo} nao avanca a saga na etapa {saga.etapa.value}"
    elif not _instante_valido(saga, agora):
        msg = f"{tipo} num instante sem fuso ou anterior ao ultimo registro"
    elif tipo in MOTIVO_DA_FALHA:
        return inicio(saga, MOTIVO_DA_FALHA[tipo])
    elif tipo in RESPOSTA_DA_COMPENSACAO:
        return resposta(saga, tipo)
    elif saga.etapa in ETAPAS_EM_COMPENSACAO:
        # So o ExecucaoIniciada se classifica para processar aqui.
        return CORRIDA_DO_PIVOT
    else:
        return FLUXO_NORMAL[tipo]
    raise TransicaoDaSagaInvalidaError(msg)


def transicao_do_cancelamento(saga: Saga, agora: datetime) -> Transicao:
    """O pedido de cancelamento antes do pivot: a compensacao comeca (RFC-004 4.4).

    Raises:
        TransicaoDaSagaInvalidaError: etapa que nao e de antes do pivot, ou
            instante sem fuso ou que volta antes do ultimo registro.
    """
    if saga.etapa not in ANTES_DO_PIVOT:
        msg = f"cancelamento pela saga na etapa {saga.etapa.value}"
    elif not _instante_valido(saga, agora):
        msg = "cancelamento num instante sem fuso ou anterior ao ultimo registro"
    else:
        return inicio(saga, MotivoCompensacao.CANCELAMENTO)
    raise TransicaoDaSagaInvalidaError(msg)


def _instante_valido(saga: Saga, agora: datetime) -> bool:
    return agora.tzinfo is not None and agora >= saga.atualizada_em


def conferir_envio(
    transicao: Transicao,
    envio: Envio | None,
    *,
    ordem_id: UUID,
    itens: Sequence[ItemDoDiagnostico],
    marcos: MarcosDaOrdem,
) -> None:
    """O ``envio`` e o comando da ``transicao``, com os dados que a saga e a OS fixam.

    Nenhum envio onde ela nao envia; nos dados, o ``ordem_id`` da OS e o que a
    saga e a OS guardam: os ``itens`` do diagnostico no ``GerarOrcamento``, as
    pecas deles no ``ReservarPecas``, o orcamento dos ``marcos`` da OS no
    ``SolicitarPagamento``, a prioridade ``normal`` no ``AgendarExecucao`` e o
    ``motivo`` nas compensacoes.

    Raises:
        TransicaoDaSagaInvalidaError: o envio nao e o da transicao, ou seus
            dados nao sao os desta saga.
    """
    if envio is None and transicao.comando is None:
        return
    if envio is None or envio.tipo is not transicao.comando:
        enviado = envio.tipo if envio is not None else "nenhum comando"
        msg = f"a linha envia {transicao.comando or 'nenhum comando'}, nao {enviado}"
        raise TransicaoDaSagaInvalidaError(msg)
    # Sem orcamento na OS, str(None), que e "None", nao casa com id nenhum.
    da_saga: dict[Comando, dict[str, object]] = {
        Comando.GERAR_ORCAMENTO: {"itens": list(itens)},
        Comando.RESERVAR_PECAS: {"pecas": pecas_dos_itens(itens)},
        Comando.SOLICITAR_PAGAMENTO: {"orcamento_id": str(marcos.orcamento_id)},
        Comando.AGENDAR_EXECUCAO: {"prioridade": PRIORIDADE_DO_AGENDAMENTO},
        **{c: {"motivo": transicao.motivo} for c in ORDEM_DAS_COMPENSACOES},
    }
    esperados = {"ordem_id": str(ordem_id), **da_saga.get(envio.tipo, {})}
    if any(envio.dados.get(chave) != valor for chave, valor in esperados.items()):
        msg = f"{envio.tipo} com dados que nao sao os desta saga"
        raise TransicaoDaSagaInvalidaError(msg)


def conferir_prazo(envio: Envio | None, agora: datetime) -> None:
    """O prazo de resposta do ``envio``, que so os ``COMANDOS_COM_PRAZO`` levam.

    O prazo e depois de ``agora`` e tem fuso horario.

    Raises:
        TransicaoDaSagaInvalidaError: prazo faltando, sobrando, sem fuso horario
            ou que nao e depois do envio.
    """
    if envio is None:
        return
    prazo = envio.prazo_resposta_em
    com_prazo = envio.tipo in COMANDOS_COM_PRAZO
    if (prazo is not None) is not com_prazo or (
        prazo is not None and (prazo.tzinfo is None or prazo <= agora)
    ):
        msg = f"{envio.tipo} sem o prazo de resposta depois do envio"
        raise TransicaoDaSagaInvalidaError(msg)


def conferir_estado(saga: Saga) -> None:
    """Invariantes do estado da saga, para quem a monta por fora do ``iniciar``.

    Prazo se e so se ha comando em voo; ``reenvios`` nunca negativo e zero sem
    comando em voo; plano so com a compensacao em curso ou parada, e nela
    sempre, com o motivo; a falha so e sempre em ``falha_na_compensacao``.

    Raises:
        ValueError: estado contraditorio.
    """
    em_voo = saga.comando_em_voo is not None
    compensacao = saga.etapa in ETAPAS_EM_COMPENSACAO
    if (saga.prazo_resposta_em is not None) is not em_voo:
        msg = "prazo de resposta so com comando em voo, e todo comando em voo tem"
    elif saga.reenvios < 0 or (not em_voo and saga.reenvios):
        msg = f"reenvios {saga.reenvios} sem comando em voo ou negativo"
    elif compensacao and not (saga.motivo and saga.plano_compensacao):
        msg = f"{saga.etapa.value} exige o motivo e o plano de compensacao"
    elif saga.plano_compensacao and not compensacao:
        msg = f"plano de compensacao na etapa {saga.etapa.value}"
    elif (saga.falha is not None) is not (saga.etapa is _E.FALHA_NA_COMPENSACAO):
        msg = "a falha so existe em falha_na_compensacao, e sempre nela"
    else:
        return
    raise ValueError(msg)
