"""Orquestrador da saga: trata os eventos de ``os.eventos`` (RFC-004 secao 4).

Cada evento roda na transacao da mensagem: le saga e OS, classifica o evento
pela etapa ANTES de tocar no dominio (RFC-004 secao 4.5), aplica o fato na OS,
avanca a saga e grava o comando seguinte na outbox. Quem comita e o consumidor,
junto com ``mensagens_processadas`` (ADR-036): etapa, status, passo e comando
entram no mesmo commit ou nao entram.

Evento de etapa ja passada, repetido ou com a saga fora do fluxo e ignorado com
log; o adiantado volta pela fila de retry (``EventoAdiantadoError``). As falhas
de negocio e as respostas de compensacao sao classificadas da mesma forma, mas,
enquanto as compensacoes nao chegam ao orquestrador, a que caberia tratar
tambem e ignorada com log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog

from src.compartilhado.aplicacao.mensageria import (
    Comando,
    ContratoInvalidoError,
    Desfecho,
    FalhaTransitoriaError,
)
from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException
from src.ordem_servico.aplicacao.saga.modelo import (
    Envio,
    EtapaSaga,
    SagaNaoEncontradaException,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    COMANDOS_COM_PRAZO,
    Classificacao,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import timedelta

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.compartilhado.aplicacao.unit_of_work import PublicadorDeComandos
    from src.ordem_servico.aplicacao.ports import SagaRepository
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository

_log = structlog.get_logger(__name__)

# Ator dos fatos que o consumidor aplica (historico da OS e passos da saga).
ATOR_CONSUMIDOR: Final = "consumidor"
# Unica prioridade nesta fase: nenhuma regra escolhe `alta` (RFC-004 secao 5.3).
_PRIORIDADE: Final = "normal"


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


type _Aplicacao = Callable[
    [OrquestradorDaSaga, MensagemRecebida, Saga, OrdemDeServico, datetime], None
]


class OrquestradorDaSaga:
    """Aplica o fluxo normal na OS e na saga (tabela da RFC-004 secao 4.1)."""

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
            SagaNaoEncontradaException: ``ordem_id`` sem saga (permanente, DLQ).
            ContratoInvalidoError: ``ordem_id`` dos dados diferente do
                ``correlation_id`` (permanente, DLQ).
        """
        ordem_id = evento.correlation_id
        if UUID(evento.dados["ordem_id"]) != ordem_id:
            msg = "ordem_id dos dados diverge do correlation_id"
            raise ContratoInvalidoError(msg, caminho="$.dados.ordem_id")
        saga = self._sagas.obter(ordem_id)
        ordem = self._ordens.obter_por_id(ordem_id)
        if saga is None or ordem is None:
            raise SagaNaoEncontradaException(ordem_id)
        etapa = saga.etapa
        contexto = {"correlation_id": str(ordem_id), "tipo": evento.tipo}
        classificacao = saga.classificar(evento.tipo, ordem)
        if classificacao is Classificacao.ADIANTADO:
            _log.warning("saga event ahead", etapa=etapa.value, **contexto)
            raise EventoAdiantadoError(etapa)
        aplicar = _FLUXO_NORMAL.get(evento.tipo)
        if classificacao is not Classificacao.PROCESSAR or aplicar is None:
            # Falha de negocio ou resposta de compensacao na etapa em que caberia
            # tratar: a compensacao ainda nao esta no orquestrador.
            motivo = (
                "sem_compensacao"
                if classificacao is Classificacao.PROCESSAR
                else classificacao.value
            )
            _log.info(
                "saga event ignored",
                etapa=etapa.value,
                classificacao=motivo,
                **contexto,
            )
            return Tratamento(Desfecho.IGNORADA, etapa, etapa)
        aplicar(self, evento, saga, ordem, self._relogio())
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

    def _enviar(
        self,
        tipo: Comando,
        dados: Mapping[str, Any],
        evento: MensagemRecebida,
        agora: datetime,
    ) -> Envio:
        """Grava o comando na outbox, causado pelo ``evento`` (RFC-004 secao 5.2)."""
        comando_id = self._publicador.publicar_comando(
            tipo,
            dados,
            correlation_id=evento.correlation_id,
            causation_id=evento.id,
        )
        prazo = agora + self._prazo_resposta if tipo in COMANDOS_COM_PRAZO else None
        return Envio(tipo=tipo, id=comando_id, dados=dados, prazo_resposta_em=prazo)

    # ----- uma linha da tabela da RFC-004 secao 4.1 por evento

    def _diagnostico_iniciado(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        ordem.registrar_diagnostico_iniciado(ator=ATOR_CONSUMIDOR)
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR)

    def _diagnostico_concluido(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        dados = {
            "ordem_id": str(saga.ordem_id),
            "itens": itens_do_diagnostico(evento.dados),
        }
        envio = self._enviar(Comando.GERAR_ORCAMENTO, dados, evento, agora)
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR, envio=envio)

    def _orcamento_gerado(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        dados = evento.dados
        ordem.registrar_orcamento_gerado(
            orcamento_id=UUID(dados["orcamento_id"]),
            total=Dinheiro(valor=Decimal(dados["total"]), moeda=dados["moeda"]),
            link_decisao=dados["link_decisao"],
            valido_ate=datetime.fromisoformat(dados["valido_ate"]),
            ator=ATOR_CONSUMIDOR,
        )
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR)

    def _orcamento_aprovado(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        # Decisao registrada pelo atendente em nome do cliente: o passo leva o
        # sub dele (RFC-004 secao 8).
        ator = (
            evento.dados["decidido_por"]
            if evento.dados["canal"] == "atendente"
            else ATOR_CONSUMIDOR
        )
        pecas = [
            {"sku": item["codigo"], "quantidade": item["quantidade"]}
            for item in saga.itens
            if item["tipo"] == "peca"
        ]
        dados = {"ordem_id": str(saga.ordem_id), "pecas": pecas}
        envio = self._enviar(Comando.RESERVAR_PECAS, dados, evento, agora)
        saga.avancar(evento, agora=agora, ator=ator, envio=envio)

    def _pecas_reservadas(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        ordem.registrar_pecas_reservadas(ator=ATOR_CONSUMIDOR)
        orcamento = ordem.resumo_orcamento
        if orcamento is None:
            # A OS so chega a aguardando_aprovacao com o resumo do orcamento.
            msg = "Ordem sem orcamento gerado"
            raise ViolacaoRegraDeNegocioException(msg)
        dados = {
            "ordem_id": str(saga.ordem_id),
            "orcamento_id": str(orcamento.orcamento_id),
        }
        envio = self._enviar(Comando.SOLICITAR_PAGAMENTO, dados, evento, agora)
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR, envio=envio)

    def _pagamento_solicitado(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        dados = evento.dados
        ordem.registrar_pagamento_solicitado(
            pagamento_id=UUID(dados["pagamento_id"]),
            valor=Dinheiro(valor=Decimal(dados["valor"]), moeda=dados["moeda"]),
            checkout_url=dados["checkout_url"],
            expira_em=datetime.fromisoformat(dados["expira_em"]),
        )
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR)

    def _pagamento_confirmado(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        ordem.registrar_pagamento_confirmado(ator=ATOR_CONSUMIDOR)
        dados = {"ordem_id": str(saga.ordem_id), "prioridade": _PRIORIDADE}
        envio = self._enviar(Comando.AGENDAR_EXECUCAO, dados, evento, agora)
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR, envio=envio)

    def _execucao_agendada(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR)

    def _execucao_iniciada(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        ordem.registrar_execucao_iniciada(ator=ATOR_CONSUMIDOR)
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR)

    def _execucao_finalizada(
        self,
        evento: MensagemRecebida,
        saga: Saga,
        ordem: OrdemDeServico,
        agora: datetime,
    ) -> None:
        ordem.finalizar(ator=ATOR_CONSUMIDOR)
        saga.avancar(evento, agora=agora, ator=ATOR_CONSUMIDOR)


_FLUXO_NORMAL: Final[Mapping[str, _Aplicacao]] = {
    "DiagnosticoIniciado": OrquestradorDaSaga._diagnostico_iniciado,
    "DiagnosticoConcluido": OrquestradorDaSaga._diagnostico_concluido,
    "OrcamentoGerado": OrquestradorDaSaga._orcamento_gerado,
    "OrcamentoAprovado": OrquestradorDaSaga._orcamento_aprovado,
    "PecasReservadas": OrquestradorDaSaga._pecas_reservadas,
    "PagamentoSolicitado": OrquestradorDaSaga._pagamento_solicitado,
    "PagamentoConfirmado": OrquestradorDaSaga._pagamento_confirmado,
    "ExecucaoAgendada": OrquestradorDaSaga._execucao_agendada,
    "ExecucaoIniciada": OrquestradorDaSaga._execucao_iniciada,
    "ExecucaoFinalizada": OrquestradorDaSaga._execucao_finalizada,
}
