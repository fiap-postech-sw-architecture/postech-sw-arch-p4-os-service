"""Propriedade: evento fora de ordem nunca leva a estado invalido nem a DLQ.

Participantes simulados (stdlib ``random``, sementes fixas) respondem a cada
comando que o orquestrador real publica, com os eventos daquele passo emitidos
em sequencia e entregues com atraso aleatorio: dentro da janela eles chegam
embaralhados, alguns sao republicados com id novo (o participante respondendo
a um comando repetido) e alguns atrasam bem mais. Um evento so existe depois
do comando que o habilita, como no sistema real. O adiantado volta como a
copia da fila de retry, depois do atraso do nivel (1, 5, 15, 60 e 300 s), e a
falha seguinte a quinta copia seria a DLQ (RFC-004 secao 5.1).

Em pontos aleatorios o atendente tenta cancelar ou entregar a OS: o
cancelamento e sempre recusado (a saga esta em andamento, ou a OS ja passou do
pivot) e a entrega so passa com a OS finalizada.

A cada entrega: so ``FalhaTransitoriaError`` escapa do handler; o par (etapa,
status da OS) esta na tabela da RFC-004 secao 4.1; ha prazo se e so se ha
comando com prazo em voo; o fato ja aplicado nao muda nada; nenhum comando sai
depois do estado final; a OS nunca fica encerrada (cancelada ou entregue) com a
saga viva. No fim: DLQ vazia, saga concluida e cada comando uma vez, na ordem.
"""

from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import pytest

from src.compartilhado.aplicacao.mensageria import Desfecho, FalhaTransitoriaError
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import COMANDOS_COM_PRAZO
from src.ordem_servico.aplicacao.use_cases import CancelarOrdem, RegistrarEntrega
from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS, StatusOrdem
from tests.fabricas import ATOR_ATENDENTE
from tests.unitarios.fakes import FakeUnitOfWork
from tests.unitarios.ordem_servico.cenario_da_saga import FLUXO_FELIZ, CenarioDaSaga

if TYPE_CHECKING:
    from src.compartilhado.aplicacao.mensageria import MensagemRecebida

S = StatusOrdem
# Cerca de mil execucoes, deterministicas.
SEMENTES: Final = range(1000)
ATRASOS_DE_RETRY_S: Final = (1, 5, 15, 60, 300)
# Eventos que cada comando habilita, na ordem em que o participante ou a pessoa
# (mecanico, cliente) os emite (RFC-004 secao 4.8a).
RESPOSTAS: Final = {
    "SolicitarDiagnostico": ("DiagnosticoIniciado", "DiagnosticoConcluido"),
    "GerarOrcamento": ("OrcamentoGerado", "OrcamentoAprovado"),
    "ReservarPecas": ("PecasReservadas",),
    "SolicitarPagamento": ("PagamentoSolicitado", "PagamentoConfirmado"),
    "AgendarExecucao": ("ExecucaoAgendada", "ExecucaoIniciada", "ExecucaoFinalizada"),
}
# Entre duas emissoes do mesmo passo; atraso de entrega (a janela que embaralha);
# chance de republicar com id novo e de atrasar muito uma entrega.
ENTRE_EMISSOES_S: Final = 1.0
JANELA_S: Final = 2.0
CHANCE_DE_REPETIR: Final = 0.3
CHANCE_DE_ATRASAR: Final = 0.1
ATRASO_LONGO_S: Final = 30.0
# Chance de o atendente tentar cancelar ou entregar a OS antes de uma entrega.
CHANCE_DE_ENCERRAR: Final = 0.1
# Status da OS em cada etapa do fluxo normal (tabela da RFC-004 secao 4.1).
PARES: Final = {
    EtapaSaga.AGUARDANDO_DIAGNOSTICO: {S.RECEBIDA, S.EM_DIAGNOSTICO},
    EtapaSaga.AGUARDANDO_ORCAMENTO: {S.EM_DIAGNOSTICO},
    EtapaSaga.AGUARDANDO_DECISAO: {S.AGUARDANDO_APROVACAO},
    EtapaSaga.AGUARDANDO_RESERVA: {S.AGUARDANDO_APROVACAO},
    EtapaSaga.AGUARDANDO_PAGAMENTO: {S.AGUARDANDO_PAGAMENTO},
    EtapaSaga.AGUARDANDO_AGENDAMENTO: {S.AGUARDANDO_EXECUCAO},
    EtapaSaga.AGUARDANDO_INICIO: {S.AGUARDANDO_EXECUCAO},
    EtapaSaga.EM_EXECUCAO: {S.EM_EXECUCAO},
    EtapaSaga.CONCLUIDA: {S.FINALIZADA, S.ENTREGUE},
}

