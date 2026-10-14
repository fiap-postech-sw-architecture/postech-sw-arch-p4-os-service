"""Propriedade: participante mudo, lento ou perdido nunca leva a estado invalido.

Billing e Execucao simulados (stdlib ``random``, sementes fixas) respondem aos
comandos que o orquestrador e o ``prazos`` reais publicam, com tempo virtual:
o prazo de resposta e de 120 s e o ciclo do ``prazos`` roda a cada 30 s. O
roteiro de cada semente sorteia o maximo de reenvios (0, 1, 2 ou 5), a chance
de um envio se perder, os tipos de comando a que o participante nunca responde,
se o provedor recusa o estorno ate o operador resolver a causa, se o
consumidor do OS sai do ar por uma janela (o ciclo nao reenvia nem esgota), se
o atendente cancela e se o operador retoma a compensacao parada. As respostas
chegam com atraso, duplicadas e as vezes depois de o prazo vencer; o adiantado
volta pela fila de retry.

A cada passo: o par (etapa, status da OS) esta na tabela da RFC-004 secao 4.1;
ha prazo se e so se ha comando com prazo em voo (parada, o comando fica, sem
prazo); os reenvios nunca passam do maximo; o ciclo pausado nao muda nada; as
compensacoes saem na ordem do plano; nenhum comando sai depois do estado final.
No fim, pelos registros da saga: cada reenvio e do mesmo comando em voo, numera
de 1 ao maximo e so sai depois do prazo; o prazo esgotado so vem com todos os
reenvios gastos; a retomada reenvia a pendente e zera o contador. A saga termina
concluida, compensada ou parada em ``falha_na_compensacao``, e com participante
mudo nunca conclui.
"""

from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import pytest

from src.compartilhado.aplicacao.mensageria import FalhaTransitoriaError
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    COMANDOS_COM_PRAZO,
    GATILHOS_VALIDOS,
)
from src.ordem_servico.aplicacao.saga.vencer_prazos import ResultadoDoCiclo
from src.ordem_servico.aplicacao.use_cases import RetomarCompensacao
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE
from tests.unitarios.ordem_servico.cenario_da_saga import COMPENSACOES, CenarioDaSaga
from tests.unitarios.ordem_servico.ciclo_dos_prazos import PRAZO, Prazos
from tests.unitarios.ordem_servico.test_propriedades_da_saga import (
    ATRASOS_DE_RETRY_S,
    _conferir_estado,
)

if TYPE_CHECKING:
    from src.compartilhado.aplicacao.mensageria import MensagemRecebida

E = EtapaSaga
S = StatusOrdem
SEMENTES: Final = range(1000)
INTERVALO_S: Final = 30.0
HORIZONTE_S: Final = 6 * 3600.0
# Maximo de reenvios sorteado, chance de perder um envio, de duplicar e de atrasar
# uma resposta para depois do prazo.
MAXIMOS: Final = (0, 1, 2, 5)
PERDAS: Final = (0.0, 0.3, 0.6)
CHANCE_DE_DUPLICAR: Final = 0.25
CHANCE_DE_ATRASAR: Final = 0.15
# Uma resposta atrasada chega de uma vez com os eventos humanos do mesmo comando,
# entre o prazo e 240 s depois dele: a ordem entre eles se mantem, e o adiantado
# alcanca o evento que falta antes de a fila de retry se esgotar (381 s).
ATRASO_LONGO_S: Final = 240.0
# O operador retoma a mesma saga no maximo tres vezes.
MAXIMO_DE_RETOMADAS: Final = 3
# Os comandos com prazo do fluxo normal; os outros cinco sao compensacoes.
NO_FLUXO: Final = frozenset(
    {"GerarOrcamento", "ReservarPecas", "SolicitarPagamento", "AgendarExecucao"}
)
# O que cada comando responde: (evento, humano). O primeiro e o desfecho do
# comando, que o participante republica a cada reenvio; os humanos so saem na
# primeira vez (o mecanico, o cliente).
RESPOSTAS: Final[dict[str, tuple[tuple[str, bool], ...]]] = {
    "SolicitarDiagnostico": (
        ("DiagnosticoIniciado", True),
        ("DiagnosticoConcluido", True),
    ),
    "GerarOrcamento": (("OrcamentoGerado", False), ("OrcamentoAprovado", True)),
    "ReservarPecas": (("PecasReservadas", False),),
    "SolicitarPagamento": (
        ("PagamentoSolicitado", False),
        ("PagamentoConfirmado", True),
    ),
    "AgendarExecucao": (
        ("ExecucaoAgendada", False),
        ("ExecucaoIniciada", True),
        ("ExecucaoFinalizada", True),
    ),
    "CancelarExecucao": (("ExecucaoCancelada", False),),
    "EstornarPagamento": (("PagamentoEstornado", False),),
    "LiberarReserva": (("ReservaLiberada", False),),
    "CancelarOrcamento": (("OrcamentoCancelado", False),),
    "DescartarDiagnostico": (("DiagnosticoDescartado", False),),
}


