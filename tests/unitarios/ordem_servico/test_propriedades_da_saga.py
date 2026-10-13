"""Propriedade: fora de ordem, repeticao e compensacao nunca levam a estado invalido.

Billing e Execucao simulados (stdlib ``random``, sementes fixas) respondem a
cada comando que o orquestrador real publica, como maquinas de estado: o
diagnostico, o orcamento, a reserva, o pagamento e a execucao de cada um, com
as decisoes humanas (mecanico e cliente) num instante sorteado depois do
comando que as habilita. O roteiro de cada semente escolhe uma falha de negocio
(ou nenhuma), se o provedor recusa o estorno e se o cliente paga depois de a
cobranca ser cancelada, expirada ou recusada (o Billing estorna sozinho,
ADR-040), e em parte delas o estorno automatico chega ao consumidor antes da
resposta ao ``EstornarPagamento`` (duas replicas entregando em ordem trocada).
A compensacao que chega antes do fato humano o anula (a lapide da
RFC-004 secao 4.5), e o ``CancelarExecucao`` depois do inicio e ignorado sem
resposta.

Cada evento e entregue com atraso aleatorio: dentro da janela os eventos
chegam embaralhados, alguns sao republicados com id novo (o participante
respondendo a um comando repetido) e alguns atrasam bem mais. O adiantado
volta como a copia da fila de retry, depois do atraso do nivel (1, 5, 15, 60 e
300 s), e a falha seguinte a quinta copia seria a DLQ (RFC-004 secao 5.1). Em
pontos aleatorios o atendente pede o cancelamento (pela saga: antes do pivot
ela compensa, depois e recusado) ou registra a entrega da OS.

A cada entrega: so ``FalhaTransitoriaError`` escapa do handler; o par (etapa,
status da OS) esta na tabela da RFC-004 secao 4.1; a OS fica cancelada se e so
se a saga esta compensada; ha prazo se e so se ha comando com prazo em voo
(parada no estorno recusado, o comando fica em voo, sem prazo); a compensacao
seguinte so sai depois da resposta da anterior, na ordem do plano (o estorno
nunca antes da execucao cancelada); o fato ja aplicado nao muda nada; nenhum
comando sai depois do estado final; o plano de cada compensacao e o da tabela
da RFC-004 secao 4.4 para o que a saga ja tinha feito e o motivo.
No fim: DLQ vazia e a saga concluida (pelo caminho feliz ou pela corrida do
pivot que o inicio venceu), compensada pelo plano inteiro ou parada no estorno
recusado.
"""

from __future__ import annotations

import heapq
import itertools
import random
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import pytest

from src.compartilhado.aplicacao.mensageria import Desfecho, FalhaTransitoriaError
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import COMANDOS_COM_PRAZO
from src.ordem_servico.aplicacao.use_cases import RegistrarEntrega
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS, StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE
from tests.unitarios.fakes import FakeUnitOfWork
from tests.unitarios.ordem_servico.cenario_da_saga import (
    COMPENSACOES,
    FEITO,
    FLUXO_FELIZ,
    PLANO_DA_RFC,
    CenarioDaSaga,
)

if TYPE_CHECKING:
    from src.compartilhado.aplicacao.mensageria import MensagemRecebida