type _Entrega = tuple[float, int, str, "MensagemRecebida | None", int]


@dataclass
class Simulacao:
    """Agenda de entregas ao consumidor, em ordem de tempo."""

    cenario: CenarioDaSaga
    rnd: random.Random
    agenda: list[_Entrega] = field(default_factory=list)
    sequencia: itertools.count[int] = field(default_factory=itertools.count)
    entregas: list[str] = field(default_factory=list)
    adiantados: int = 0
    dlq: list[str] = field(default_factory=list)
    # (acao, etapa da saga na tentativa, se passou)
    encerramentos: list[tuple[str, EtapaSaga, bool]] = field(default_factory=list)

    def agendar(
        self, instante: float, tipo: str, mensagem: MensagemRecebida | None, copias: int
    ) -> None:
        heapq.heappush(
            self.agenda, (instante, next(self.sequencia), tipo, mensagem, copias)
        )

    def responder(self, comando: str, instante: float) -> None:
        """O participante emite os eventos do passo, cada um com seu atraso."""
        for tipo in RESPOSTAS.get(comando, ()):
            instante += self.rnd.uniform(0, ENTRE_EMISSOES_S)
            entrega = instante + self.rnd.uniform(0, JANELA_S)
            if self.rnd.random() < CHANCE_DE_ATRASAR:
                entrega += self.rnd.uniform(0, ATRASO_LONGO_S)
            self.agendar(entrega, tipo, None, 0)
            if self.rnd.random() < CHANCE_DE_REPETIR:
                # Republicado com id novo: chega depois do original.
                repetido = entrega + self.rnd.uniform(0, ATRASO_LONGO_S)
                self.agendar(repetido, tipo, None, 0)

    def tentar_encerrar(self) -> None:
        """O atendente pede o cancelamento ou registra a entrega da OS."""
        cenario = self.cenario
        acao = self.rnd.choice(("cancelar", "entregar"))
        antes, etapa = _foto(cenario), cenario.saga.etapa
        try:
            if acao == "cancelar":
                CancelarOrdem(cenario.ordens, FakeUnitOfWork(), cenario.sagas).executar(
                    cenario.ordem_id, "cliente desistiu", ator=ATOR_ATENDENTE
                )
            else:
                RegistrarEntrega(
                    cenario.ordens, FakeUnitOfWork(), cenario.sagas
                ).executar(cenario.ordem_id, ator=ATOR_ATENDENTE)
        except TransicaoStatusInvalidaException:
            assert _foto(cenario) == antes
            self.encerramentos.append((acao, etapa, False))
        else:
            self.encerramentos.append((acao, etapa, True))
        _conferir_estado(cenario)

    def rodar(self) -> None:
        cenario = self.cenario
        self.responder("SolicitarDiagnostico", 0.0)
        enviados = len(cenario.publicador.comandos)
        aplicados: set[str] = set()
        comandos_ao_concluir: int | None = None
        while self.agenda:
            if self.rnd.random() < CHANCE_DE_ENCERRAR:
                self.tentar_encerrar()
            instante, _, tipo, mensagem, copias = heapq.heappop(self.agenda)
            mensagem = mensagem or cenario.evento(tipo)
            self.entregas.append(tipo)
            antes = _foto(cenario)
            try:
                tratamento = cenario.orquestrador.tratar(mensagem)
            except FalhaTransitoriaError:
                assert _foto(cenario) == antes
                self.adiantados += 1
                if copias == len(ATRASOS_DE_RETRY_S):
                    self.dlq.append(tipo)
                else:
                    volta = instante + ATRASOS_DE_RETRY_S[copias]
                    self.agendar(volta, tipo, mensagem, copias + 1)
                continue
            _conferir_estado(cenario)
            if tipo in aplicados:
                # O mesmo fato de novo leva ao mesmo estado que uma vez so.
                assert tratamento.desfecho is Desfecho.IGNORADA
                assert _foto(cenario) == antes
            if tratamento.desfecho is Desfecho.PROCESSADA:
                aplicados.add(tipo)
            if comandos_ao_concluir is not None:
                # Nenhum comando depois do estado final.
                assert len(cenario.publicador.comandos) == comandos_ao_concluir
            elif cenario.saga.etapa is EtapaSaga.CONCLUIDA:
                comandos_ao_concluir = len(cenario.publicador.comandos)
            for comando, *_ in cenario.publicador.comandos[enviados:]:
                self.responder(comando, instante)
            enviados = len(cenario.publicador.comandos)