@dataclass(frozen=True)
class Roteiro:
    maximo: int
    perda: float
    mudos: frozenset[str]
    estorno_recusado: bool
    pausa: tuple[float, float] | None
    cancela: bool
    operador_retoma: bool
    mudo_some_na_retomada: bool


@dataclass(frozen=True)
class Entrega:
    mensagem: MensagemRecebida
    copias: int = 0


@dataclass(frozen=True)
class Ciclo:
    pass


@dataclass(frozen=True)
class Retomada:
    pass


@dataclass(frozen=True)
class Cancelamento:
    pass


@dataclass
class Simulacao:
    roteiro: Roteiro
    rnd: random.Random
    cenario: CenarioDaSaga
    prazos: Prazos = field(init=False)
    retomar: RetomarCompensacao = field(init=False)
    agenda: list[tuple[float, int, Any]] = field(default_factory=list)
    sequencia: itertools.count[int] = field(default_factory=itertools.count)
    agora: float = 0.0
    enviados: int = 0
    tratados: set[str] = field(default_factory=set)
    mudos: set[str] = field(default_factory=set)
    causa_resolvida: bool = False
    retomada_agendada: bool = False
    comandos_no_fim: int | None = None
    ciclos_pausados: int = 0
    retomadas: int = 0
    dlq: list[str] = field(default_factory=list)
    tipos_enviados: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.prazos = Prazos(
            self.cenario, maximo=self.roteiro.maximo, na_outbox_do_cenario=True
        )
        self.retomar = RetomarCompensacao(
            self.prazos.transacao,
            self.cenario.sagas,
            ContextoDeEnvio(PRAZO, self.prazos.relogio),
        )
        self.mudos = set(self.roteiro.mudos)
        self.inicio = self.cenario.saga.atualizada_em

    # ----- tempo e agenda

    def agendar(self, depois_de: float, acao: Any) -> None:
        heapq.heappush(
            self.agenda, (self.agora + depois_de, next(self.sequencia), acao)
        )

    def sincronizar(self) -> None:
        """O mesmo instante para o consumidor, o ``prazos`` e a API."""
        instante = self.inicio + timedelta(seconds=self.agora)
        self.cenario.relogio.agora = max(self.cenario.relogio.agora, instante)
        self.prazos.relogio.agora = max(self.prazos.relogio.agora, instante)

    # ----- participantes

    def despachar(self) -> None:
        """Os comandos novos da outbox chegam aos participantes, na ordem."""
        comandos = self.cenario.publicador.comandos
        for indice in range(self.enviados, len(comandos)):
            tipo = comandos[indice][0]
            self.conferir_compensacao(indice)
            self.tipos_enviados.add(tipo)
            self.receber(tipo, UUID(self.cenario.publicador.envelopes[indice]["id"]))
        self.enviados = len(comandos)

    def receber(self, tipo: str, comando_id: UUID) -> None:
        # O SolicitarDiagnostico nao tem prazo: perdido, a saga espera o mecanico
        # ate o redrive ou o cancelamento (RFC-004 secao 4.3), e fica de fora.
        if tipo in COMANDOS_COM_PRAZO and (
            tipo in self.mudos or self.rnd.random() < self.roteiro.perda
        ):
            return
        primeira = tipo not in self.tratados
        self.tratados.add(tipo)
        # O participante lento: o desfecho e os fatos humanos saem depois do prazo.
        lento = self.rnd.uniform(PRAZO.total_seconds(), ATRASO_LONGO_S)
        extra = lento if self.rnd.random() < CHANCE_DE_ATRASAR else 0.0
        for posicao, (resposta, humano) in enumerate(RESPOSTAS[tipo]):
            if posicao and not primeira:
                break
            self.responder(
                tipo, resposta, comando_id, humano=humano, posicao=posicao, extra=extra
            )

    def responder(
        self,
        comando: str,
        resposta: str,
        causa: UUID,
        *,
        humano: bool,
        posicao: int,
        extra: float,
    ) -> None:
        dados: dict[str, Any] = {}
        if comando == "EstornarPagamento":
            if self.roteiro.estorno_recusado and not self.causa_resolvida:
                resposta = "EstornoDePagamentoFalhou"
            else:
                dados["motivo"] = "compensacao"
        atraso = self.rnd.uniform(0, 3) + posicao * self.rnd.uniform(1, 30) + extra
        atraso += self.rnd.uniform(5, 60) if humano else 0
        mensagem = evento(resposta, self.cenario.ordem_id, causation_id=causa, **dados)
        self.agendar(atraso, Entrega(mensagem))
        if self.rnd.random() < CHANCE_DE_DUPLICAR:
            repetida = evento(
                resposta, self.cenario.ordem_id, causation_id=causa, **dados
            )
            self.agendar(atraso + self.rnd.uniform(0, 120), Entrega(repetida))

    # ----- o que acontece

    def entregar(self, entrega: Entrega) -> None:
        antes = _foto(self.cenario)
        try:
            self.cenario.orquestrador.tratar(entrega.mensagem)
        except FalhaTransitoriaError:
            assert _foto(self.cenario) == antes
            if entrega.copias == len(ATRASOS_DE_RETRY_S):
                self.dlq.append(entrega.mensagem.tipo)
            else:
                self.agendar(
                    ATRASOS_DE_RETRY_S[entrega.copias],
                    Entrega(entrega.mensagem, entrega.copias + 1),
                )

    def ciclo(self) -> None:
        pausa = self.roteiro.pausa
        fora = pausa is not None and pausa[0] <= self.agora < pausa[1]
        self.prazos.presenca.ha = not fora
        antes = _foto(self.cenario)
        resultado = self.prazos.caso_de_uso.executar()
        if fora:
            # Sem consumidor, o ciclo nao reenvia nem esgota: nada muda. Pausa
            # quando ha saga vencida; o ciclo ocioso nem consulta a fila.
            assert resultado in (ResultadoDoCiclo(pausado=True), ResultadoDoCiclo())
            assert _foto(self.cenario) == antes
            self.ciclos_pausados += resultado.pausado
        else:
            assert not resultado.pausado
            assert resultado.recusadas == 0

    def manter_os_ciclos(self) -> None:
        """O proximo ciclo existe enquanto algo ainda pode vencer ou chegar."""
        if any(isinstance(a, Ciclo) for _, _, a in self.agenda):
            return
        pendente = any(not isinstance(a, Ciclo) for _, _, a in self.agenda)
        com_prazo = self.cenario.saga.prazo_resposta_em is not None
        if (pendente or com_prazo) and self.agora < HORIZONTE_S:
            self.agendar(INTERVALO_S, Ciclo())

    def cancelar(self) -> None:
        try:
            self.cenario.cancelar()
        except TransicaoStatusInvalidaException:
            pass

    def retomar_a_compensacao(self) -> None:
        self.retomada_agendada = False
        if self.cenario.saga.etapa is not E.FALHA_NA_COMPENSACAO:
            return
        self.causa_resolvida = True
        if self.roteiro.mudo_some_na_retomada:
            self.mudos.clear()
        self.retomadas += 1
        self.retomar.executar(self.cenario.ordem_id, ator=ATOR_ATENDENTE)
        assert self.cenario.saga.etapa is E.COMPENSANDO
        assert self.cenario.saga.reenvios == 0
        assert self.cenario.saga.falha is None

    def conferir(self) -> None:
        saga = self.cenario.saga
        _conferir_estado(self.cenario)
        assert saga.reenvios <= self.roteiro.maximo
        em_voo = saga.comando_em_voo
        if em_voo is not None:
            ids = em_voo["mensagem_ids"]
            assert len(set(ids)) == len(ids)
        parada = saga.etapa is E.FALHA_NA_COMPENSACAO
        pode_retomar = self.roteiro.operador_retoma and (
            self.retomadas < MAXIMO_DE_RETOMADAS
        )
        if parada and not self.retomada_agendada and pode_retomar:
            self.retomada_agendada = True
            self.agendar(self.rnd.uniform(10, 600), Retomada())
        if self.comandos_no_fim is not None:
            assert len(self.cenario.publicador.comandos) == self.comandos_no_fim
        elif saga.encerrada:
            self.comandos_no_fim = len(self.cenario.publicador.comandos)

    def conferir_compensacao(self, indice: int) -> None:
        """A compensacao sai na ordem do plano (o estorno nunca antes da execucao)."""
        comandos = self.cenario.publicador.comandos
        tipo = comandos[indice][0]
        if tipo not in COMPENSACOES:
            return
        anteriores = [c[0] for c in comandos[:indice] if c[0] in COMPENSACOES]
        if anteriores and anteriores[-1] != tipo:
            assert COMPENSACOES.index(anteriores[-1]) < COMPENSACOES.index(tipo)

    def rodar(self) -> None:
        self.despachar()
        self.agendar(INTERVALO_S, Ciclo())
        if self.roteiro.cancela:
            self.agendar(self.rnd.uniform(0, 300), Cancelamento())
        while self.agenda:
            self.agora, _, acao = heapq.heappop(self.agenda)
            self.sincronizar()
            match acao:
                case Entrega():
                    self.entregar(acao)
                case Ciclo():
                    self.ciclo()
                case Retomada():
                    self.retomar_a_compensacao()
                case Cancelamento():
                    self.cancelar()
            self.conferir()
            self.despachar()
            self.manter_os_ciclos()