S = StatusOrdem
E = EtapaSaga
# Cerca de mil execucoes, deterministicas.
SEMENTES: Final = range(1000)
ATRASOS_DE_RETRY_S: Final = (1, 5, 15, 60, 300)
# Falhas de negocio do roteiro (None: caminho feliz), cada uma com o motivo da
# compensacao e o plano da RFC-004 secao 4.4.
FALHAS: Final = (
    None,
    "GeracaoDeOrcamentoFalhou",
    "OrcamentoRecusado",
    "OrcamentoExpirado",
    "ReservaDePecasFalhou",
    "PagamentoRecusado",
    "PagamentoExpirado",
)
# O perfil de ``FEITO`` (o que a saga ja fez) de cada estado da saga.
PERFIL_DO_FEITO: Final = {feito: perfil for perfil, feito in FEITO.items()}
# A resposta de cada compensacao.
RESPOSTA: Final = {
    "CancelarExecucao": "ExecucaoCancelada",
    "EstornarPagamento": ("PagamentoCancelado", "PagamentoEstornado"),
    "LiberarReserva": "ReservaLiberada",
    "CancelarOrcamento": "OrcamentoCancelado",
    "DescartarDiagnostico": "DiagnosticoDescartado",
}
# Entre duas decisoes humanas; atraso de entrega (a janela que embaralha);
# chance de republicar com id novo e de atrasar muito uma entrega.
ENTRE_ACOES_S: Final = 1.0
JANELA_S: Final = 2.0
CHANCE_DE_REPETIR: Final = 0.3
CHANCE_DE_ATRASAR: Final = 0.1
ATRASO_LONGO_S: Final = 30.0
# Roteiro: chance de o provedor recusar o estorno e de o cliente pagar depois
# de a cobranca encerrar; chance de o atendente tentar cancelar ou entregar a
# OS antes de uma entrega.
CHANCE_DE_ESTORNO_RECUSADO: Final = 0.3
CHANCE_DE_APROVACAO_TARDIA: Final = 0.5
# Chance de o estorno automatico chegar antes da resposta ao EstornarPagamento.
CHANCE_DE_INVERTER_O_ESTORNO: Final = 0.5
CHANCE_DE_ENCERRAR: Final = 0.05
# Status da OS em cada etapa (tabela da RFC-004 secao 4.1): na compensacao, o
# de antes do pivot em que ela comecou.
ANTES_DO_PIVOT: Final = {
    S.RECEBIDA,
    S.EM_DIAGNOSTICO,
    S.AGUARDANDO_APROVACAO,
    S.AGUARDANDO_PAGAMENTO,
    S.AGUARDANDO_EXECUCAO,
}
PARES: Final = {
    E.AGUARDANDO_DIAGNOSTICO: {S.RECEBIDA, S.EM_DIAGNOSTICO},
    E.AGUARDANDO_ORCAMENTO: {S.EM_DIAGNOSTICO},
    E.AGUARDANDO_DECISAO: {S.AGUARDANDO_APROVACAO},
    E.AGUARDANDO_RESERVA: {S.AGUARDANDO_APROVACAO},
    E.AGUARDANDO_PAGAMENTO: {S.AGUARDANDO_PAGAMENTO},
    E.AGUARDANDO_AGENDAMENTO: {S.AGUARDANDO_EXECUCAO},
    E.AGUARDANDO_INICIO: {S.AGUARDANDO_EXECUCAO},
    E.EM_EXECUCAO: {S.EM_EXECUCAO},
    E.CONCLUIDA: {S.FINALIZADA, S.ENTREGUE},
    E.COMPENSANDO: ANTES_DO_PIVOT,
    E.FALHA_NA_COMPENSACAO: ANTES_DO_PIVOT,
    E.COMPENSADA: {S.CANCELADA},
}

# (instante, ordem, acao) na agenda: a decisao humana que o participante toma
# (``Decisao``) ou a entrega de um evento ao consumidor do OS (``Entrega``).


@dataclass(frozen=True)
class Decisao:
    """Fato humano que o participante publica no instante, se nada o anulou."""

    tipo: str
    causa: UUID


@dataclass(frozen=True)
class Entrega:
    mensagem: MensagemRecebida
    copias: int = 0


@dataclass
class Participantes:
    """Billing e Execucao: o estado de cada recurso da OS, como os servicos o veem."""

    roteiro: Roteiro
    diagnostico: str = "aguardando"
    orcamento: str | None = None
    reserva: str | None = None
    pagamento: str | None = None
    execucao: str | None = None
    # Desfecho do pagamento que o cliente produz e o id do SolicitarPagamento.
    pago_por: UUID | None = None