def _foto(cenario: CenarioDaSaga) -> tuple[object, ...]:
    saga, ordem = cenario.saga, cenario.ordem
    return (
        saga.etapa,
        saga.etapa_desde,
        saga.passos,
        saga.passos_concluidos,
        saga.comando_em_voo,
        saga.prazo_resposta_em,
        saga.itens,
        ordem.status,
        ordem.historico,
        ordem.resumo_orcamento,
        ordem.resumo_pagamento,
        tuple(cenario.publicador.comandos),
    )


def _conferir_estado(cenario: CenarioDaSaga) -> None:
    saga, ordem = cenario.saga, cenario.ordem
    assert ordem.status in PARES[saga.etapa], (saga.etapa, ordem.status)
    em_voo = saga.comando_em_voo
    assert (saga.prazo_resposta_em is None) is (em_voo is None)
    assert em_voo is None or em_voo["tipo"] in COMANDOS_COM_PRAZO
    # Sem comando em voo, nada a reenviar (e o contador do proximo comeca do zero).
    assert em_voo is not None or saga.reenvios == 0
    # OS cancelada se e so se saga compensada (nenhuma das duas no caminho feliz)
    # e OS encerrada so com a saga encerrada.
    assert (ordem.status is S.CANCELADA) is (saga.etapa is EtapaSaga.COMPENSADA)
    assert ordem.status not in ESTADOS_TERMINAIS or saga.encerrada


def _simular(semente: int) -> Simulacao:
    rnd = random.Random(semente)  # noqa: S311  # roteiro de teste, nao segredo
    simulacao = Simulacao(CenarioDaSaga(), rnd)
    simulacao.rodar()
    return simulacao


@pytest.mark.parametrize("semente", SEMENTES)
def test_fora_de_ordem_nao_quebra_a_saga_nem_vai_para_a_dlq(semente: int) -> None:
    simulacao = _simular(semente)
    cenario = simulacao.cenario

    assert simulacao.dlq == [], simulacao.entregas
    assert cenario.saga.etapa is EtapaSaga.CONCLUIDA
    assert cenario.ordem.status in {S.FINALIZADA, S.ENTREGUE}
    # Nenhum cancelamento passa, e a entrega so passa com a saga concluida.
    passaram = [(acao, etapa) for acao, etapa, ok in simulacao.encerramentos if ok]
    assert passaram in ([], [("entregar", EtapaSaga.CONCLUIDA)])
    assert [p["gatilho"] for p in cenario.saga.passos] == ["abertura", *FLUXO_FELIZ]
    assert [c[0] for c in cenario.publicador.comandos] == [
        "SolicitarDiagnostico",
        "GerarOrcamento",
        "ReservarPecas",
        "SolicitarPagamento",
        "AgendarExecucao",
    ]


def test_simulacao_produz_fora_de_ordem_repetidos_e_retry() -> None:
    # Guarda do gerador: as sementes produzem de fato o que a propriedade promete.
    simulacoes = [_simular(s) for s in range(200)]
    fora_de_ordem = sum(1 for s in simulacoes if s.entregas[:10] != list(FLUXO_FELIZ))
    repetidos = sum(1 for s in simulacoes if len(set(s.entregas)) < len(s.entregas))
    com_retry = sum(1 for s in simulacoes if s.adiantados)
    # Tentativas de encerrar a OS com a saga em andamento, todas recusadas.
    cancelamentos_com_saga_viva = sum(
        1
        for s in simulacoes
        for acao, etapa, _ in s.encerramentos
        if acao == "cancelar" and etapa is not EtapaSaga.CONCLUIDA
    )
    assert fora_de_ordem > 150
    assert repetidos > 150
    assert com_retry > 100
    assert cancelamentos_com_saga_viva > 100
