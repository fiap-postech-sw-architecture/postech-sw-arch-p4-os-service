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
from uuid import UUID, uuid4

import pytest
import structlog
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import event, text
from structlog.testing import capture_logs

from src.compartilhado.infraestrutura.mensageria import consumidor as modulo_consumidor
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
    obter_obter_saga,
    obter_registrar_entrega,
)
from tests.eventos import envelope_de_evento
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    outbox_recusando_no_commit,
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
    # Um UPDATE da saga por evento: a abertura grava a versao 1 e cada um dos
    # dez eventos a sobe uma vez (o traceparent vai no mesmo UPDATE).
    with atendimento.engine.connect() as conexao:
        versao = conexao.execute(
            text("SELECT versao FROM sagas WHERE ordem_id = :id"), {"id": ordem_id}
        ).scalar_one()
    assert versao == 11
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


def test_placa_e_descricao_so_no_comando_da_execucao(
    atendimento: Atendimento,
    engine: Engine,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Valores sentinela no caminho real (abertura, relay, consumidor,
    # orquestrador, entrega): a placa e o texto livre so podem aparecer no
    # envelope do SolicitarDiagnostico, por contrato (RFC-004 secao 5.3).
    placa, descricao = "QZX7W42", "Sentinela 7f3a do problema relatado"
    ordem_id = atendimento.abrir(placa=placa, descricao=descricao)
    atendimento.ate_aguardando_agendamento()
    for tipo in ("ExecucaoAgendada", "ExecucaoIniciada"):
        atendimento.responder("AgendarExecucao", tipo)
        atendimento.esperar_passo(tipo)
    atendimento.responder("AgendarExecucao", "ExecucaoFinalizada")
    esperar_ate(lambda: atendimento.etapa() == "concluida")
    with session_factory() as sess:
        obter_registrar_entrega(sess).executar(ordem_id, ator=_ATENDENTE)
        saga = obter_obter_saga(sess).executar(ordem_id)
    esperar_ate(lambda: rastreador.spans("process ExecucaoFinalizada"))

    saidas = capfd.readouterr()
    # Os logs do servico sairam (e foram lidos) nesta captura.
    assert "saga transition" in saidas.out + saidas.err
    textos = [saidas.out, saidas.err, generate_latest(REGISTRY).decode(), repr(saga)]
    for span in rastreador.spans():
        textos += [span.name, str(span.attributes), str(span.status.description)]
        textos += [str(evento.attributes) for evento in span.events]
    with engine.connect() as conexao:
        for consulta in (
            "SELECT passos::text, comando_em_voo::text, itens::text FROM sagas",
            "SELECT * FROM mensagens_processadas",
            "SELECT * FROM historico_status_ordem",
            "SELECT ultimo_erro FROM outbox",
        ):
            textos += [str(linha) for linha in conexao.execute(text(consulta))]
        com_placa = conexao.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox WHERE envelope::text LIKE :p"),
            {"p": f"%{placa}%"},
        ).scalars()
        assert list(com_placa) == ["SolicitarDiagnostico"]
    vazamentos = [t for t in textos if placa in t or descricao in t]
    assert vazamentos == []


def test_execucao_iniciada_antes_da_agendada_volta_pela_retry_e_passa(
    atendimento: Atendimento, broker: Broker, rastreador: Rastreador
) -> None:
    atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    retries = _retries()

    # O inicio ultrapassa o agendamento (retry ou consumidores concorrentes):
    # primeiro na fila, ele chega antes ao consumidor (prefetch 1) e e
    # adiantado; publicados em seguida, nao dependem de tempo do teste.
    atendimento.responder("AgendarExecucao", "ExecucaoIniciada")
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")

    esperar_ate(lambda: atendimento.etapa() == "em_execucao")
    assert _retries() > retries
    assert broker.contar(_DLQ) == 0
    assert atendimento.gatilhos()[-2:] == ["ExecucaoAgendada", "ExecucaoIniciada"]
    adiantado = [
        s
        for s in rastreador.spans("process ExecucaoIniciada")
        if s.attributes and s.attributes.get("pytstop.saga.desfecho") == "adiantada"
    ]
    assert adiantado


def test_commit_recusado_depois_das_escritas_desfaz_os_saga_comando_e_registro(
    atendimento: Atendimento, engine: Engine, broker: Broker
) -> None:
    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_pagamento_com_checkout()
    antes = _estado(engine, ordem_id)
    escritas: list[str] = []

    def registrar(_conexao: Any, _cursor: Any, sql: str, *_: Any) -> None:
        escritas.append(sql.split("(")[0].split(" SET ")[0].strip())

    # O PagamentoConfirmado muda a OS (status, resumo, historico), a saga
    # (etapa, passo, comando em voo) e grava o AgendarExecucao; o trigger
    # deferido so falha no COMMIT, depois de tudo isso. Tentativa apos
    # tentativa, a transacao inteira cai, ate a DLQ.
    with outbox_recusando_no_commit(engine):
        event.listen(engine, "before_cursor_execute", registrar)
        try:
            confirmado = atendimento.responder(
                "SolicitarPagamento", "PagamentoConfirmado"
            )
            esperar_ate(lambda: broker.contar(_DLQ) == 1)
        finally:
            event.remove(engine, "before_cursor_execute", registrar)

    # A falha veio depois das escritas pendentes de OS, saga e outbox.
    assert {
        "UPDATE ordens_de_servico",
        "INSERT INTO historico_status_ordem",
        "UPDATE sagas",
        "INSERT INTO outbox",
    } <= set(escritas)
    assert _estado(engine, ordem_id) == antes
    with engine.connect() as conexao:
        registrada = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
            {"id": confirmado["id"]},
        ).scalar_one()
    assert registrada == 0


