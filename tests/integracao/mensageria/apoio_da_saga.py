"""Apoio dos testes da saga com broker real: o atendimento, o relay e o consumidor.

O ``Atendimento`` abre a OS pelo caso de uso e faz os participantes falsos lerem
as filas ``billing.comandos`` e ``execucao.comandos`` e responderem pelo broker;
``relay_de_teste`` e ``consumidor_de_teste`` montam os processos reais com poll
curto. Os testes da saga, dos prazos e das notificacoes o compartilham.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from prometheus_client import REGISTRY
from sqlalchemy import text

from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
    Handler,
)
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_dos_cabecalhos,
)
from src.consumidor import montar_despachante
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.interfaces.dependencies import obter_abrir_ordem
from tests.eventos import envelope_de_evento
from tests.integracao.broker import esperar_ate
from tests.integracao.seed_helpers import criar_cliente_com_veiculo
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

ATENDENTE = "4c2f8f8e-1b8e-4d5a-9a9e-3f1d2c7b6a55"

# Fila de cada comando que o OS publica no caminho feliz.
FILA_DO_COMANDO = {
    "SolicitarDiagnostico": "execucao.comandos",
    "GerarOrcamento": "billing.comandos",
    "ReservarPecas": "execucao.comandos",
    "SolicitarPagamento": "billing.comandos",
    "AgendarExecucao": "execucao.comandos",
    "CancelarExecucao": "execucao.comandos",
    "EstornarPagamento": "billing.comandos",
    "LiberarReserva": "execucao.comandos",
    "CancelarOrcamento": "billing.comandos",
    "DescartarDiagnostico": "execucao.comandos",
}


def amostra(nome: str, **labels: str) -> float:
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
        self.engine = engine
        self._session_factory = session_factory
        self._broker = broker
        self._rastreador = rastreador
        self.comandos: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.ordem_id = UUID(int=0)

    def abrir(
        self, *, placa: str | None = None, descricao: str = "Barulho na suspensao"
    ) -> UUID:
        """POST de abertura: o span dele e a raiz do trace da saga."""
        with self._session_factory() as sess:
            cliente = criar_cliente_com_veiculo(sess, placa=placa)
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
                        descricao_problema=descricao,
                        ator=ATENDENTE,
                    )
                )
                .id
            )
        return self.ordem_id

    def receber(self, tipo: str) -> dict[str, Any]:
        """O participante tira o comando da fila dele e confere o contrato."""
        props, corpo = esperar_ate(lambda: self._broker.pegar(FILA_DO_COMANDO[tipo]))
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
        with self.engine.connect() as conexao:
            return conexao.execute(
                text("SELECT etapa FROM sagas WHERE ordem_id = :id"),
                {"id": self.ordem_id},
            ).scalar_one_or_none()

    def gatilhos(self) -> list[str]:
        with self.engine.connect() as conexao:
            passos = conexao.execute(
                text("SELECT passos FROM sagas WHERE ordem_id = :id"),
                {"id": self.ordem_id},
            ).scalar_one()
        return [p["gatilho"] for p in passos]

    def esperar_passo(self, gatilho: str) -> None:
        esperar_ate(lambda: gatilho in self.gatilhos())

    def ate_aguardando_pagamento_com_checkout(self) -> None:
        """Do pedido de diagnostico ao PagamentoSolicitado, na ordem."""
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

    def ate_aguardando_agendamento(self) -> None:
        """Do pedido de diagnostico ao AgendarExecucao, na ordem."""
        self.ate_aguardando_pagamento_com_checkout()
        self.responder("SolicitarPagamento", "PagamentoConfirmado")
        self.receber("AgendarExecucao")


def relay_de_teste(
    engine: Engine, broker: Broker, rastreador: Rastreador, pasta: Path
) -> Relay:
    return Relay(
        engine=engine,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.1, diretorio_de_saude=pasta),
    )


def consumidor_de_teste(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    pasta: Path,
    despachante: Mapping[str, Handler] | None = None,
) -> Consumidor:
    return Consumidor(
        session_factory=session_factory,
        parametros=broker.parametros("os"),
        despachante=despachante or montar_despachante(timedelta(seconds=120)),
        tracer=rastreador.tracer,
        config=ConfigConsumidor(inatividade_s=0.1, diretorio_de_saude=pasta),
    )
