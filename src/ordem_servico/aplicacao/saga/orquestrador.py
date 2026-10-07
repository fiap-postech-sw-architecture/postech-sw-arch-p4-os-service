"""Orquestrador da saga: trata os eventos de ``os.eventos`` (RFC-004 secao 4).

Cada evento roda na transacao da mensagem: le saga e OS, classifica o evento
pela etapa ANTES de tocar no dominio (RFC-004 secao 4.5), aplica o fato na OS,
avanca a saga e grava o comando seguinte na outbox. Quem comita e o consumidor,
junto com ``mensagens_processadas`` (ADR-036): etapa, status, passo e comando
entram no mesmo commit ou nao entram.

Evento de etapa ja passada, repetido ou com a saga fora do fluxo e ignorado com
log; o adiantado volta pela fila de retry (``EventoAdiantadoError``). O que
nenhuma tentativa resolve vai para a DLQ com o motivo em codigo
(``EventoRecusadoError``): OS sem saga, ``ordem_id`` divergente, OS encerrada
com a saga viva, fato que a OS ou a saga recusam e, enquanto as compensacoes
nao chegam ao orquestrador, a falha de negocio e a resposta de compensacao na
etapa em que caberia trata-las (``sem_tratador_nesta_versao``, para o redrive
na versao que as trata).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
from src.ordem_servico.aplicacao.saga.modelo import (
    DadosDoContrato,
    Envio,
    EtapaSaga,
    TransicaoDaSagaInvalidaError,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.resumos_do_billing import (
    resumo_do_orcamento,
    resumo_do_pagamento,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    Classificacao,
)
from src.ordem_servico.dominio.marcos import MarcosDaOrdem

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import timedelta

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.compartilhado.aplicacao.unit_of_work import PublicadorDeComandos
    from src.ordem_servico.aplicacao.ports import SagaRepository
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository
    from src.ordem_servico.dominio.resumos import ResumoOrcamento

_log = structlog.get_logger(__name__)

# Ator dos fatos que o consumidor aplica (historico da OS e passos da saga).
ATOR_CONSUMIDOR: Final = "consumidor"
# Unica prioridade nesta fase: nenhuma regra escolhe `alta` (RFC-004 secao 5.3).
_PRIORIDADE: Final = "normal"
# O que a OS ou a saga recusam ao aplicar o fato: estado incompativel com o
# evento, que nenhuma nova tentativa muda.
_RECUSAS_DO_DOMINIO: Final = (
    TransicaoStatusInvalidaException,
    ViolacaoRegraDeNegocioException,
    TransicaoDaSagaInvalidaError,
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


# O comando que a linha da tabela envia: tipo e ``dados`` (sem ele, ``None``).
type _Pedido = tuple[Comando, DadosDoContrato] | None
type _Aplicacao = Callable[[MensagemRecebida, Saga, OrdemDeServico], _Pedido]


class OrquestradorDaSaga:
    """Aplica o fluxo normal na OS e na saga (tabela da RFC-004 secao 4.1).

    Le saga e OS, classifica o evento e traduz cada linha da tabela em
    chamadas: o fato na OS, o comando na outbox e o passo na saga, que confere
    etapa, comando, dados e prazo antes de mudar.
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
                saga viva), ``sem_tratador_nesta_versao`` (falha de negocio ou
                resposta de compensacao na etapa em que caberia trata-la) e
                ``transicao_invalida`` (a OS ou a saga recusam o fato).
        """
        saga, ordem = self._ler(evento)
        etapa = saga.etapa
        contexto = {"correlation_id": str(saga.ordem_id), "tipo": evento.tipo}
        # Os marcos de antes do fato: a saga confere a mesma classificacao.
        marcos = MarcosDaOrdem.da_ordem(ordem)
        classificacao = saga.classificar(evento.tipo, marcos)
        if classificacao is Classificacao.ADIANTADO:
            _log.warning("saga event ahead", etapa=etapa.value, **contexto)
            raise EventoAdiantadoError(etapa)
        if classificacao is Classificacao.ORDEM_ENCERRADA:
            # Estado que o cancelamento recusa: nunca comando para OS encerrada,
            # e o alerta da DLQ o mostra (o pagamento confirmado inclusive).
            raise EventoRecusadoError("ordem_encerrada", etapa)
        if classificacao is not Classificacao.PROCESSAR:
            _log.info(
                "saga event ignored",
                etapa=etapa.value,
                classificacao=classificacao.value,
                **contexto,
            )
            return Tratamento(Desfecho.IGNORADA, etapa, etapa)
        self._aplicar(evento, saga, ordem, marcos)
        self._ordens.salvar(ordem)
        self._sagas.salvar(saga)
        _log.info(
            "saga transition",
            etapa=etapa.value,
            etapa_nova=saga.etapa.value,
            status=ordem.status.value,
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

    def _aplicar(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        marcos: MarcosDaOrdem,
    ) -> None:
        """A linha do evento: fato na OS, comando na outbox e passo na saga."""
        aplicar = _FLUXO_NORMAL.get(evento.tipo)
        if aplicar is None:
            # Falha de negocio ou resposta de compensacao na etapa em que caberia
            # trata-la: consumida agora, ela se perderia; na DLQ, espera o redrive
            # da versao com as compensacoes.
            raise EventoRecusadoError("sem_tratador_nesta_versao", saga.etapa)
        # O registro anterior pode vir de outra replica (ou da API), com o
        # relogio uns milissegundos a frente: o novo nunca fica antes dele, que
        # a saga recusaria como instante que volta.
        agora = max(self._relogio(), saga.atualizada_em)
        try:
            pedido = aplicar(evento, saga, ordem)
            envio = self._enviar(pedido, evento, agora) if pedido else None
            saga.avancar(evento, marcos, agora=agora, ator=_ator(evento), envio=envio)
        except _RECUSAS_DO_DOMINIO as exc:
            raise EventoRecusadoError("transicao_invalida", saga.etapa) from exc

    def _enviar(
        self,
        pedido: tuple[Comando, DadosDoContrato],
        evento: MensagemRecebida,
        agora: datetime,
    ) -> Envio:
        """Grava o comando na outbox, causado pelo ``evento`` (RFC-004 secao 5.2).

        Todo comando do fluxo normal tem resposta automatica: sai com o prazo
        tecnico (RFC-004 secao 4.6), que a saga confere contra a tabela.
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