def _foto(cenario: CenarioDaSaga) -> tuple[object, ...]:
    saga, ordem = cenario.saga, cenario.ordem
    return (
        saga.etapa,
        saga.etapa_desde,
        saga.atualizada_em,
        saga.passos,
        saga.comando_em_voo,
        saga.prazo_resposta_em,
        saga.reenvios,
        saga.plano_compensacao,
        ordem.status,
        ordem.historico,
        tuple(cenario.publicador.comandos),
    )


@cache
def _simular(semente: int) -> Simulacao:
    rnd = random.Random(semente)  # noqa: S311  # roteiro de teste, nao segredo
    pausa = None
    if rnd.random() < 0.4:
        inicio = rnd.uniform(0, 600)
        pausa = (inicio, inicio + rnd.uniform(30, 900))
    mudos = (
        frozenset({rnd.choice(sorted(COMANDOS_COM_PRAZO))})
        if rnd.random() < 0.4
        else frozenset()
    )
    roteiro = Roteiro(
        maximo=rnd.choice(MAXIMOS),
        perda=rnd.choice(PERDAS),
        mudos=mudos,
        estorno_recusado=rnd.random() < 0.3,
        pausa=pausa,
        cancela=rnd.random() < 0.3,
        operador_retoma=rnd.random() < 0.8,
        mudo_some_na_retomada=rnd.random() < 0.7,
    )
    simulacao = Simulacao(roteiro, rnd, CenarioDaSaga())
    simulacao.rodar()
    return simulacao


