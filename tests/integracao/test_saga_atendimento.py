"""Passos do BDD de componente da saga (``tests/bdd/saga_atendimento.feature``).

O OS roda inteiro sobre o PostgreSQL de teste; o RabbitMQ da lugar a um
barramento em memoria (ADR-041): os comandos saem da outbox e vao aos
participantes falsos, e os eventos que eles publicam passam pelo mesmo caminho
do consumidor, com o despachante do processo.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy import select, update

from src.compartilhado.aplicacao.mensageria import (
    Desfecho,
    FalhaTransitoriaError,
    MensagemRecebida,
)
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.outbox_mapping import (
    outbox_table,
    registrar_processada,
)
from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem
from src.consumidor import montar_despachante
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_obter_ordem,
    obter_registrar_entrega,
)
from tests.eventos import envelope_de_evento
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

scenarios("saga_atendimento.feature")

_ATENDENTE: Final = "0f8e2d7c-6b5a-4c3d-9e1f-a2b3c4d5e6f7"
# Fila de comandos e origem dos eventos de cada participante.
_PARTICIPANTE: Final = {"o Billing": "billing", "a Execução": "execucao"}
_ORIGEM: Final = {"o Billing": "billing-service", "a Execução": "execution-service"}
# A copia de retry volta cinco vezes; a falha seguinte iria para a DLQ.
_ENTREGAS_ATE_A_DLQ: Final = 6
# Evento espontaneo leva o id do comando que abriu o fluxo (RFC-004 secao 5.2).
_COMANDO_QUE_ABRIU: Final = {
    "DiagnosticoIniciado": "SolicitarDiagnostico",
    "DiagnosticoConcluido": "SolicitarDiagnostico",
    "OrcamentoGerado": "GerarOrcamento",
    "OrcamentoAprovado": "GerarOrcamento",
    "PecasReservadas": "ReservarPecas",
    "PagamentoSolicitado": "SolicitarPagamento",
    "PagamentoConfirmado": "SolicitarPagamento",
    "ExecucaoAgendada": "AgendarExecucao",
    "ExecucaoIniciada": "AgendarExecucao",
    "ExecucaoFinalizada": "AgendarExecucao",
}


class Barramento:
    """Outbox -> participantes e participantes -> consumidor do OS, em memoria."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._despachante = montar_despachante(timedelta(seconds=120))
        self.fila_do_os: deque[tuple[dict[str, Any], int]] = deque()
        self.comandos: dict[str, dict[str, Any]] = {}
        self.caixas: dict[str, list[str]] = {"billing": [], "execucao": []}
        self.desfechos: dict[str, str] = {}
        self.dlq: list[str] = []

    def levar_a_outbox(self) -> None:
        """O relay: comandos pendentes vao, validados, a fila do participante."""
        with self._session_factory() as sessao:
            linhas = sessao.execute(
                select(outbox_table.c.id, outbox_table.c.envelope)
                .where(outbox_table.c.status == "pendente")
                .order_by(outbox_table.c.id)
            ).all()
            for linha in linhas:
                envelope = linha.envelope
                catalogo().validar(envelope)
                destino = catalogo().destino(envelope["tipo"]).routing_key
                self.caixas[destino.split(".")[1]].append(envelope["tipo"])
                self.comandos[envelope["tipo"]] = envelope
            sessao.execute(
                update(outbox_table)
                .where(outbox_table.c.id.in_([linha.id for linha in linhas]))
                .values(status="entregue")
            )
            sessao.commit()

    def publicar(self, envelope: dict[str, Any]) -> None:
        catalogo().validar(envelope)
        self.fila_do_os.append((envelope, 1))
        self.entregar()

    def entregar(self) -> None:
        """O consumidor: passa pela fila ate nenhuma mensagem andar.

        Cada mensagem grava ``mensagens_processadas``, roda o handler e comita
        numa transacao; a transitoria volta para o fim da fila, como a copia
        de retry.
        """
        andou = True
        while andou and self.fila_do_os:
            andou = False
            for _ in range(len(self.fila_do_os)):
                envelope, entrega = self.fila_do_os.popleft()
                if self._consumir(envelope):
                    andou = True
                elif entrega == _ENTREGAS_ATE_A_DLQ:
                    self.dlq.append(envelope["tipo"])
                else:
                    self.fila_do_os.append((envelope, entrega + 1))
        self.levar_a_outbox()

    def _consumir(self, envelope: dict[str, Any]) -> bool:
        mensagem = MensagemRecebida.do_envelope(envelope)
        with self._session_factory() as sessao:
            if not registrar_processada(sessao, mensagem.id):
                return True
            try:
                desfecho = self._despachante[mensagem.tipo](
                    mensagem, TransacaoDaMensagem(sessao)
                )
            except (FalhaTransitoriaError, ConflitoDeConcorrenciaException):
                sessao.rollback()
                self.desfechos[mensagem.tipo] = "adiantada"
                return False
            sessao.commit()
        self.desfechos[mensagem.tipo] = desfecho.value
        return True


