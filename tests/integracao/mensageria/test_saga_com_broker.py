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

from src.compartilhado.aplicacao.mensageria import Desfecho, FalhaPermanenteError
from src.compartilhado.infraestrutura import observability as observabilidade
from src.compartilhado.infraestrutura.mensageria import consumidor as modulo_consumidor
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
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_cancelar_ordem,
    obter_obter_ordem,
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
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from opentelemetry.sdk.trace import ReadableSpan
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador, Saidas

_ATENDENTE = "4c2f8f8e-1b8e-4d5a-9a9e-3f1d2c7b6a55"
_DLQ = "os.eventos.dlq"
# Fila de cada comando que o OS publica no caminho feliz.
_FILA = {
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
# As cinco compensacoes, na ordem da RFC-004 secao 4.4, e a resposta de cada uma.
_COMPENSACOES = {
    "CancelarExecucao": "ExecucaoCancelada",
    "EstornarPagamento": "PagamentoEstornado",
    "LiberarReserva": "ReservaLiberada",
    "CancelarOrcamento": "OrcamentoCancelado",
    "DescartarDiagnostico": "DiagnosticoDescartado",
}


# O evento cujo consumo grava cada comando seguinte na outbox.
_CAUSA = {
    "GerarOrcamento": "DiagnosticoConcluido",
    "ReservarPecas": "OrcamentoAprovado",
    "SolicitarPagamento": "PecasReservadas",
    "AgendarExecucao": "PagamentoConfirmado",
}


def _pai(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


def _id(rastreador: Rastreador, nome: str) -> int:
    (span,) = rastreador.spans(nome)
    return span.get_span_context().span_id


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


def _consumidas(tipo: str, resultado: str) -> float:
    return _amostra(
        "pytstop_mensagens_consumidas_total", tipo=tipo, resultado=resultado
    )


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


def _relay(
    engine: Engine, broker: Broker, rastreador: Rastreador, pasta: Path
) -> Relay:
    return Relay(
        engine=engine,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.1, diretorio_de_saude=pasta),
    )


def _consumidor(
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


@pytest.fixture
def atendimento(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> Iterator[Atendimento]:
    """Relay e consumidor reais em threads, com o despachante do processo."""
    relay = _relay(engine, broker, rastreador, tmp_path)
    consumidor = _consumidor(session_factory, broker, rastreador, tmp_path)
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

    # Um trace da abertura aos comandos e respostas (RFC-004 secao 9). O span
    # do ultimo consumo termina depois do ack: espera por ele antes de ler.
    esperar_ate(lambda: rastreador.spans("process ExecucaoFinalizada"))
    (raiz,) = rastreador.spans("POST /api/v1/ordens-de-servico")
    trace_id = raiz.get_span_context().trace_id
    spans = rastreador.spans()
    assert {s.get_span_context().trace_id for s in spans} == {trace_id}
    # E uma arvore: o publish de cada comando e filho de quem o gravou na
    # outbox (a requisicao ou o consumo do evento que o causou), e o consumo
    # de cada evento, do participante que o publicou.
    pais = {s.name: _pai(s) for s in spans}
    assert pais["publish SolicitarDiagnostico"] == raiz.get_span_context().span_id
    for comando, causa in _CAUSA.items():
        assert pais[f"publish {comando}"] == _id(rastreador, f"process {causa}")
    for tipo in atendimento.gatilhos()[1:]:
        assert pais[f"process {tipo}"] == _id(rastreador, f"participante {tipo}")
    (consumo,) = rastreador.spans("process PecasReservadas")
    assert consumo.attributes is not None
    assert {
        k: v for k, v in consumo.attributes.items() if k.startswith("pytstop.saga")
    } == {
        "pytstop.saga.etapa": "aguardando_reserva",
        "pytstop.saga.etapa_nova": "aguardando_pagamento",
        "pytstop.saga.desfecho": "processada",
    }


def test_mesmo_evento_entregue_duas_vezes_gera_um_comando_so(
    atendimento: Atendimento, engine: Engine, broker: Broker
) -> None:
    # RN-028: a reentrega do mesmo id (o broker devolve a mensagem sem ack, o
    # participante republica o mesmo envelope) nao repete o efeito.
    atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
    atendimento.esperar_passo("DiagnosticoIniciado")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoConcluido")
    atendimento.receber("GerarOrcamento")
    atendimento.responder("GerarOrcamento", "OrcamentoGerado")
    atendimento.esperar_passo("OrcamentoGerado")
    atendimento.responder("GerarOrcamento", "OrcamentoAprovado")
    atendimento.receber("ReservarPecas")
    duplicadas = _consumidas("PecasReservadas", "duplicada")

    reservadas = atendimento.responder("ReservarPecas", "PecasReservadas")
    broker.publicar_evento(reservadas)
    esperar_ate(lambda: _consumidas("PecasReservadas", "duplicada") > duplicadas)

    with engine.connect() as conexao:
        pedidos = conexao.execute(
            text(
                "SELECT count(*) FROM outbox "
                "WHERE envelope ->> 'tipo' = 'SolicitarPagamento'"
            )
        ).scalar_one()
    assert pedidos == 1
    assert atendimento.gatilhos().count("PecasReservadas") == 1
    assert broker.contar(_DLQ) == 0


def test_placa_e_descricao_so_no_comando_da_execucao(
    saidas: Saidas,
    atendimento: Atendimento,
    engine: Engine,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
) -> None:
    # Valores sentinela no caminho real (abertura, relay, consumidor,
    # orquestrador, entrega): a placa e o texto livre so podem aparecer no
    # envelope do SolicitarDiagnostico, por contrato (RFC-004 secao 5.3). Os
    # logs vem da fixture `saidas`, que nao depende do stream em que o logging
    # foi configurado por outro teste.
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

    # Os logs do caminho sairam (e foram capturados) neste teste.
    eventos = {evento["event"] for evento in saidas.eventos}
    assert {"saga started", "saga transition"} <= eventos
    textos = [saidas.texto(rastreador), generate_latest(REGISTRY).decode(), repr(saga)]
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
            "encerrada", "ordem_encerrada", "aguardando_diagnostico", id="encerrada"
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
    ordem_id = atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    if caso == "encerrada":
        # OS encerrada com a saga viva, forcada por fora: o cancelamento passa
        # pela saga e nunca produz esse estado, e nenhum evento o toca.
        with engine.begin() as conexao:
            conexao.execute(
                text(
                    "UPDATE ordens_de_servico SET status = 'cancelada' WHERE id = :id"
                ),
                {"id": ordem_id},
            )
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
                recusado = atendimento.responder(
                    "SolicitarDiagnostico", "DiagnosticoIniciado"
                )
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


def _compensacoes_na_outbox(engine: Engine) -> list[str]:
    with engine.connect() as conexao:
        tipos = conexao.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox ORDER BY id")
        ).scalars()
        return [tipo for tipo in tipos if tipo in _COMPENSACOES]


def _cancelar(
    atendimento: Atendimento,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
    motivo: str = "Cliente vendeu o carro",
) -> None:
    """O atendente pede o cancelamento, no trace de uma requisicao propria."""
    monkeypatch.setattr(observabilidade, "_tracer_da_api", rastreador.tracer)
    with (
        rastreador.tracer.start_as_current_span("POST cancelamento"),
        session_factory() as sess,
    ):
        cancelada = obter_cancelar_ordem(sess).executar(
            atendimento.ordem_id, motivo, ator=_ATENDENTE
        )
    assert cancelada.etapa == "compensando"


def test_cancelamento_depois_do_pagamento_compensa_uma_por_vez_na_ordem_da_rfc(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compensacoes = _amostra("pytstop_saga_compensacoes_total", motivo="cancelamento")
    compensadas = _amostra("pytstop_saga_finalizadas_total", resultado="compensada")
    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")
    atendimento.esperar_passo("ExecucaoAgendada")

    _cancelar(atendimento, session_factory, rastreador, monkeypatch)

    respostas: list[dict[str, Any]] = []
    for indice, (comando, resposta) in enumerate(_COMPENSACOES.items(), start=1):
        envelope = atendimento.receber(comando)
        # Uma por vez: a seguinte so sai depois da resposta desta.
        assert _compensacoes_na_outbox(engine) == list(_COMPENSACOES)[:indice]
        assert envelope["dados"]["motivo"] == "cancelamento"
        # A primeira nasce da requisicao (sem causa); as outras, da resposta
        # anterior (RFC-004 secao 5.2).
        causa = respostas[-1]["id"] if respostas else None
        assert envelope["causation_id"] == causa
        respostas.append(atendimento.responder(comando, resposta))
    esperar_ate(lambda: atendimento.etapa() == "compensada")

    with session_factory() as sess:
        ordem = obter_obter_ordem(sess).executar(ordem_id)
    assert (ordem.status, ordem.etapa) == ("cancelada", "compensada")
    ultima = ordem.historico[-1]
    assert (ultima.origem, ultima.motivo) == ("saga", "Cliente vendeu o carro")
    gatilhos = atendimento.gatilhos()
    assert gatilhos[-6:] == ["cancelamento_solicitado", *_COMPENSACOES.values()]
    assert broker.contar(_DLQ) == 0
    assert _amostra(
        "pytstop_saga_compensacoes_total", motivo="cancelamento"
    ) == pytest.approx(compensacoes + 1)
    assert _amostra(
        "pytstop_saga_finalizadas_total", resultado="compensada"
    ) == pytest.approx(compensadas + 1)

    # O trace da saga segue pelo cancelamento: o span da acao e filho do ultimo
    # contexto dela, com link para a requisicao, e o primeiro comando sai dele.
    esperar_ate(lambda: rastreador.spans("process DiagnosticoDescartado"))
    (raiz,) = rastreador.spans("POST /api/v1/ordens-de-servico")
    (requisicao,) = rastreador.spans("POST cancelamento")
    (acao,) = rastreador.spans("saga cancelamento")
    trace_id = raiz.get_span_context().trace_id
    assert acao.context.trace_id == trace_id
    assert _pai(acao) == _id(rastreador, "process ExecucaoAgendada")
    (link,) = acao.links
    assert link.context.span_id == requisicao.get_span_context().span_id
    assert _pai(rastreador.spans("publish CancelarExecucao")[0]) == (
        acao.get_span_context().span_id
    )
    assert {s.get_span_context().trace_id for s in rastreador.spans()} == {
        trace_id,
        requisicao.get_span_context().trace_id,
    }


def test_estorno_recusado_para_a_sequencia_e_guarda_o_comando_em_voo(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")
    atendimento.esperar_passo("ExecucaoAgendada")
    _cancelar(atendimento, session_factory, rastreador, monkeypatch)
    atendimento.receber("CancelarExecucao")
    atendimento.responder("CancelarExecucao", "ExecucaoCancelada")
    estorno = atendimento.receber("EstornarPagamento")

    atendimento.responder("EstornarPagamento", "EstornoDePagamentoFalhou")
    esperar_ate(lambda: atendimento.etapa() == "falha_na_compensacao")

    with session_factory() as sess:
        saga = obter_obter_saga(sess).executar(ordem_id)
        ordem = obter_obter_ordem(sess).executar(ordem_id)
    assert (saga.falha, saga.motivo) == ("estorno_recusado", "cancelamento")
    assert saga.plano_compensacao == tuple(list(_COMPENSACOES)[1:])
    # O comando em voo fica, sem prazo, para a retomada.
    assert saga.comando_em_voo is not None
    assert (saga.comando_em_voo.tipo, saga.prazo_resposta_em) == (
        "EstornarPagamento",
        None,
    )
    with engine.connect() as conexao:
        ids = conexao.execute(
            text(
                "SELECT comando_em_voo -> 'mensagem_ids' FROM sagas WHERE ordem_id = :i"
            ),
            {"i": ordem_id},
        ).scalar_one()
    assert ids == [estorno["id"]]
    # A sequencia parou: nada alem das duas primeiras saiu, e a OS fica como estava.
    assert _compensacoes_na_outbox(engine) == ["CancelarExecucao", "EstornarPagamento"]
    assert ordem.status == "aguardando_execucao"

    # A resposta atrasada ao envio original so atualiza o resumo do pagamento.
    atendimento.responder("EstornarPagamento", "PagamentoEstornado")

    def estornado() -> bool:
        with session_factory() as sess:
            pagamento = obter_obter_ordem(sess).executar(ordem_id).pagamento
        return pagamento is not None and pagamento.status == "estornado"

    esperar_ate(estornado)
    assert atendimento.etapa() == "falha_na_compensacao"
    assert broker.contar(_DLQ) == 0


def test_redrive_da_dlq_trata_o_evento_que_a_versao_anterior_recusou(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    # A versao anterior mandava a falha de negocio e a resposta de compensacao
    # para a DLQ com `sem_tratador_nesta_versao`, sem registrar a mensagem
    # (D98): o redrive devolve a fila inteira e a versao nova as trata.
    def sem_tratador(_mensagem: object, _transacao: object) -> Desfecho:
        raise FalhaPermanenteError("sem_tratador_nesta_versao")

    anterior = {**montar_despachante(timedelta(seconds=120))}
    for tipo in ("PagamentoRecusado", *_COMPENSACOES.values()):
        anterior[tipo] = sem_tratador
    atendimento = Atendimento(engine, session_factory, broker, rastreador)
    pasta_anterior = tmp_path / "anterior"
    pasta_anterior.mkdir()
    with (
        EmSegundoPlano(_relay(engine, broker, rastreador, pasta_anterior)),
        EmSegundoPlano(
            _consumidor(session_factory, broker, rastreador, pasta_anterior, anterior)
        ),
    ):
        esperar_ate(lambda: (pasta_anterior / "consumidor-pronto").exists())
        atendimento.abrir()
        atendimento.ate_aguardando_pagamento_com_checkout()
        recusado = atendimento.responder("SolicitarPagamento", "PagamentoRecusado")
        esperar_ate(lambda: broker.contar(_DLQ) == 1)
        assert atendimento.etapa() == "aguardando_pagamento"

    pasta_nova = tmp_path / "nova"
    pasta_nova.mkdir()
    with (
        EmSegundoPlano(_relay(engine, broker, rastreador, pasta_nova)),
        EmSegundoPlano(_consumidor(session_factory, broker, rastreador, pasta_nova)),
    ):
        esperar_ate(lambda: (pasta_nova / "consumidor-pronto").exists())
        # O mesmo shovel de `make -C platform redrive FILA=os.eventos`.
        broker.rabbitmqctl(
            "set_parameter",
            "shovel",
            "redrive-os.eventos",
            json.dumps(
                {
                    "src-uri": "amqp://",
                    "src-queue": _DLQ,
                    "dest-uri": "amqp://",
                    "dest-queue": "os.eventos",
                    "src-delete-after": "queue-length",
                }
            ),
        )
        esperar_ate(lambda: atendimento.etapa() == "compensando")
        liberacao = atendimento.receber("LiberarReserva")

    # A saga compensa como se o evento tivesse chegado na hora, e a DLQ esvazia.
    assert liberacao["causation_id"] == recusado["id"]
    assert liberacao["dados"]["motivo"] == "pagamento_recusado"
    esperar_ate(lambda: broker.contar(_DLQ) == 0)
    assert atendimento.gatilhos()[-1] == "PagamentoRecusado"