def _conferir_registros(simulacao: Simulacao) -> None:
    """Os reenvios, o esgotamento e a retomada, pelos registros da saga."""
    passos = simulacao.cenario.saga.passos
    atual: str | None = None
    enviado_em = None
    reenvios = 0
    ids: set[str] = set()
    for passo in passos:
        assert passo["gatilho"] in GATILHOS_VALIDOS
        if passo["comando_id"] is not None:
            assert passo["comando_id"] not in ids
            ids.add(passo["comando_id"])
        gatilho = passo["gatilho"]
        if gatilho == "reenvio":
            assert passo["comando"] == atual
            assert passo["ator"] == "prazos"
            reenvios += 1
            assert passo["reenvio"] == reenvios <= simulacao.roteiro.maximo
        elif gatilho == "prazo_esgotado":
            # Todos os reenvios gastos antes de desistir do passo.
            assert reenvios == simulacao.roteiro.maximo
            assert passo["ator"] == "prazos"
        elif gatilho == "retomada":
            assert passo["comando"] == atual
            reenvios = 0
        if gatilho in {"reenvio", "prazo_esgotado"}:
            # So depois do prazo do envio anterior.
            assert enviado_em is not None
            assert datetime.fromisoformat(passo["em"]) - enviado_em >= PRAZO
        if passo["comando"] is not None:
            enviado_em = datetime.fromisoformat(passo["em"])
            if gatilho not in {"reenvio", "retomada"}:
                atual, reenvios = passo["comando"], 0


