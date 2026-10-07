"""Tabela da saga (RFC-004 secao 4.1) e a classificacao dos eventos (secao 4.5).

A ordem das etapas, a etapa em que cada evento e esperado, a linha de cada
evento do fluxo normal (etapa seguinte, comando enviado e passo concluido) e os
comandos com prazo; ``classificar`` decide se o evento recebido e processado,
ignorado, adiantado ou recusado. ``linha_do_evento`` e ``conferir_envio`` sao
as conferencias que a ``Saga`` faz antes de aplicar uma linha.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
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


@dataclass(frozen=True, slots=True)
class LinhaDoFluxo:
    """Linha da tabela da RFC-004 secao 4.1 para um evento do fluxo normal.

    ``seguinte`` e a etapa depois do evento; ``comando``, o que ele envia (com
    prazo tecnico se estiver em ``COMANDOS_COM_PRAZO``); ``concluido``, o passo
    T que ele conclui, que o plano de compensacao desfaz (RFC-004 secao 4.4).
    """

    seguinte: EtapaSaga
    comando: Comando | None = None
    concluido: str | None = None


FLUXO_NORMAL: Final[Mapping[str, LinhaDoFluxo]] = MappingProxyType(
    {
        "DiagnosticoIniciado": LinhaDoFluxo(_E.AGUARDANDO_DIAGNOSTICO),
        "DiagnosticoConcluido": LinhaDoFluxo(
            _E.AGUARDANDO_ORCAMENTO, Comando.GERAR_ORCAMENTO
        ),
        "OrcamentoGerado": LinhaDoFluxo(_E.AGUARDANDO_DECISAO, concluido="T3"),
        "OrcamentoAprovado": LinhaDoFluxo(
            _E.AGUARDANDO_RESERVA, Comando.RESERVAR_PECAS
        ),
        "PecasReservadas": LinhaDoFluxo(
            _E.AGUARDANDO_PAGAMENTO, Comando.SOLICITAR_PAGAMENTO, "T5"
        ),
        "PagamentoSolicitado": LinhaDoFluxo(_E.AGUARDANDO_PAGAMENTO, concluido="T6"),
        "PagamentoConfirmado": LinhaDoFluxo(
            _E.AGUARDANDO_AGENDAMENTO, Comando.AGENDAR_EXECUCAO
        ),
        "ExecucaoAgendada": LinhaDoFluxo(_E.AGUARDANDO_INICIO, concluido="T7"),
        "ExecucaoIniciada": LinhaDoFluxo(_E.EM_EXECUCAO),
        "ExecucaoFinalizada": LinhaDoFluxo(_E.CONCLUIDA),
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
    # OS cancelada ou entregue com a saga viva: o cancelamento recusa esse
    # estado, e nenhum evento o toca (nem comando para uma OS encerrada).
    ORDEM_ENCERRADA = "ordem_encerrada"


def classificar(etapa: EtapaSaga, tipo: str, marcos: MarcosDaOrdem) -> Classificacao:
    """Classifica o evento ``tipo`` ANTES de tocar no dominio (RFC-004 secao 4.5).

    Saga encerrada: ignorado. OS encerrada com a saga viva: recusado. Resposta
    de compensacao: processada em compensando e ignorada fora dela. Evento do
    fluxo normal com a saga em compensacao (ou na falha dela) ou de etapa ja
    passada: ignorado; de etapa a frente: adiantado. Na mesma etapa, os marcos
    da OS desempatam: ``DiagnosticoConcluido`` antes do diagnostico iniciado e
    ``PagamentoConfirmado``, ``Recusado`` ou ``Expirado`` antes do checkout
    aberto sao adiantados; ``DiagnosticoIniciado`` com o diagnostico ja
    iniciado e ``PagamentoSolicitado`` com o checkout ja aberto, repetidos.
    """
    esperada = ETAPA_ESPERADA[tipo]
    if etapa in ETAPAS_FINAIS:
        if esperada is _E.COMPENSANDO:
            return Classificacao.FORA_DA_COMPENSACAO
        return Classificacao.FORA_DO_FLUXO
    if marcos.encerrada:
        return Classificacao.ORDEM_ENCERRADA
    if esperada is _E.COMPENSANDO:
        if etapa is _E.COMPENSANDO:
            return Classificacao.PROCESSAR
        return Classificacao.FORA_DA_COMPENSACAO
    if etapa not in FLUXO_DA_SAGA:
        return Classificacao.FORA_DO_FLUXO
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


def linha_do_evento(
    saga: Saga, evento: MensagemRecebida, marcos: MarcosDaOrdem, agora: datetime
) -> LinhaDoFluxo:
    """A linha do ``evento`` no fluxo normal, se ele pode avancar a ``saga`` agora.

    O evento e da OS da saga e se classifica para processar na etapa dela (com
    os ``marcos`` da OS de antes do fato), e o instante tem fuso e nao volta
    antes do ultimo registro.

    Raises:
        TransicaoDaSagaInvalidaError: alguma das tres conferencias falhou.
    """
    tipo = evento.tipo
    linha = FLUXO_NORMAL.get(tipo)
    if evento.correlation_id != saga.id:
        msg = f"{tipo} de outra ordem"
    elif linha is None or classificar(saga.etapa, tipo, marcos) is not (
        Classificacao.PROCESSAR
    ):
        msg = f"{tipo} nao avanca a saga na etapa {saga.etapa.value}"
    elif agora.tzinfo is None or agora < saga.atualizada_em:
        msg = f"{tipo} num instante sem fuso ou anterior ao ultimo registro"
    else:
        return linha
    raise TransicaoDaSagaInvalidaError(msg)


def conferir_envio(
    linha: LinhaDoFluxo,
    envio: Envio | None,
    *,
    ordem_id: UUID,
    itens: Sequence[ItemDoDiagnostico],
    agora: datetime,
) -> None:
    """O ``envio`` e o comando da ``linha``, com os dados e o prazo dela.

    Nenhum envio onde a linha nao envia; nos dados, o ``ordem_id`` da OS e o que
    a saga guarda (os ``itens`` do diagnostico no ``GerarOrcamento``, as pecas
    deles no ``ReservarPecas``); prazo depois de ``agora`` so nos
    ``COMANDOS_COM_PRAZO``.

    Raises:
        TransicaoDaSagaInvalidaError: o envio nao e o da linha.
    """
    if envio is None and linha.comando is None:
        return
    if envio is None or envio.tipo is not linha.comando:
        enviado = envio.tipo if envio is not None else "nenhum comando"
        msg = f"a linha envia {linha.comando or 'nenhum comando'}, nao {enviado}"
        raise TransicaoDaSagaInvalidaError(msg)
    da_saga: dict[Comando, dict[str, object]] = {
        Comando.GERAR_ORCAMENTO: {"itens": list(itens)},
        Comando.RESERVAR_PECAS: {"pecas": pecas_dos_itens(itens)},
    }
    esperados = {"ordem_id": str(ordem_id), **da_saga.get(envio.tipo, {})}
    if any(envio.dados.get(chave) != valor for chave, valor in esperados.items()):
        msg = f"{envio.tipo} com dados que nao sao os desta saga"
        raise TransicaoDaSagaInvalidaError(msg)
    prazo = envio.prazo_resposta_em
    com_prazo = envio.tipo in COMANDOS_COM_PRAZO
    if (prazo is not None) is not com_prazo or (
        prazo is not None and (prazo.tzinfo is None or prazo <= agora)
    ):
        msg = f"{envio.tipo} sem o prazo de resposta depois do envio"
        raise TransicaoDaSagaInvalidaError(msg)