@dataclass(frozen=True)
class Roteiro:
    falha: str | None
    estorno_recusado: bool
    aprovacao_tardia: bool
    inverte_o_estorno: bool


@dataclass
class Simulacao:
    """Agenda de decisoes e entregas, em ordem de tempo."""

    cenario: CenarioDaSaga
    rnd: random.Random
    roteiro: Roteiro
    agenda: list[tuple[float, int, Decisao | Entrega]] = field(default_factory=list)
    sequencia: itertools.count[int] = field(default_factory=itertools.count)
    participantes: Participantes = field(init=False)
    entregas: list[str] = field(default_factory=list)
    # Como cada entrega se classificou na etapa em que chegou.
    classificacoes: Counter[str] = field(default_factory=Counter)
    dlq: list[str] = field(default_factory=list)
    # Respostas casadas do cancelamento que chegaram com o resumo ja estornado.
    cancelados_apos_o_estorno: int = 0
    # (acao, etapa da saga na tentativa, se passou)
    encerramentos: list[tuple[str, EtapaSaga, bool]] = field(default_factory=list)
    # A compensacao, quando comecou: (perfil do que a saga ja tinha feito,
    # motivo, plano).
    compensacao: tuple[str, str, tuple[str, ...]] | None = None
    enviados: int = 0
    # Eventos processados: (tipo, causation_id) -> id, e id -> tipo.
    aplicados: dict[tuple[str, UUID | None], UUID] = field(default_factory=dict)
    tipo_do_aplicado: dict[UUID, str] = field(default_factory=dict)
    agora: float = 0.0

    def __post_init__(self) -> None:
        self.participantes = Participantes(self.roteiro)

    def agendar(self, instante: float, acao: Decisao | Entrega) -> None:
        heapq.heappush(self.agenda, (instante, next(self.sequencia), acao))

    def publicar(
        self,
        tipo: str,
        causa: UUID,
        *,
        janela: tuple[float, float] = (0, JANELA_S),
        **dados: Any,
    ) -> None:
        """O participante publica: entrega com atraso, as vezes repetida.

        ``janela`` e o atraso de entrega, em segundos depois de agora; so uma
        janela dada (o estorno que passa a frente) dispensa o atraso longo.
        """
        mensagem = evento(tipo, self.cenario.ordem_id, causation_id=causa, **dados)
        entrega = self.agora + self.rnd.uniform(*janela)
        if janela[0] == 0 and self.rnd.random() < CHANCE_DE_ATRASAR:
            entrega += self.rnd.uniform(0, ATRASO_LONGO_S)
        self.agendar(entrega, Entrega(mensagem))
        if self.rnd.random() < CHANCE_DE_REPETIR:
            # Republicado com id novo e a mesma causa: chega depois do original.
            repetida = evento(tipo, self.cenario.ordem_id, causation_id=causa, **dados)
            atraso = entrega + self.rnd.uniform(0, ATRASO_LONGO_S)
            self.agendar(atraso, Entrega(repetida))

    def decidir(self, tipo: str, causa: UUID) -> None:
        """Agenda a decisao humana num instante depois de agora."""
        self.agendar(
            self.agora + self.rnd.uniform(0, ENTRE_ACOES_S), Decisao(tipo, causa)
        )

    def receber_comando(self, tipo: str, comando_id: UUID) -> None:
        """O participante trata o comando na hora em que ele e publicado."""
        p, falha = self.participantes, self.roteiro.falha
        match tipo:
            case "SolicitarDiagnostico":
                self.decidir("DiagnosticoIniciado", comando_id)
            case "GerarOrcamento" if falha == "GeracaoDeOrcamentoFalhou":
                p.orcamento = "falhou"
                self.publicar("GeracaoDeOrcamentoFalhou", comando_id)
            case "GerarOrcamento":
                p.orcamento = "pendente"
                self.publicar("OrcamentoGerado", comando_id)
                recusa = falha in {"OrcamentoRecusado", "OrcamentoExpirado"}
                self.decidir(
                    falha if recusa and falha else "OrcamentoAprovado", comando_id
                )
            case "ReservarPecas" if falha == "ReservaDePecasFalhou":
                p.reserva = "recusada"
                self.publicar("ReservaDePecasFalhou", comando_id)
            case "ReservarPecas":
                p.reserva = "ativa"
                self.publicar("PecasReservadas", comando_id)
            case "SolicitarPagamento":
                p.pagamento, p.pago_por = "solicitado", comando_id
                self.publicar("PagamentoSolicitado", comando_id)
                if self.roteiro.inverte_o_estorno:
                    # O atendente cancela com o checkout aberto: a compensacao
                    # tem o EstornarPagamento, que e onde a ordem se inverte.
                    self.decidir("Cancelamento", comando_id)
                recusa = falha in {"PagamentoRecusado", "PagamentoExpirado"}
                self.decidir(
                    falha if recusa and falha else "PagamentoConfirmado", comando_id
                )
            case "AgendarExecucao":
                p.execucao = "aguardando"
                self.publicar("ExecucaoAgendada", comando_id)
                self.decidir("ExecucaoIniciada", comando_id)
            case _:
                self.compensar(tipo, comando_id)

    def compensar(self, tipo: str, comando_id: UUID) -> None:
        """Compensacao: desfaz e responde; a que chega antes do fato e lapide."""
        p = self.participantes
        match tipo:
            case "DescartarDiagnostico":
                p.diagnostico = "descartado"
                self.publicar("DiagnosticoDescartado", comando_id)
            case "CancelarOrcamento":
                p.orcamento = "cancelado"
                self.publicar("OrcamentoCancelado", comando_id)
            case "LiberarReserva":
                p.reserva = "liberada"
                self.publicar("ReservaLiberada", comando_id)
            case "EstornarPagamento":
                self.estornar(comando_id)
            case "CancelarExecucao" if p.execucao in {None, "aguardando", "cancelada"}:
                p.execucao = "cancelada"
                self.publicar("ExecucaoCancelada", comando_id)
            case "CancelarExecucao":
                # Execucao ja iniciada: descompasso de estado, sem resposta.
                pass

    def estornar(self, comando_id: UUID) -> None:
        """``EstornarPagamento`` em cada estado do pagamento (RFC-004 secao 4.4)."""
        p = self.participantes
        if p.pagamento == "confirmado" and self.roteiro.estorno_recusado:
            self.publicar("EstornoDePagamentoFalhou", comando_id)
        elif p.pagamento in {"confirmado", "estornado"}:
            p.pagamento = "estornado"
            self.publicar("PagamentoEstornado", comando_id, motivo="compensacao")
        elif self.roteiro.inverte_o_estorno and p.pago_por is not None:
            self.estorno_automatico_passa_a_frente(comando_id, p.pago_por)
        else:
            # Solicitado (ou em voo), ja cancelado, recusado ou expirado: a
            # cobranca fecha sem nada a devolver.
            p.pagamento = "cancelado"
            self.publicar("PagamentoCancelado", comando_id)

    def estorno_automatico_passa_a_frente(
        self, comando_id: UUID, pago_por: UUID
    ) -> None:
        """A aprovacao tardia ja estornada: o estorno chega antes do cancelamento.

        O Billing cancela a cobranca em resposta ao comando e estorna sozinho a
        aprovacao que chegou, mas duas replicas do consumidor entregam o
        ``PagamentoEstornado`` (causa: o ``SolicitarPagamento``) antes do
        ``PagamentoCancelado`` (causa: o comando), que chega depois da janela
        em que o estorno chega.
        """
        self.participantes.pagamento = "estornado"
        self.publicar(
            "PagamentoEstornado",
            pago_por,
            janela=(0.0, JANELA_S),
            motivo="pagamento_apos_encerramento",
        )
        self.publicar("PagamentoCancelado", comando_id, janela=(JANELA_S, 2 * JANELA_S))

    def tomar_decisao(self, decisao: Decisao) -> None:
        """O fato humano acontece, salvo se uma compensacao chegou antes."""
        p, tipo, causa = self.participantes, decisao.tipo, decisao.causa
        match tipo:
            case "DiagnosticoIniciado" | "DiagnosticoConcluido" if (
                p.diagnostico == "descartado"
            ):
                return
            case "DiagnosticoIniciado":
                self.publicar(tipo, causa)
                self.decidir("DiagnosticoConcluido", causa)
            case "DiagnosticoConcluido":
                self.publicar(tipo, causa)
            case "OrcamentoAprovado" | "OrcamentoRecusado" | "OrcamentoExpirado":
                if p.orcamento == "pendente":
                    p.orcamento = tipo.removeprefix("Orcamento").lower()
                    self.publicar(tipo, causa)
            case "PagamentoConfirmado" | "PagamentoRecusado" | "PagamentoExpirado":
                self.pagar(tipo, causa)
            case "Cancelamento":
                self.tentar_encerrar("cancelar")
            case "PagamentoTardio":
                if p.pagamento in {"cancelado", "recusado", "expirado"}:
                    p.pagamento = "estornado"
                    self.publicar(
                        "PagamentoEstornado",
                        causa,
                        motivo="pagamento_apos_encerramento",
                    )
            case "ExecucaoIniciada" if p.execucao == "aguardando":
                p.execucao = "iniciada"
                self.publicar(tipo, causa)
                self.decidir("ExecucaoFinalizada", causa)
            case "ExecucaoFinalizada":
                p.execucao = "finalizada"
                self.publicar(tipo, causa)

    def pagar(self, tipo: str, causa: UUID) -> None:
        """O cliente paga, ou a cobranca e recusada ou expira (RFC-004 secao 4.6)."""
        p = self.participantes
        if p.pagamento == "solicitado":
            p.pagamento = tipo.removeprefix("Pagamento").lower()
            self.publicar(tipo, causa, **_motivo_do_desfecho(tipo))
            if tipo != "PagamentoConfirmado" and self.roteiro.aprovacao_tardia:
                self.decidir("PagamentoTardio", causa)
        elif tipo == "PagamentoConfirmado" or self.roteiro.aprovacao_tardia:
            # Cobranca ja cancelada pela compensacao: a aprovacao que chega
            # depois e estornada pelo proprio Billing.
            self.decidir("PagamentoTardio", causa)

    def tentar_encerrar(self, acao: str | None = None) -> None:
        """O atendente pede o cancelamento (pela saga) ou registra a entrega."""
        cenario = self.cenario
        acao = acao or self.rnd.choice(("cancelar", "entregar"))
        antes, etapa = _foto(cenario), cenario.saga.etapa
        feito = _feito(cenario)
        try:
            if acao == "cancelar":
                cenario.cancelar()
            else:
                RegistrarEntrega(
                    cenario.ordens, FakeUnitOfWork(), cenario.sagas
                ).executar(cenario.ordem_id, ator=ATOR_ATENDENTE)
        except TransicaoStatusInvalidaException:
            assert _foto(cenario) == antes
            self.encerramentos.append((acao, etapa, False))
        else:
            self.encerramentos.append((acao, etapa, True))
            self.registrar_compensacao(etapa, feito)
        _conferir_estado(cenario)
        self.despachar()

    def registrar_compensacao(
        self, etapa: EtapaSaga, feito: tuple[tuple[str, ...], str | None]
    ) -> None:
        """A compensacao que comecou agora tem o plano da RFC-004 secao 4.4."""
        saga = self.cenario.saga
        if etapa in {E.COMPENSANDO, E.FALHA_NA_COMPENSACAO}:
            return
        if saga.etapa is not E.COMPENSANDO:
            return
        assert self.compensacao is None
        perfil, motivo = PERFIL_DO_FEITO[feito], saga.motivo
        assert motivo is not None
        plano = saga.plano_compensacao
        assert plano == PLANO_DA_RFC[(perfil, motivo)], (perfil, motivo)
        self.compensacao = (perfil, motivo, plano)

    def despachar(self) -> None:
        """Os comandos novos da outbox vao aos participantes, na ordem."""
        comandos = self.cenario.publicador.comandos
        for indice in range(self.enviados, len(comandos)):
            self.conferir_compensacao(indice)
            self.receber_comando(
                comandos[indice][0], _id_do_comando(self.cenario, indice)
            )
        self.enviados = len(comandos)

    def rodar(self) -> None:
        cenario = self.cenario
        self.despachar()
        comandos_no_fim: int | None = None
        while self.agenda:
            if self.rnd.random() < CHANCE_DE_ENCERRAR:
                self.tentar_encerrar()
            self.agora, _, acao = heapq.heappop(self.agenda)
            if isinstance(acao, Decisao):
                self.tomar_decisao(acao)
                continue
            self.entregar(acao)
            _conferir_estado(cenario)
            if comandos_no_fim is not None:
                # Nenhum comando depois do estado final.
                assert len(cenario.publicador.comandos) == comandos_no_fim
            elif cenario.saga.encerrada:
                comandos_no_fim = len(cenario.publicador.comandos)
            self.despachar()

    def entregar(self, entrega: Entrega) -> None:
        """O consumidor do OS trata a mensagem; o adiantado volta pela retry."""
        cenario, mensagem = self.cenario, entrega.mensagem
        self.entregas.append(mensagem.tipo)
        marcos = MarcosDaOrdem.da_ordem(cenario.ordem)
        classificacao = cenario.saga.classificar(
            mensagem.tipo, marcos, causation_id=mensagem.causation_id
        )
        self.classificacoes[classificacao.value] += 1
        resumo = cenario.ordem.resumo_pagamento
        if (
            mensagem.tipo == "PagamentoCancelado"
            and classificacao.value == "processar"
            and resumo is not None
            and resumo.status.value == "estornado"
        ):
            self.cancelados_apos_o_estorno += 1
        antes, etapa, feito = _foto(cenario), cenario.saga.etapa, _feito(cenario)
        try:
            tratamento = cenario.orquestrador.tratar(mensagem)
        except FalhaTransitoriaError:
            assert _foto(cenario) == antes
            if entrega.copias == len(ATRASOS_DE_RETRY_S):
                self.dlq.append(mensagem.tipo)
            else:
                volta = self.agora + ATRASOS_DE_RETRY_S[entrega.copias]
                self.agendar(volta, Entrega(mensagem, entrega.copias + 1))
            return
        chave = (mensagem.tipo, mensagem.causation_id)
        if chave in self.aplicados:
            # O mesmo fato de novo (republicado com id novo) nao muda nada.
            assert tratamento.desfecho is Desfecho.IGNORADA
            assert _foto(cenario) == antes
        if tratamento.desfecho is Desfecho.PROCESSADA:
            self.aplicados[chave] = mensagem.id
            self.tipo_do_aplicado[mensagem.id] = mensagem.tipo
            self.registrar_compensacao(etapa, feito)

    def conferir_compensacao(self, indice: int) -> None:
        """A compensacao sai na ordem do plano, depois da resposta da anterior.

        O ``EstornarPagamento`` nunca sai antes da execucao cancelada (RFC-004
        secao 4.5): a anterior dele no plano e o ``CancelarExecucao``.
        """
        comandos = self.cenario.publicador.comandos
        tipo, _, _, causa = comandos[indice]
        if tipo not in COMPENSACOES:
            return
        anteriores = [c[0] for c in comandos[:indice] if c[0] in COMPENSACOES]
        if not anteriores:
            return
        anterior = anteriores[-1]
        assert COMPENSACOES.index(anterior) < COMPENSACOES.index(tipo)
        resposta = self.tipo_do_aplicado.get(causa) if causa else None
        respostas = RESPOSTA[anterior]
        assert resposta in (respostas if isinstance(respostas, tuple) else (respostas,))


