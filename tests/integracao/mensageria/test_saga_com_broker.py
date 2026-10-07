"""Saga de ponta a ponta no OS: relay, consumidor, RabbitMQ e participantes falsos.

Os participantes leem ``billing.comandos`` e ``execucao.comandos`` (como admin) e
respondem como ``billing`` e ``execucao``, com o ``causation_id`` do comando que
responde (RFC-004 secao 5.2) e o contexto de trace dele; os passos humanos
(mecanico, cliente) saem do contexto do comando que os pos em espera (ADR-043).
As filas de retry do broker de teste tem TTL de 100 ms.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
)
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_dos_cabecalhos,
)
from src.consumidor import montar_despachante
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_registrar_entrega,
)
from tests.eventos import envelope_de_evento
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    outbox_recusando_insert,
)
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

_ATENDENTE = "4c2f8f8e-1b8e-4d5a-9a9e-3f1d2c7b6a55"
_DLQ = "os.eventos.dlq"
# Fila de cada comando que o OS publica no caminho feliz.
_FILA = {
    "SolicitarDiagnostico": "execucao.comandos",
    "GerarOrcamento": "billing.comandos",
    "ReservarPecas": "execucao.comandos",
    "SolicitarPagamento": "billing.comandos",
    "AgendarExecucao": "execucao.comandos",
}


def _retries() -> float:
    return sum(
        amostra.value
        for metrica in REGISTRY.collect()
        if metrica.name == "pytstop_mensagens_consumidas"
        for amostra in metrica.samples
        if amostra.name.endswith("_total") and amostra.labels["resultado"] == "retry"
    )


def _amostra(nome: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(nome, labels) or 0.0


class Atendimento:
    """Uma OS aberta pelo caso de uso e os participantes respondendo pelo broker."""

    def __init__(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory
        self._broker = broker
        self._rastreador = rastreador
        self.comandos: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.ordem_id = UUID(int=0)

    def abrir(self) -> UUID:
        """POST de abertura: o span dele e a raiz do trace da saga."""
        with self._session_factory() as sess:
            cliente = criar_cliente_com_veiculo(sess)
            sess.commit()
            cliente_id, veiculo_id = cliente.id, cliente.veiculos[0].id
        with (
            self._rastreador.tracer.start_as_current_span(
                "POST /api/v1/ordens-de-servico"
            ),
            self._session_factory() as sess,
        ):
            self.ordem_id = (
                obter_abrir_ordem(sess)
                .executar(
                    AbrirOrdemDTO(
                        cliente_id=cliente_id,
                        veiculo_id=veiculo_id,
                        descricao_problema="Barulho na suspensao",
                        ator=_ATENDENTE,
                    )
                )
                .id
            )
        return self.ordem_id

    def receber(self, tipo: str) -> dict[str, Any]:
        """O participante tira o comando da fila dele e confere o contrato."""
        props, corpo = esperar_ate(lambda: self._broker.pegar(_FILA[tipo]))
        envelope = json.loads(corpo)
        catalogo().validar(envelope)
        assert (props.type, envelope["tipo"]) == (tipo, tipo)
        assert envelope["correlation_id"] == str(self.ordem_id)
        self.comandos[tipo] = (props, envelope)
        return envelope

    def responder(self, comando: str, tipo: str, **dados: Any) -> dict[str, Any]:
        """Evento em resposta ao (ou aberto pelo) comando, no trace dele."""
        props, envelope_do_comando = self.comandos[comando]
        with self._rastreador.tracer.start_as_current_span(
            f"participante {tipo}", context=contexto_dos_cabecalhos(props.headers)
        ) as span:
            cabecalhos = {"traceparent": traceparent(span)}
        envelope = envelope_de_evento(
            tipo,
            correlation_id=self.ordem_id,
            causation_id=envelope_do_comando["id"],
            **dados,
        )
        catalogo().validar(envelope)
        self._broker.publicar_evento(envelope, cabecalhos=cabecalhos)
        return envelope

    def etapa(self) -> str | None:
        with self._engine.connect() as conexao:
            return conexao.execute(
                text("SELECT etapa FROM sagas WHERE ordem_id = :id"),
                {"id": self.ordem_id},
            ).scalar_one_or_none()

    def gatilhos(self) -> list[str]:
        with self._engine.connect() as conexao:
            passos = conexao.execute(
                text("SELECT passos FROM sagas WHERE ordem_id = :id"),
                {"id": self.ordem_id},
            ).scalar_one()
        return [p["gatilho"] for p in passos]

    def esperar_passo(self, gatilho: str) -> None:
        esperar_ate(lambda: gatilho in self.gatilhos())

    def ate_aguardando_agendamento(self) -> None:
        """Do pedido de diagnostico ao AgendarExecucao, na ordem."""
        self.receber("SolicitarDiagnostico")
        self.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
        self.esperar_passo("DiagnosticoIniciado")
        self.responder("SolicitarDiagnostico", "DiagnosticoConcluido")
        self.receber("GerarOrcamento")
        self.responder("GerarOrcamento", "OrcamentoGerado")
        self.esperar_passo("OrcamentoGerado")
        self.responder("GerarOrcamento", "OrcamentoAprovado")
        self.receber("ReservarPecas")
        self.responder("ReservarPecas", "PecasReservadas")
        self.receber("SolicitarPagamento")
        self.responder("SolicitarPagamento", "PagamentoSolicitado")
        self.esperar_passo("PagamentoSolicitado")
        self.responder("SolicitarPagamento", "PagamentoConfirmado")
        self.receber("AgendarExecucao")


@pytest.fixture
def atendimento(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> Iterator[Atendimento]:
    """Relay e consumidor reais em threads, com o despachante do processo."""
    relay = Relay(
        engine=engine,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.1, diretorio_de_saude=tmp_path),
    )
    consumidor = Consumidor(
        session_factory=session_factory,
        parametros=broker.parametros("os"),
        despachante=montar_despachante(timedelta(seconds=120)),
        tracer=rastreador.tracer,
        config=ConfigConsumidor(inatividade_s=0.1, diretorio_de_saude=tmp_path),
    )
    with EmSegundoPlano(relay), EmSegundoPlano(consumidor):
        esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
        yield Atendimento(engine, session_factory, broker, rastreador)


def test_caminho_feliz_ate_a_entrega_num_trace_so(
    atendimento: Atendimento,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
) -> None:
    iniciadas = _amostra("pytstop_saga_iniciadas_total")
    concluidas = _amostra("pytstop_saga_finalizadas_total", resultado="concluida")
    retries = _retries()

    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")
    atendimento.esperar_passo("ExecucaoAgendada")
    atendimento.responder("AgendarExecucao", "ExecucaoIniciada")
    atendimento.esperar_passo("ExecucaoIniciada")
    atendimento.responder("AgendarExecucao", "ExecucaoFinalizada")
    esperar_ate(lambda: atendimento.etapa() == "concluida")
    with session_factory() as sess:
        entregue = obter_registrar_entrega(sess).executar(ordem_id, ator=_ATENDENTE)

    assert (entregue.status, entregue.etapa) == ("entregue", "concluida")
    assert atendimento.gatilhos() == [
        "abertura",
        "DiagnosticoIniciado",
        "DiagnosticoConcluido",
        "OrcamentoGerado",
        "OrcamentoAprovado",
        "PecasReservadas",
        "PagamentoSolicitado",
        "PagamentoConfirmado",
        "ExecucaoAgendada",
        "ExecucaoIniciada",
        "ExecucaoFinalizada",
    ]
    assert [m.para for m in entregue.historico] == [
        "recebida",
        "em_diagnostico",
        "aguardando_aprovacao",
        "aguardando_pagamento",
        "aguardando_execucao",
        "em_execucao",
        "finalizada",
        "entregue",
    ]
    # Cada comando seguinte leva o id do evento que o causou; o primeiro, nulo.
    causas = {t: e["causation_id"] for t, (_, e) in atendimento.comandos.items()}
    passos = {p["comando"]: p["mensagem_id"] for p in entregue.passos if p["comando"]}
    assert causas == {
        "SolicitarDiagnostico": None,
        "GerarOrcamento": passos["GerarOrcamento"],
        "ReservarPecas": passos["ReservarPecas"],
        "SolicitarPagamento": passos["SolicitarPagamento"],
        "AgendarExecucao": passos["AgendarExecucao"],
    }
    assert broker.contar(_DLQ) == 0
    assert _retries() == retries
    assert _amostra("pytstop_saga_iniciadas_total") == iniciadas + 1
    assert (
        _amostra("pytstop_saga_finalizadas_total", resultado="concluida")
        == concluidas + 1
    )

    # Um trace da abertura aos comandos e respostas (RFC-004 secao 9).
    (raiz,) = rastreador.spans("POST /api/v1/ordens-de-servico")
    trace_id = raiz.get_span_context().trace_id
    spans = rastreador.spans()
    assert {s.get_span_context().trace_id for s in spans} == {trace_id}
    nomes = {s.name for s in spans}
    assert {f"publish {c}" for c in _FILA} <= nomes
    assert {f"process {t}" for t in atendimento.gatilhos()[1:]} <= nomes
    (consumo,) = rastreador.spans("process PecasReservadas")
    assert consumo.attributes is not None
    assert {
        k: v for k, v in consumo.attributes.items() if k.startswith("pytstop.saga")
    } == {
        "pytstop.saga.etapa": "aguardando_reserva",
        "pytstop.saga.etapa_nova": "aguardando_pagamento",
        "pytstop.saga.desfecho": "processada",
    }


def test_execucao_iniciada_antes_da_agendada_volta_pela_retry_e_passa(
    atendimento: Atendimento, broker: Broker, rastreador: Rastreador
) -> None:
    atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    retries = _retries()

    # O inicio ultrapassa o agendamento (retry ou consumidores concorrentes).
    atendimento.responder("AgendarExecucao", "ExecucaoIniciada")
    esperar_ate(lambda: _retries() > retries)
    assert atendimento.etapa() == "aguardando_agendamento"
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")

    esperar_ate(lambda: atendimento.etapa() == "em_execucao")
    assert broker.contar(_DLQ) == 0
    assert atendimento.gatilhos()[-2:] == ["ExecucaoAgendada", "ExecucaoIniciada"]
    adiantado = [
        s
        for s in rastreador.spans("process ExecucaoIniciada")
        if s.attributes and s.attributes.get("pytstop.saga.desfecho") == "adiantada"
    ]
    assert adiantado


def test_falha_no_insert_da_outbox_nao_grava_nada_do_evento(
    atendimento: Atendimento, engine: Engine, broker: Broker
) -> None:
    ordem_id = atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
    atendimento.esperar_passo("DiagnosticoIniciado")

    # O GerarOrcamento nao entra na outbox: a transacao da mensagem inteira cai,
    # tentativa apos tentativa, ate a DLQ.
    with outbox_recusando_insert(engine):
        concluido = atendimento.responder(
            "SolicitarDiagnostico", "DiagnosticoConcluido"
        )
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    with engine.connect() as conexao:
        processada = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
            {"id": concluido["id"]},
        ).scalar_one()
        status, itens = conexao.execute(
            text(
                "SELECT o.status, s.itens FROM ordens_de_servico o "
                "JOIN sagas s ON s.ordem_id = o.id WHERE o.id = :id"
            ),
            {"id": ordem_id},
        ).one()
        comandos = conexao.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox ORDER BY id")
        ).scalars()
        assert list(comandos) == ["SolicitarDiagnostico"]
    assert (processada, status, itens) == (0, "em_diagnostico", [])
    assert atendimento.etapa() == "aguardando_diagnostico"
    assert atendimento.gatilhos() == ["abertura", "DiagnosticoIniciado"]