class Atendimento:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.barramento = Barramento(session_factory)
        self.ordem_id = UUID(int=0)
        self.veiculo: tuple[UUID, UUID] | None = None


@given("um cliente com um veículo cadastrado", target_fixture="atendimento")
def _cliente_com_veiculo(session_factory: sessionmaker[Session]) -> Atendimento:
    atendimento = Atendimento(session_factory)
    with session_factory() as sessao:
        cliente = criar_cliente_com_veiculo(sessao)
        sessao.commit()
        atendimento.veiculo = (cliente.id, cliente.veiculos[0].id)
    return atendimento


@when("o atendente abre a ordem de serviço")
def _abrir(atendimento: Atendimento) -> None:
    assert atendimento.veiculo is not None
    cliente_id, veiculo_id = atendimento.veiculo
    with atendimento.session_factory() as sessao:
        atendimento.ordem_id = (
            obter_abrir_ordem(sessao)
            .executar(
                AbrirOrdemDTO(
                    cliente_id=cliente_id,
                    veiculo_id=veiculo_id,
                    descricao_problema="Barulho na suspensao dianteira",
                    ator=_ATENDENTE,
                )
            )
            .id
        )
    atendimento.barramento.levar_a_outbox()


@when(parsers.parse('{participante} publica "{tipo}"'))
def _publicar(atendimento: Atendimento, participante: str, tipo: str) -> None:
    envelope = envelope_de_evento(
        tipo,
        correlation_id=atendimento.ordem_id,
        causation_id=atendimento.barramento.comandos[_COMANDO_QUE_ABRIU[tipo]]["id"],
    )
    assert envelope["origem"] == _ORIGEM[participante]
    atendimento.barramento.publicar(envelope)


@when("o atendente registra a entrega")
def _entregar(atendimento: Atendimento) -> None:
    with atendimento.session_factory() as sessao:
        obter_registrar_entrega(sessao).executar(atendimento.ordem_id, ator=_ATENDENTE)


@then(parsers.parse('{participante} recebe o comando "{comando}"'))
def _recebe(atendimento: Atendimento, participante: str, comando: str) -> None:
    caixa = atendimento.barramento.caixas[_PARTICIPANTE[participante]]
    assert caixa[-1:] == [comando]
    envelope = atendimento.barramento.comandos[comando]
    assert envelope["correlation_id"] == str(atendimento.ordem_id)


@then(parsers.parse('a saga está na etapa "{etapa}" com a OS "{status}"'))
def _etapa_e_status(atendimento: Atendimento, etapa: str, status: str) -> None:
    with atendimento.session_factory() as sessao:
        ordem = obter_obter_ordem(sessao).executar(atendimento.ordem_id)
    assert (ordem.etapa, ordem.status) == (etapa, status)


@then(parsers.parse('a OS fica "{status}" com a saga "{etapa}"'))
def _final(atendimento: Atendimento, status: str, etapa: str) -> None:
    with atendimento.session_factory() as sessao:
        ordem = obter_obter_ordem(sessao).executar(atendimento.ordem_id)
    assert (ordem.status, ordem.etapa) == (status, etapa)
    assert [p["gatilho"] for p in ordem.passos] == [
        "abertura",
        *_COMANDO_QUE_ABRIU,
    ]


@then(parsers.parse('o evento "{tipo}" volta para a fila'))
def _volta(atendimento: Atendimento, tipo: str) -> None:
    barramento = atendimento.barramento
    assert barramento.desfechos[tipo] == "adiantada"
    assert [e["tipo"] for e, _ in barramento.fila_do_os] == [tipo]


@then(parsers.parse('o evento "{tipo}" é ignorado'))
def _ignorado(atendimento: Atendimento, tipo: str) -> None:
    assert atendimento.barramento.desfechos[tipo] == Desfecho.IGNORADA.value


@then("nenhuma mensagem ficou na fila do OS")
def _fila_vazia(atendimento: Atendimento) -> None:
    assert (list(atendimento.barramento.fila_do_os), atendimento.barramento.dlq) == (
        [],
        [],
    )