def _motivo_do_desfecho(tipo: str) -> dict[str, str]:
    return {} if tipo == "PagamentoConfirmado" else {"motivo": "codigo do provedor"}


def _feito(cenario: CenarioDaSaga) -> tuple[tuple[str, ...], str | None]:
    """O que a saga ja fez: passos concluidos e o comando em voo."""
    em_voo = cenario.saga.comando_em_voo
    return cenario.saga.passos_concluidos, em_voo["tipo"] if em_voo else None


def _id_do_comando(cenario: CenarioDaSaga, indice: int) -> UUID:
    return UUID(cenario.publicador.envelopes[indice]["id"])


def _foto(cenario: CenarioDaSaga) -> tuple[object, ...]:
    saga, ordem = cenario.saga, cenario.ordem
    return (
        saga.etapa,
        saga.etapa_desde,
        saga.passos,
        saga.passos_concluidos,
        saga.comando_em_voo,
        saga.prazo_resposta_em,
        saga.plano_compensacao,
        saga.itens,
        ordem.status,
        ordem.historico,
        ordem.resumo_orcamento,
        ordem.resumo_pagamento,
        ordem.motivo_cancelamento,
        tuple(cenario.publicador.comandos),
    )


def _conferir_estado(cenario: CenarioDaSaga) -> None:
    saga, ordem = cenario.saga, cenario.ordem
    assert ordem.status in PARES[saga.etapa], (saga.etapa, ordem.status)
    em_voo = saga.comando_em_voo
    # Parada na falha, o comando em voo fica, sem prazo (RFC-004 secao 4.7).
    if saga.etapa is E.FALHA_NA_COMPENSACAO:
        assert em_voo is not None
        assert saga.prazo_resposta_em is None
    else:
        assert (saga.prazo_resposta_em is None) is (em_voo is None)
    assert em_voo is None or em_voo["tipo"] in COMANDOS_COM_PRAZO
    # Sem comando em voo, nada a reenviar (e o contador do proximo comeca do zero).
    assert em_voo is not None or saga.reenvios == 0
    # OS cancelada se e so se saga compensada, e OS encerrada so com a saga
    # encerrada.
    assert (ordem.status is S.CANCELADA) is (saga.etapa is E.COMPENSADA)
    assert ordem.status not in ESTADOS_TERMINAIS or saga.encerrada