def _ator(evento: MensagemRecebida) -> str:
    """Quem provoca o passo: o consumidor, ou o atendente que registrou a decisao.

    A decisao do orcamento registrada pelo atendente em nome do cliente leva o
    sub dele (RFC-004 secao 8).
    """
    if evento.tipo == "OrcamentoAprovado" and evento.dados["canal"] == "atendente":
        return str(evento.dados["decidido_por"])
    return ATOR_CONSUMIDOR


# ----- uma linha da tabela da RFC-004 secao 4.1 por evento: o fato na OS e o
# comando que a linha envia (a saga confere tipo, dados e prazo)


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
    return Comando.RESERVAR_PECAS, {"ordem_id": str(saga.ordem_id), "pecas": saga.pecas}


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
    ordem.registrar_pagamento_confirmado(ator=ATOR_CONSUMIDOR)
    return Comando.AGENDAR_EXECUCAO, {
        "ordem_id": str(saga.ordem_id),
        "prioridade": _PRIORIDADE,
    }


def _execucao_agendada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    return None


def _execucao_iniciada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    ordem.registrar_execucao_iniciada(ator=ATOR_CONSUMIDOR)
    return None


def _execucao_finalizada(
    evento: MensagemRecebida, saga: Saga, ordem: OrdemDeServico
) -> _Pedido:
    ordem.finalizar(ator=ATOR_CONSUMIDOR)
    return None


_FLUXO_NORMAL: Final[Mapping[str, _Aplicacao]] = {
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
