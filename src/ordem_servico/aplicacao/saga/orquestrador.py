"""Orquestrador da saga: trata os eventos de ``os.eventos`` (RFC-004 secao 4).

Cada evento roda na transacao da mensagem: le saga e OS, classifica o evento
pela etapa ANTES de tocar no dominio (RFC-004 secao 4.5), aplica o fato na OS,
avanca a saga e grava o comando seguinte na outbox. Quem comita e o consumidor,
junto com ``mensagens_processadas`` (ADR-036): etapa, status, registro e
comando entram no mesmo commit ou nao entram.

Cada tipo de evento tem a sua linha: o fluxo normal (tabela da RFC-004 secao
4.1); a falha de negocio, que inicia a compensacao com a primeira do plano; a
resposta da compensacao pendente, que envia a seguinte ou, na ultima, cancela
a OS (secao 4.4); e o ``ExecucaoIniciada`` que vence o cancelamento (corrida
do pivot, secao 4.5). Evento de etapa ja passada, repetido ou com a saga fora
do fluxo e ignorado com log; o ``PagamentoEstornado`` que nao responde a
compensacao (estorno automatico) so atualiza o resumo do pagamento; o
adiantado volta pela fila de retry (``EventoAdiantadoError``). O que nenhuma
tentativa resolve vai para a DLQ com o motivo em codigo
(``EventoRecusadoError``): OS sem saga, ``ordem_id`` divergente, OS encerrada
com a saga viva e fato que a OS ou a saga recusam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import structlog

from src.compartilhado.aplicacao.mensageria import (
    Comando,
    Desfecho,
    FalhaPermanenteError,
    FalhaTransitoriaError,
)
from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaException,
    ViolacaoRegraDeNegocioException,
)
from src.ordem_servico.aplicacao.saga.compensacao import (
    MOTIVO_DA_FALHA,
    RESPOSTA_DA_COMPENSACAO,
    ROTULO_DO_MOTIVO,
    inicio,
    resposta,
)
from src.ordem_servico.aplicacao.saga.modelo import (
    DadosDoContrato,
    Envio,
    EtapaSaga,
    MotivoCompensacao,
    TransicaoDaSagaInvalidaError,
    itens_do_diagnostico,
    pecas_dos_itens,
)
from src.ordem_servico.aplicacao.saga.resumos_do_billing import (
    resumo_do_orcamento,
    resumo_do_pagamento,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    PRIORIDADE_DO_AGENDAMENTO,
    Classificacao,
)
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.resumos import StatusPagamento

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.compartilhado.aplicacao.unit_of_work import PublicadorDeComandos
    from src.ordem_servico.aplicacao.ports import SagaRepository
    from src.ordem_servico.aplicacao.saga.modelo import Transicao
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository
    from src.ordem_servico.dominio.resumos import ResumoOrcamento

_log = structlog.get_logger(__name__)

# Ator dos fatos que o consumidor aplica (historico da OS e passos da saga).
ATOR_CONSUMIDOR: Final = "consumidor"
# Quanto o relogio desta replica pode estar atras do ultimo registro da saga
# (de outra replica ou da API): ate 1 s e jitter e passa sem aviso; acima, o
# aviso mostra a defasagem; acima de 60 s nao e jitter, e o evento vai para a DLQ.
_DEFASAGEM_SEM_AVISO: Final = timedelta(seconds=1)
_DEFASAGEM_MAXIMA: Final = timedelta(seconds=60)
# O que a OS ou a saga recusam ao aplicar o fato: estado incompativel com o
# evento, que nenhuma nova tentativa muda.
_RECUSAS_DO_DOMINIO: Final = (
    TransicaoStatusInvalidaException,
    ViolacaoRegraDeNegocioException,
    TransicaoDaSagaInvalidaError,
)
# Eventos do Billing que mudam o estado do pagamento depois do checkout: as
# falhas de negocio dele e as respostas ao EstornarPagamento.
_STATUS_DO_PAGAMENTO: Final[Mapping[str, StatusPagamento]] = MappingProxyType(
    {
        "PagamentoRecusado": StatusPagamento.RECUSADO,
        "PagamentoExpirado": StatusPagamento.EXPIRADO,
        "PagamentoCancelado": StatusPagamento.CANCELADO,
        "PagamentoEstornado": StatusPagamento.ESTORNADO,
    }
)


def _agora() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Tratamento:
    """Desfecho do evento e a etapa antes e depois dele (span e log)."""

    desfecho: Desfecho
    etapa: EtapaSaga
    etapa_nova: EtapaSaga


class EventoAdiantadoError(FalhaTransitoriaError):
    """Evento de etapa a frente da atual: volta pela fila de retry (RFC-004 secao 4.5).

    Esgotadas as tentativas, vai para a DLQ com alerta.
    """

    def __init__(self, etapa: EtapaSaga) -> None:
        super().__init__(f"evento adiantado na etapa {etapa.value}")
        self.etapa = etapa


class EventoRecusadoError(FalhaPermanenteError):
    """Evento que nenhuma tentativa resolve: DLQ com o ``motivo`` em codigo.

    ``etapa`` e a da saga quando ela existe (vai para o span do consumo).
    """

    def __init__(self, motivo: str, etapa: EtapaSaga | None = None) -> None:
        super().__init__(motivo)
        self.etapa = etapa


# O comando que a linha envia: tipo e ``dados`` (sem ele, ``None``).
type _Pedido = tuple[Comando, DadosDoContrato] | None
type _Aplicacao = Callable[[MensagemRecebida, Saga, OrdemDeServico], _Pedido]


class OrquestradorDaSaga:
    """Aplica cada evento na OS e na saga (RFC-004 secoes 4.1, 4.4 e 4.5).

    Le saga e OS, classifica o evento e traduz a linha dele em chamadas: o
    fato na OS, o comando na outbox e o registro na saga, que confere etapa,
    comando, dados e prazo antes de mudar.
    """

    def __init__(
        self,
        *,
        ordens: OrdemDeServicoRepository,
        sagas: SagaRepository,
        publicador: PublicadorDeComandos,
        prazo_resposta: timedelta,
        relogio: Callable[[], datetime] = _agora,
    ) -> None:
        self._ordens = ordens
        self._sagas = sagas
        self._publicador = publicador
        self._prazo_resposta = prazo_resposta
        self._relogio = relogio

    def tratar(self, evento: MensagemRecebida) -> Tratamento:
        """Classifica e aplica ``evento``; o commit e de quem chama.

        Raises:
            EventoAdiantadoError: evento de etapa a frente (transitorio).
            EventoRecusadoError: permanente, DLQ com o motivo:
                ``ordem_id_divergente`` (``ordem_id`` dos dados diferente do
                ``correlation_id``), ``saga_inexistente`` (OS sem saga, ou saga
                sem OS), ``ordem_encerrada`` (OS cancelada ou entregue com a
                saga viva) e ``transicao_invalida`` (a OS ou a saga recusam o
                fato).
        """
        saga, ordem = self._ler(evento)
        etapa = saga.etapa
        contexto = {"correlation_id": str(saga.ordem_id), "tipo": evento.tipo}
        # Os marcos de antes do fato: a saga confere a mesma classificacao.
        marcos = MarcosDaOrdem.da_ordem(ordem)
        classificacao = saga.classificar(
            evento.tipo, marcos, causation_id=evento.causation_id
        )
        if classificacao is Classificacao.ADIANTADO:
            _log.warning("saga event ahead", etapa=etapa.value, **contexto)
            raise EventoAdiantadoError(etapa)
        if classificacao is Classificacao.ORDEM_ENCERRADA:
            # Estado que o cancelamento recusa: nunca comando para OS encerrada,
            # e o alerta da DLQ o mostra (o pagamento confirmado inclusive).
            raise EventoRecusadoError("ordem_encerrada", etapa)
        if classificacao is Classificacao.ESTORNO_AUTOMATICO:
            return self._estorno_automatico(ordem, etapa, contexto)
        if classificacao is not Classificacao.PROCESSAR:
            return _ignorado(classificacao, etapa, contexto)
        self._aplicar(evento, saga, ordem, marcos)
        self._ordens.salvar(ordem)
        self._sagas.salvar(saga)
        _log.info(
            "saga transition",
            etapa=etapa.value,
            etapa_nova=saga.etapa.value,
            status=ordem.status.value,
            motivo=saga.motivo,
            **contexto,
        )
        return Tratamento(Desfecho.PROCESSADA, etapa, saga.etapa)

    def _ler(self, evento: MensagemRecebida) -> tuple[Saga, OrdemDeServico]:
        """Saga e OS do ``correlation_id``, conferido contra o ``ordem_id``."""
        ordem_id = evento.correlation_id
        if UUID(evento.dados["ordem_id"]) != ordem_id:
            raise EventoRecusadoError("ordem_id_divergente")
        saga = self._sagas.obter(ordem_id)
        ordem = self._ordens.obter_por_id(ordem_id)
        if saga is None or ordem is None:
            raise EventoRecusadoError("saga_inexistente")
        return saga, ordem

    def _estorno_automatico(
        self, ordem: OrdemDeServico, etapa: EtapaSaga, contexto: dict[str, str]
    ) -> Tratamento:
        """``PagamentoEstornado`` que nao responde a compensacao (RFC-004 4.4).

        O Billing estornou sozinho a aprovacao que chegou depois do
        cancelamento, da expiracao ou da recusa (ADR-040): a etapa nao muda e
        nenhum comando sai; so o resumo do pagamento passa a estornado, se a OS
        o tiver (a metrica e a do Billing).
        """
        resumo = ordem.resumo_pagamento
        if resumo is None or resumo.status is StatusPagamento.ESTORNADO:
            return _ignorado(Classificacao.ESTORNO_AUTOMATICO, etapa, contexto)
        ordem.registrar_status_do_pagamento(
            StatusPagamento.ESTORNADO, ator=ATOR_CONSUMIDOR
        )
        self._ordens.salvar(ordem)
        _log.info("late payment refunded", etapa=etapa.value, **contexto)
        return Tratamento(Desfecho.PROCESSADA, etapa, etapa)

    def _aplicar(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        marcos: MarcosDaOrdem,
    ) -> None:
        """A linha do evento: fato na OS, comando na outbox e registro na saga."""
        aplicar = _LINHAS[evento.tipo]
        agora = self._instante_do_passo(evento, saga)
        try:
            pedido = aplicar(evento, saga, ordem)
            envio = self._enviar(pedido, evento, agora) if pedido else None
            saga.avancar(evento, marcos, agora=agora, ator=_ator(evento), envio=envio)
        except _RECUSAS_DO_DOMINIO as exc:
            raise EventoRecusadoError("transicao_invalida", saga.etapa) from exc

    def _instante_do_passo(self, evento: MensagemRecebida, saga: Saga) -> datetime:
        """O relogio, sem ficar antes do ultimo registro da saga.

        O registro anterior pode vir de outra replica (ou da API) com o relogio
        uns milissegundos a frente, e a saga recusaria o novo como instante que
        volta: ele fica no instante do anterior. Passando de 1 s o aviso mostra a
        defasagem, e passando de 60 s o evento vai para a DLQ: relogio fora de
        hora gravaria ``em`` e prazos no futuro.

        Raises:
            EventoRecusadoError: ``relogio_defasado``.
        """
        agora = self._relogio()
        defasagem = saga.atualizada_em - agora
        if defasagem > _DEFASAGEM_SEM_AVISO:
            _log.warning(
                "saga clock behind last record",
                etapa=saga.etapa.value,
                defasagem_segundos=defasagem.total_seconds(),
                correlation_id=str(saga.ordem_id),
                tipo=evento.tipo,
            )
        if defasagem > _DEFASAGEM_MAXIMA:
            raise EventoRecusadoError("relogio_defasado", saga.etapa)
        return max(agora, saga.atualizada_em)

    def _enviar(
        self,
        pedido: tuple[Comando, DadosDoContrato],
        evento: MensagemRecebida,
        agora: datetime,
    ) -> Envio:
        """Grava o comando na outbox, causado pelo ``evento`` (RFC-004 secao 5.2).

        Todo comando que um evento envia tem resposta automatica: sai com o
        prazo tecnico (RFC-004 secao 4.6), que a saga confere contra a linha.
        """
        tipo, dados = pedido
        comando_id = self._publicador.publicar_comando(
            tipo,
            dados,
            correlation_id=evento.correlation_id,
            causation_id=evento.id,
        )
        return Envio(
            tipo=tipo,
            id=comando_id,
            dados=dados,
            prazo_resposta_em=agora + self._prazo_resposta,
        )


def dados_da_compensacao(
    comando: Comando, ordem: OrdemDeServico, motivo: str | None
) -> DadosDoContrato:
    """``{ordem_id, motivo}`` e o id do recurso quando a OS ja o conhece.

    O ``CancelarOrcamento`` leva o ``orcamento_id`` e o ``EstornarPagamento``, o
    ``pagamento_id``, dos resumos da OS; sem eles (o passo estava em voo), o
    participante acha o recurso pelo ``ordem_id`` (RFC-004 secao 5.3).
    """
    dados = {"ordem_id": str(ordem.id), "motivo": motivo}
    orcamento, pagamento = ordem.resumo_orcamento, ordem.resumo_pagamento
    if comando is Comando.CANCELAR_ORCAMENTO and orcamento is not None:
        dados["orcamento_id"] = str(orcamento.orcamento_id)
    if comando is Comando.ESTORNAR_PAGAMENTO and pagamento is not None:
        dados["pagamento_id"] = str(pagamento.pagamento_id)
    return dados


def _ignorado(
    classificacao: Classificacao, etapa: EtapaSaga, contexto: dict[str, str]
) -> Tratamento:
    """Evento sem efeito, com log da classificacao.

    O incoerente sai em erro: o inicio da execucao com outra compensacao
    pendente contradiz o que a saga sabe (RFC-004 secao 4.5).
    """
    registrar = _log.error if classificacao is Classificacao.INCOERENTE else _log.info
    registrar(
        "saga event ignored",
        etapa=etapa.value,
        classificacao=classificacao.value,
        **contexto,
    )
    return Tratamento(Desfecho.IGNORADA, etapa, etapa)


def _ator(evento: MensagemRecebida) -> str:
    """Quem provoca o passo: o consumidor, ou o atendente que registrou a decisao.

    A decisao do orcamento registrada pelo atendente em nome do cliente leva o
    sub dele (RFC-004 secao 8).
    """
    decisao = evento.tipo in {"OrcamentoAprovado", "OrcamentoRecusado"}
    if decisao and evento.dados["canal"] == "atendente":
        return str(evento.dados["decidido_por"])
    return ATOR_CONSUMIDOR


def _compensacao(transicao: Transicao, ordem: OrdemDeServico) -> _Pedido:
    """A compensacao que a ``transicao`` envia, com os ``dados`` dela."""
    if transicao.comando is None:
        return None
    dados = dados_da_compensacao(transicao.comando, ordem, transicao.motivo)
    return transicao.comando, dados


# ----- uma linha por evento: o fato na OS e o comando que ela envia (a saga
# confere tipo, dados e prazo). O fluxo normal segue a tabela da RFC-004
# secao 4.1; a compensacao, a secao 4.4.


def _diagnostico_iniciado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    ordem.registrar_diagnostico_iniciado(ator=ATOR_CONSUMIDOR)
    return None


def _diagnostico_concluido(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    itens = itens_do_diagnostico(evento.dados)
    return Comando.GERAR_ORCAMENTO, {"ordem_id": str(saga.ordem_id), "itens": itens}


def _orcamento_gerado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    ordem.registrar_orcamento_gerado(
        resumo_do_orcamento(evento.dados), ator=ATOR_CONSUMIDOR
    )
    return None


def _orcamento_aprovado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    pecas = pecas_dos_itens(saga.itens)
    return Comando.RESERVAR_PECAS, {"ordem_id": str(saga.ordem_id), "pecas": pecas}


def _pecas_reservadas(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
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
) -> _Pedido:
    ordem.registrar_pagamento_solicitado(resumo_do_pagamento(evento.dados))
    return None


def _pagamento_confirmado(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    ordem.registrar_status_do_pagamento(
        StatusPagamento.CONFIRMADO, ator=ATOR_CONSUMIDOR
    )
    return Comando.AGENDAR_EXECUCAO, {
        "ordem_id": str(saga.ordem_id),
        "prioridade": PRIORIDADE_DO_AGENDAMENTO,
    }


def _execucao_agendada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    return None


def _execucao_iniciada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    # No fluxo e na corrida do pivot: a OS descarta o pedido de cancelamento.
    ordem.registrar_execucao_iniciada(ator=ATOR_CONSUMIDOR)
    return None


def _execucao_finalizada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    ordem.finalizar(ator=ATOR_CONSUMIDOR)
    return None


def _falha_de_negocio(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    """A compensacao comeca pela primeira do plano (RFC-004 secao 4.4).

    O pagamento recusado ou expirado tambem passa ao resumo da OS.
    """
    status = _STATUS_DO_PAGAMENTO.get(evento.tipo)
    if status is not None:
        ordem.registrar_status_do_pagamento(status, ator=ATOR_CONSUMIDOR)
    return _compensacao(inicio(saga, MOTIVO_DA_FALHA[evento.tipo]), ordem)


def _resposta_da_compensacao(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    """A seguinte do plano sai; depois da ultima, a OS e cancelada.

    O cancelamento e o estorno do pagamento passam ao resumo da OS, quando ela
    o tem. A OS cancelada leva o texto do pedido do atendente ou, na falha de
    negocio, o rotulo do motivo (RFC-004 secao 4.4).
    """
    status = _STATUS_DO_PAGAMENTO.get(evento.tipo)
    if status is not None and ordem.resumo_pagamento is not None:
        ordem.registrar_status_do_pagamento(status, ator=ATOR_CONSUMIDOR)
    transicao = resposta(saga, evento.tipo)
    if transicao.seguinte is EtapaSaga.COMPENSADA:
        # Em compensacao a saga sempre tem o motivo (``conferir_estado``).
        rotulo = ROTULO_DO_MOTIVO[MotivoCompensacao(cast("str", saga.motivo))]
        motivo = ordem.motivo_cancelamento or rotulo
        ordem.cancelar(motivo, OrigemMudanca.SAGA, ator=ATOR_CONSUMIDOR)
    return _compensacao(transicao, ordem)


_LINHAS: Final[Mapping[str, _Aplicacao]] = MappingProxyType(
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
        **dict.fromkeys(MOTIVO_DA_FALHA, _falha_de_negocio),
        **dict.fromkeys(RESPOSTA_DA_COMPENSACAO, _resposta_da_compensacao),
    }
)