@cache
def _simular(semente: int) -> Simulacao:
    """Uma simulacao por semente, a mesma para a propriedade e para a guarda."""
    rnd = random.Random(semente)  # noqa: S311  # roteiro de teste, nao segredo
    roteiro = Roteiro(
        falha=rnd.choice(FALHAS),
        estorno_recusado=rnd.random() < CHANCE_DE_ESTORNO_RECUSADO,
        aprovacao_tardia=rnd.random() < CHANCE_DE_APROVACAO_TARDIA,
        inverte_o_estorno=rnd.random() < CHANCE_DE_INVERTER_O_ESTORNO,
    )
    simulacao = Simulacao(CenarioDaSaga(), rnd, roteiro)
    simulacao.rodar()
    return simulacao


def _compensacoes(simulacao: Simulacao) -> list[str]:
    comandos = simulacao.cenario.publicador.comandos
    return [c[0] for c in comandos if c[0] in COMPENSACOES]


@pytest.mark.parametrize("semente", SEMENTES)
def test_fora_de_ordem_e_compensacao_nao_quebram_a_saga(semente: int) -> None:
    simulacao = _simular(semente)
    saga, ordem = simulacao.cenario.saga, simulacao.cenario.ordem
    enviadas = _compensacoes(simulacao)

    assert simulacao.dlq == [], simulacao.entregas
    if simulacao.compensacao is None:
        assert saga.etapa is E.CONCLUIDA
        assert ordem.status in {S.FINALIZADA, S.ENTREGUE}
        assert [p["gatilho"] for p in saga.passos] == ["abertura", *FLUXO_FELIZ]
        assert enviadas == []
        return
    _, motivo, plano = simulacao.compensacao
    match saga.etapa:
        case E.COMPENSADA:
            assert (saga.motivo, ordem.status) == (motivo, S.CANCELADA)
            assert enviadas == list(plano)
        case E.FALHA_NA_COMPENSACAO:
            # O provedor recusou o estorno: a sequencia parou nele.
            assert simulacao.roteiro.estorno_recusado
            assert saga.falha == "estorno_recusado"
            assert enviadas == list(plano[: plano.index("EstornarPagamento") + 1])
            # A pendente segue no plano e em voo para a retomada.
            assert saga.plano_compensacao[0] == "EstornarPagamento"
            assert saga.comando_em_voo is not None
            assert saga.comando_em_voo["tipo"] == "EstornarPagamento"
            # A OS mantem o status de antes da compensacao.
            assert ordem.status in ANTES_DO_PIVOT
        case E.CONCLUIDA:
            # O inicio da execucao venceu o cancelamento (corrida do pivot).
            assert (motivo, enviadas) == ("cancelamento", ["CancelarExecucao"])
            assert "cancelamento_recusado" in [p["gatilho"] for p in saga.passos]
            assert ordem.status in {S.FINALIZADA, S.ENTREGUE}
        case _:
            pytest.fail(f"saga parada em {saga.etapa}")