@pytest.mark.parametrize("semente", SEMENTES)
def test_participante_mudo_ou_perdido_nao_quebra_a_saga(semente: int) -> None:
    simulacao = _simular(semente)
    saga = simulacao.cenario.saga
    roteiro = simulacao.roteiro

    assert simulacao.dlq == []
    _conferir_registros(simulacao)
    assert saga.etapa in {E.CONCLUIDA, E.COMPENSADA, E.FALHA_NA_COMPENSACAO}
    # Sem resposta a um comando do fluxo, a saga nao conclui. A compensacao muda
    # fica de fora: o inicio da execucao pode vencer o cancelamento (corrida do
    # pivot), e a execucao ja nao e cancelada.
    if saga.etapa is E.CONCLUIDA:
        assert not simulacao.tipos_enviados & roteiro.mudos & NO_FLUXO
    if saga.etapa is E.FALHA_NA_COMPENSACAO:
        assert saga.falha in {"reenvios_esgotados", "estorno_recusado"}
        assert saga.plano_compensacao[0] == saga.comando_em_voo["tipo"]
        assert saga.prazo_resposta_em is None
        # A retomada que o operador faria ja foi feita se a causa estava ao alcance.
        assert not simulacao.retomada_agendada


def test_gerador_exercita_o_reenvio_o_esgotamento_a_pausa_e_a_retomada() -> None:
    # Guarda do gerador: nas mil sementes, cada situacao que a propriedade
    # promete aparece em muitas. Medido com este gerador (sementes): reenvio
    # (538), reenvio 5 (68), prazo esgotado (434), retomada (239), ciclo pausado
    # (313); termina concluida em 412, compensada em 470 e parada em
    # `falha_na_compensacao` em 118. Os minimos ficam perto da metade. Mudou o
    # gerador (inclusive um sorteio a mais), meca de novo.
    simulacoes = [_simular(s) for s in SEMENTES]
    gatilhos = [{p["gatilho"] for p in s.cenario.saga.passos} for s in simulacoes]
    finais = [s.cenario.saga.etapa for s in simulacoes]
    quinto = sum(
        any(p.get("reenvio") == MAXIMOS[-1] for p in s.cenario.saga.passos)
        for s in simulacoes
    )

    assert sum("reenvio" in g for g in gatilhos) > 270
    assert quinto > 34
    assert sum("prazo_esgotado" in g for g in gatilhos) > 215
    assert sum("retomada" in g for g in gatilhos) > 120
    assert sum(s.ciclos_pausados > 0 for s in simulacoes) > 150
    assert sum(f is E.CONCLUIDA for f in finais) > 200
    assert sum(f is E.COMPENSADA for f in finais) > 235
    assert sum(f is E.FALHA_NA_COMPENSACAO for f in finais) > 59