def _estado(engine: Engine, ordem_id: UUID) -> tuple[object, ...]:
    """OS, historico, saga e comandos da outbox, lidos do banco."""
    with engine.connect() as conexao:
        os_ = conexao.execute(
            text(
                "SELECT status, pagamento_status, versao FROM ordens_de_servico "
                "WHERE id = :id"
            ),
            {"id": ordem_id},
        ).one()
        historico = conexao.execute(
            text("SELECT count(*) FROM historico_status_ordem WHERE ordem_id = :id"),
            {"id": ordem_id},
        ).scalar_one()
        saga = conexao.execute(
            text(
                "SELECT etapa, passos, comando_em_voo, versao FROM sagas "
                "WHERE ordem_id = :id"
            ),
            {"id": ordem_id},
        ).one()
        comandos = conexao.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox ORDER BY id")
        ).scalars()
        return (tuple(os_), historico, tuple(saga), tuple(comandos))


@pytest.mark.parametrize(
    ("caso", "motivo", "etapa"),
    [
        pytest.param("divergente", "ordem_id_divergente", None, id="divergente"),
        pytest.param("sem_saga", "saga_inexistente", None, id="sem-saga"),
        pytest.param(
            "recusa", "sem_tratador_nesta_versao", "aguardando_decisao", id="recusa"
        ),
    ],
)
def test_recusa_vai_para_a_dlq_com_o_motivo_sem_efeito_nem_registro(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
    caso: str,
    motivo: str,
    etapa: str | None,
) -> None:
    atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    if caso == "recusa":
        atendimento.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
        atendimento.esperar_passo("DiagnosticoIniciado")
        atendimento.responder("SolicitarDiagnostico", "DiagnosticoConcluido")
        atendimento.receber("GerarOrcamento")
        atendimento.responder("GerarOrcamento", "OrcamentoGerado")
        atendimento.esperar_passo("OrcamentoGerado")
    gatilhos = atendimento.gatilhos()
    monkeypatch.setattr(modulo_consumidor, "_log", structlog.get_logger())

    with capture_logs() as logs:
        match caso:
            case "divergente":
                recusado = atendimento.responder(
                    "SolicitarDiagnostico", "DiagnosticoIniciado", ordem_id=str(uuid4())
                )
            case "sem_saga":
                recusado = envelope_de_evento("DiagnosticoIniciado")
                broker.publicar_evento(recusado)
            case _:
                recusado = atendimento.responder("GerarOrcamento", "OrcamentoRecusado")
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    # O operador le o motivo no log e no span; o id fica livre para o redrive.
    assert [
        (log["event"], log.get("motivo")) for log in logs if "dlq" in log["event"]
    ] == [("message rejected to dlq", motivo)]
    with engine.connect() as conexao:
        registrada = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
            {"id": recusado["id"]},
        ).scalar_one()
    assert registrada == 0
    assert atendimento.gatilhos() == gatilhos
    (consumo,) = rastreador.spans(f"process {recusado['tipo']}")
    assert consumo.status.description == motivo
    assert consumo.attributes is not None
    saga = {k: v for k, v in consumo.attributes.items() if k.startswith("pytstop.saga")}
    esperado = {"pytstop.saga.desfecho": "recusada"}
    if etapa is not None:
        esperado["pytstop.saga.etapa"] = etapa
    assert saga == esperado