def test_gerador_produz_cada_situacao_da_classificacao_e_cada_roteiro() -> None:
    # Guarda do gerador: nas mil sementes, as entregas caem em cada situacao da
    # RFC-004 secoes 4.4 e 4.5 que a propriedade promete exercitar, e cada
    # falha de negocio e o caminho feliz aparecem. Medido com este gerador
    # (entregas, sementes): obsoleto (217, 155), repetido (38, 38), fora do
    # fluxo (1.649, 796), fora da compensacao (566, 454), estorno automatico
    # (261, 196) e adiantado (1.171, 513); cada roteiro em 128 a 159 sementes; o
    # cancelamento casado que chega com o resumo ja estornado, em 22 sementes.
    # Os minimos ficam perto da metade; o teto do adiantado impede que ele
    # domine. Mudou o gerador (inclusive um sorteio a mais), meca de novo.
    simulacoes = [_simular(s) for s in SEMENTES]
    total: Counter[str] = sum((s.classificacoes for s in simulacoes), Counter())
    sementes = Counter(c for s in simulacoes for c in s.classificacoes)
    roteiros = Counter(s.roteiro.falha for s in simulacoes)
    assert total["obsoleto"] > 100
    assert sementes["obsoleto"] > 75
    assert total["repetido"] > 15
    assert sementes["repetido"] > 15
    assert total["fora_do_fluxo"] > 800
    assert sementes["fora_do_fluxo"] > 400
    assert total["fora_da_compensacao"] > 280
    assert sementes["fora_da_compensacao"] > 225
    assert total["estorno_automatico"] > 130
    assert sementes["estorno_automatico"] > 98
    assert 600 < total["adiantado"] < 2400
    assert sementes["adiantado"] > 250
    # A inversao entre as duas replicas: o estorno automatico antes da resposta
    # casada, que a saga aplica sem prender a compensacao.
    assert sum(s.cancelados_apos_o_estorno for s in simulacoes) >= 10
    assert all(roteiros[f] > 100 for f in FALHAS)
    assert sum(1 for s in simulacoes if s.dlq) == 0
