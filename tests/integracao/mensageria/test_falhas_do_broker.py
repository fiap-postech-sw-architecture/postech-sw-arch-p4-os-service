"""Falhas do broker que o RabbitMQ de teste nao produz sob demanda.

Nack, queda no meio de um lote, canal fechado pelo broker, conexao perdida no
heartbeat: o relay e o consumidor falam com um canal AMQP falso (programado
falha a falha), e o banco continua o Postgres real.
"""

from __future__ import annotations

import json
import math
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pika
import pytest
from pika.exceptions import ChannelClosedByBroker, NackError, StreamLostError
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.compartilhado.aplicacao.mensageria import Desfecho, FalhaTransitoriaError
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria import relay as modulo_relay
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
)
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from tests.integracao.broker import (
    EmSegundoPlano,
    EsperasRegistradas,
    envelope_de_evento,
    esperar_ate,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

    from tests.rastreamento import Rastreador

_URL = "amqp://os:senha-de-teste@broker.invalido:5672/%2F"  # gitleaks:allow


class CanalFalso:
    """Canal AMQP programavel: cada publicacao sai ou falha na ordem pedida."""

    def __init__(
        self,
        *falhas: BaseException | None,
        na_declaracao: BaseException | None = None,
        entregas: list[tuple[Any, Any, bytes]] | None = None,
    ) -> None:
        self.falhas = list(falhas)
        self.na_declaracao = na_declaracao
        self.entregas = list(entregas or [])
        self.publicadas: list[tuple[str, str, Any]] = []
        self.confirmadas: list[int] = []
        self.rejeitadas: list[int] = []
        self.is_open = True

    def confirm_delivery(self) -> None:
        pass

    def exchange_declare(self, nome: str, *, passive: bool) -> None:
        if self.na_declaracao is not None:
            raise self.na_declaracao

    def queue_declare(self, nome: str, *, passive: bool) -> None:
        self.exchange_declare(nome, passive=passive)

    def basic_qos(self, *, prefetch_count: int) -> None:
        pass

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: Any = None,
        mandatory: bool = False,
    ) -> None:
        falha = self.falhas.pop(0) if self.falhas else None
        if isinstance(falha, ChannelClosedByBroker):
            self.is_open = False
        if falha is not None:
            raise falha
        self.publicadas.append((exchange, routing_key, properties))

    def consume(self, fila: str, inactivity_timeout: float) -> Any:
        while True:
            if self.entregas:
                yield self.entregas.pop(0)
            else:
                time.sleep(inactivity_timeout)
                yield None, None, None

    def basic_ack(self, delivery_tag: int) -> None:
        self.confirmadas.append(delivery_tag)

    def basic_reject(self, delivery_tag: int, *, requeue: bool) -> None:
        assert requeue is False
        self.rejeitadas.append(delivery_tag)


class ConexaoFalsa:
    def __init__(
        self, *canais_novos: Any, no_heartbeat: BaseException | None = None
    ) -> None:
        self.canais_novos = list(canais_novos)
        self.no_heartbeat = no_heartbeat
        self.is_open = True

    def channel(self) -> Any:
        canal = self.canais_novos.pop(0)
        if isinstance(canal, BaseException):
            raise canal
        return canal

    def process_data_events(self, time_limit: float) -> None:
        if self.no_heartbeat is not None:
            falha, self.no_heartbeat = self.no_heartbeat, None
            raise falha

    def close(self) -> None:
        self.is_open = False


@pytest.fixture
def conexoes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Resultados de ``amqp.conectar`` na ordem: excecao ou (conexao, canal)."""
    fila: list[Any] = []

    def conectar(_params: Any) -> tuple[Any, Any]:
        resultado = fila.pop(0) if fila else StreamLostError("sem broker")
        if isinstance(resultado, BaseException):
            raise resultado
        return resultado

    monkeypatch.setattr(amqp, "conectar", conectar)
    monkeypatch.setattr(amqp, "RECONEXAO_BASE_S", 0.05)
    return fila


def _gravar(
    session_factory: sessionmaker[Session], ordem_id: UUID | None = None
) -> UUID:
    exemplo = json.loads((CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text())
    ordem_id = ordem_id or uuid4()
    with SQLAlchemyUnitOfWork(session_factory) as uow:
        mensagem_id = uow.publicar_comando(
            "SolicitarDiagnostico",
            {**exemplo["dados"], "ordem_id": ordem_id},
            correlation_id=ordem_id,
        )
        uow.commit()
    return mensagem_id


def _linha(engine: Engine, mensagem_id: UUID) -> Any:
    with engine.connect() as conexao:
        return conexao.execute(
            text(
                "SELECT status, tentativas, ultimo_erro FROM outbox "
                "WHERE mensagem_id = :id"
            ),
            {"id": mensagem_id},
        ).one()


def _relay(
    engine: Engine,
    rastreador: Rastreador,
    tmp_path: Path,
    **config: Any,
) -> Relay:
    return Relay(
        engine=engine,
        parametros=amqp.parametros(_URL, "teste"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.05, diretorio_de_saude=tmp_path, **config),
    )


def test_nack_do_broker_conta_tentativa(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    canal = CanalFalso(NackError([]))
    conexoes.append((ConexaoFalsa(), canal))
    mensagem_id = _gravar(session_factory)

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path)):
        linha = esperar_ate(
            lambda: (atual := _linha(engine, mensagem_id)).tentativas == 1 and atual
        )

    assert linha.status == "pendente"
    assert linha.ultimo_erro == "recusada pelo broker (nack)"
    assert canal.publicadas == []


def test_queda_no_meio_do_lote_devolve_as_linhas_sem_gastar_tentativa(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    primeiro_canal = CanalFalso(None, StreamLostError("broker reiniciou"))
    segundo_canal = CanalFalso()
    conexoes.extend([(ConexaoFalsa(), primeiro_canal), (ConexaoFalsa(), segundo_canal)])
    mensagens = [_gravar(session_factory) for _ in range(3)]

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path)):
        esperar_ate(
            lambda: all(_linha(engine, m).status == "entregue" for m in mensagens)
        )

    assert [_linha(engine, m).tentativas for m in mensagens] == [0, 0, 0]
    assert len(primeiro_canal.publicadas) == 1
    # O lease das que voltaram foi liberado: nao esperaram 60 s.
    assert len(segundo_canal.publicadas) == 2


def test_canal_fechado_pelo_broker_conta_tentativa_e_o_relay_abre_outro(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    novo_canal = CanalFalso()
    canal = CanalFalso(ChannelClosedByBroker(403, "ACCESS_REFUSED"))
    conexoes.append((ConexaoFalsa(novo_canal), canal))
    mensagem_id = _gravar(session_factory)

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path, atrasos_s=(0.1,) * 5)):
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue")

    linha = _linha(engine, mensagem_id)
    assert linha.tentativas == 1
    assert len(novo_canal.publicadas) == 1


def test_canal_que_nao_reabre_e_queda_do_broker(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    canal = CanalFalso(ChannelClosedByBroker(406, "PRECONDITION_FAILED"))
    reconectado = CanalFalso()
    conexoes.extend(
        [
            (ConexaoFalsa(StreamLostError("caiu ao reabrir")), canal),
            (ConexaoFalsa(), reconectado),
        ]
    )
    primeira = _gravar(session_factory)
    segunda = _gravar(session_factory)

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path, atrasos_s=(0.1,) * 5)):
        esperar_ate(
            lambda: (
                {_linha(engine, m).status for m in (primeira, segunda)} == {"entregue"}
            )
        )

    assert _linha(engine, primeira).tentativas == 1
    assert _linha(engine, segunda).tentativas == 0


def test_exchange_ausente_na_declaracao_passiva_espera_e_tenta_de_novo(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    sem_topologia = CanalFalso(na_declaracao=ChannelClosedByBroker(404, "NOT_FOUND"))
    pronto = CanalFalso()
    conexoes.extend([(ConexaoFalsa(), sem_topologia), (ConexaoFalsa(), pronto)])
    mensagem_id = _gravar(session_factory)

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path)):
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue")

    assert sem_topologia.publicadas == []
    assert _linha(engine, mensagem_id).tentativas == 0


def test_conexao_perdida_no_heartbeat_ocioso_reconecta(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    ociosa = ConexaoFalsa(no_heartbeat=StreamLostError("heartbeat vencido"))
    nova = CanalFalso()
    conexoes.extend([(ociosa, CanalFalso()), (ConexaoFalsa(), nova)])

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path)):
        esperar_ate(lambda: not ociosa.is_open)
        mensagem_id = _gravar(session_factory)
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue")

    assert len(nova.publicadas) == 1


def test_banco_fora_no_ciclo_nao_derruba_o_relay(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexoes.append((ConexaoFalsa(), CanalFalso()))
    reivindicar = Relay._reivindicar
    falhas = [OperationalError("SELECT", {}, Exception("banco fora"))]

    def reivindicar_com_falha(self: Relay, conexao: Any) -> Any:
        if falhas:
            raise falhas.pop()
        return reivindicar(self, conexao)

    monkeypatch.setattr(Relay, "_reivindicar", reivindicar_com_falha)
    mensagem_id = _gravar(session_factory)

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path)):
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue")

    assert falhas == []


def test_linha_que_outra_replica_ja_finalizou_e_pulada(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canal = CanalFalso()
    conexoes.append((ConexaoFalsa(), canal))
    entregar = Relay._entregar

    def outra_replica_entrega_antes(self: Relay, linha: Any) -> None:
        # Entre o claim (ja comitado) e a entrega desta replica.
        with engine.begin() as outra:
            outra.execute(text("UPDATE outbox SET status = 'entregue'"))
        entregar(self, linha)

    monkeypatch.setattr(Relay, "_entregar", outra_replica_entrega_antes)
    mensagem_id = _gravar(session_factory)
    relay = _relay(engine, rastreador, tmp_path)

    with EmSegundoPlano(relay):
        esperar_ate(lambda: (tmp_path / "relay-pronto").exists())
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue")
        time.sleep(0.2)

    assert canal.publicadas == []


def test_sem_listen_nem_select_o_relay_segue_pelo_poll(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexoes.append((ConexaoFalsa(), CanalFalso()))
    argumentos = engine.dialect.create_connect_args
    falhas_de_listen = [OSError("listen recusado")]

    def connect_args(url: Any) -> Any:
        if falhas_de_listen:
            falhas_de_listen.pop()
            return [], {"host": "127.0.0.1", "port": 1, "connect_timeout": 1}
        return argumentos(url)

    falhas_de_select = [OSError("socket morto")]
    select_original = modulo_relay.select.select

    def select_com_falha(*args: Any) -> Any:
        if falhas_de_select:
            raise falhas_de_select.pop()
        return select_original(*args)

    monkeypatch.setattr(engine.dialect, "create_connect_args", connect_args)
    monkeypatch.setattr(
        modulo_relay, "select", SimpleNamespace(select=select_com_falha)
    )

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path)):
        esperar_ate(lambda: not falhas_de_listen and not falhas_de_select)
        mensagem_id = _gravar(session_factory)
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue")


def test_metricas_da_outbox_com_o_banco_fora_viram_nan(
    rastreador: Rastreador, tmp_path: Path
) -> None:
    fora = create_engine(
        "postgresql://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    _relay(fora, rastreador, tmp_path)

    valor = REGISTRY.get_sample_value("outbox_pendentes")

    assert valor is not None
    assert math.isnan(valor)


def test_config_do_relay_e_do_consumidor_vem_do_ambiente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTBOX_POLL_SEGUNDOS", "2.5")
    monkeypatch.setenv("OUTBOX_LOTE", "20")
    monkeypatch.setenv("OUTBOX_LEASE_SEGUNDOS", "90")
    monkeypatch.setenv("CONSUMIDOR_PREFETCH", "3")

    relay = ConfigRelay.do_ambiente()

    assert (relay.poll_s, relay.lote, relay.lease) == (2.5, 20, timedelta(seconds=90))
    assert ConfigConsumidor.do_ambiente().prefetch == 3


def _entrega(envelope: dict[str, Any], tag: int) -> tuple[Any, Any, bytes]:
    propriedades = pika.BasicProperties(
        message_id=envelope["id"],
        correlation_id=envelope["correlation_id"],
        type=envelope["tipo"],
        user_id=catalogo().produtor(envelope["tipo"]),
        content_type="application/json",
        headers={},
    )
    return (
        SimpleNamespace(delivery_tag=tag),
        propriedades,
        json.dumps(envelope).encode(),
    )


def _consumidor(
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    tmp_path: Path,
    despachante: dict[str, Any],
) -> Consumidor:
    return Consumidor(
        session_factory=session_factory,
        parametros=amqp.parametros(_URL, "teste"),
        despachante=despachante,
        tracer=rastreador.tracer,
        config=ConfigConsumidor(inatividade_s=0.05, diretorio_de_saude=tmp_path),
    )


def _falha_transitoria(*_: object) -> Desfecho:
    raise FalhaTransitoriaError("dependencia fora")


@pytest.mark.parametrize(
    "falha", [pika.exceptions.UnroutableError([]), NackError([])], ids=str
)
def test_copia_de_retry_recusada_pelo_broker_vai_para_a_dlq(
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
    falha: BaseException,
) -> None:
    envelope = envelope_de_evento("PecasReservadas")
    canal = CanalFalso(falha, entregas=[_entrega(envelope, 7)])
    conexoes.append((ConexaoFalsa(), canal))
    consumidor = _consumidor(
        session_factory,
        rastreador,
        tmp_path,
        {"PecasReservadas": _falha_transitoria},
    )

    with EmSegundoPlano(consumidor):
        esperar_ate(lambda: canal.rejeitadas)

    assert canal.rejeitadas == [7]
    assert canal.confirmadas == []


def test_evento_sem_handler_vai_para_a_dlq(
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    canal = CanalFalso(entregas=[_entrega(envelope_de_evento("ReservaLiberada"), 3)])
    conexoes.append((ConexaoFalsa(), canal))

    with EmSegundoPlano(_consumidor(session_factory, rastreador, tmp_path, {})):
        esperar_ate(lambda: canal.rejeitadas)

    assert canal.rejeitadas == [3]


def test_banco_fora_na_limpeza_nao_derruba_o_consumidor(
    conexoes: list[Any], rastreador: Rastreador, tmp_path: Path
) -> None:
    conexoes.append((ConexaoFalsa(), CanalFalso()))
    fora = create_engine(
        "postgresql://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    consumidor = _consumidor(sessionmaker(bind=fora), rastreador, tmp_path, {})

    with EmSegundoPlano(consumidor):
        esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
        heartbeat = (tmp_path / "consumidor-heartbeat").stat().st_mtime_ns
        esperar_ate(
            lambda: (tmp_path / "consumidor-heartbeat").stat().st_mtime_ns > heartbeat
        )


class _CanalCancelado(CanalFalso):
    """O broker cancela o consumo: o gerador do pika so termina, sem excecao."""

    def consume(self, fila: str, inactivity_timeout: float) -> Any:
        yield from ()


@pytest.mark.usefixtures("backoff_sem_jitter")
def test_consumo_cancelado_pelo_broker_reconecta_e_segue(
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    envelope = envelope_de_evento("ExecucaoCancelada")
    canal = CanalFalso(entregas=[_entrega(envelope, 9)])
    conexoes.extend([(ConexaoFalsa(), _CanalCancelado()), (ConexaoFalsa(), canal)])
    recebidas: list[Any] = []

    def registrar(mensagem: Any, _sessao: Any) -> Desfecho:
        recebidas.append(mensagem.id)
        return Desfecho.PROCESSADA

    consumidor = _consumidor(
        session_factory, rastreador, tmp_path, {"ExecucaoCancelada": registrar}
    )
    parar = EsperasRegistradas()
    with EmSegundoPlano(consumidor, parar):
        esperar_ate(lambda: canal.confirmadas)

    assert recebidas == [UUID(envelope["id"])]
    assert canal.confirmadas == [9]
    # O cancelamento reconecta com backoff, sem laco quente.
    assert parar.esperas[:1] == [0.05]


@pytest.mark.parametrize(
    "falha",
    [
        OperationalError("SELECT", {}, Exception("banco fora")),
        ConflitoDeConcorrenciaException(),
        TimeoutError("dependencia lenta"),
        ConnectionError("dependencia fora"),
    ],
    ids=lambda falha: type(falha).__name__,
)
def test_falha_transitoria_do_handler_vai_para_a_primeira_fila_de_retry(
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
    falha: Exception,
) -> None:
    envelope = envelope_de_evento("PagamentoExpirado")
    canal = CanalFalso(entregas=[_entrega(envelope, 4)])
    conexoes.append((ConexaoFalsa(), canal))

    def falhar(*_: object) -> Desfecho:
        raise falha

    despachante = {"PagamentoExpirado": falhar}
    with EmSegundoPlano(
        _consumidor(session_factory, rastreador, tmp_path, despachante)
    ):
        esperar_ate(lambda: canal.confirmadas)

    ((exchange, routing_key, copia),) = canal.publicadas
    assert (exchange, routing_key) == ("pytstop.retry", "os.eventos.retry.1s")
    assert copia.headers["x-tentativa"] == 1
    assert canal.confirmadas == [4]
    assert canal.rejeitadas == []


def test_mensagem_ignorada_recebe_ack_e_fica_registrada(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    envelope = envelope_de_evento("OrcamentoExpirado")
    canal = CanalFalso(entregas=[_entrega(envelope, 2)])
    conexoes.append((ConexaoFalsa(), canal))
    antes = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total",
        {"tipo": "OrcamentoExpirado", "resultado": "ignorada"},
    )

    def ignorar(*_: object) -> Desfecho:
        return Desfecho.IGNORADA

    despachante = {"OrcamentoExpirado": ignorar}
    with EmSegundoPlano(
        _consumidor(session_factory, rastreador, tmp_path, despachante)
    ):
        esperar_ate(lambda: canal.confirmadas)

    depois = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total",
        {"tipo": "OrcamentoExpirado", "resultado": "ignorada"},
    )
    assert depois == (antes or 0) + 1
    with engine.connect() as conexao:
        registrada = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
            {"id": envelope["id"]},
        ).scalar_one()
    assert registrada == 1


def test_falha_inesperada_vai_para_a_dlq_sem_derrubar_o_consumidor(
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    venenosa = envelope_de_evento("ExecucaoAgendada")
    seguinte = envelope_de_evento("ExecucaoAgendada")
    canal = CanalFalso(entregas=[_entrega(venenosa, 1), _entrega(seguinte, 2)])
    conexoes.append((ConexaoFalsa(), canal))
    respostas: list[Any] = [None, Desfecho.PROCESSADA]

    def responder(*_: object) -> Any:
        # Primeiro um handler com bug (devolve None): falha fora do handler.
        return respostas.pop(0)

    with EmSegundoPlano(
        _consumidor(
            session_factory, rastreador, tmp_path, {"ExecucaoAgendada": responder}
        )
    ):
        esperar_ate(lambda: canal.confirmadas)

    assert canal.rejeitadas == [1]
    assert canal.confirmadas == [2]


@pytest.fixture
def backoff_sem_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Espera = teto do sorteio, com teto de 0,4 s: a sequencia fica conferivel."""
    monkeypatch.setattr(amqp, "_sortear", lambda teto: teto)
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.4)


@pytest.mark.usefixtures("backoff_sem_jitter")
def test_canal_fechado_a_cada_mensagem_reconecta_com_backoff_ate_mensagem_tratada(
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    # Sem a permissao de topico da fila de retry, o broker fecha o canal (403) a
    # cada copia: a original fica sem ack (volta para a fila) e o consumidor
    # reconecta esperando cada vez mais, ate o teto. So uma mensagem tratada
    # volta a espera ao minimo.
    recusadas = [envelope_de_evento("ReservaDePecasFalhou") for _ in range(6)]
    tratada = envelope_de_evento("ReservaDePecasFalhou")
    canais = [
        CanalFalso(
            ChannelClosedByBroker(403, "ACCESS_REFUSED"), entregas=[_entrega(e, i)]
        )
        for i, e in enumerate(recusadas)
    ]
    canais.append(
        CanalFalso(
            ChannelClosedByBroker(403, "ACCESS_REFUSED"),
            entregas=[_entrega(tratada, 10), _entrega(recusadas[0], 11)],
        )
    )
    conexoes.extend((ConexaoFalsa(), canal) for canal in canais)

    def tratar(mensagem: Any, _transacao: Any) -> Desfecho:
        if mensagem.id != UUID(tratada["id"]):
            raise FalhaTransitoriaError("dependencia fora")
        return Desfecho.PROCESSADA

    consumidor = _consumidor(
        session_factory, rastreador, tmp_path, {"ReservaDePecasFalhou": tratar}
    )
    parar = EsperasRegistradas()

    with EmSegundoPlano(consumidor, parar):
        esperar_ate(lambda: len(parar.esperas) >= 7)

    assert parar.esperas[:7] == [0.05, 0.1, 0.2, 0.4, 0.4, 0.4, 0.05]
    assert all(canal.rejeitadas == [] for canal in canais)
    assert [canal.confirmadas for canal in canais] == [[]] * 6 + [[10]]


@pytest.mark.usefixtures("backoff_sem_jitter")
def test_relay_reconecta_com_backoff_que_so_zera_com_mensagem_entregue(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    # Tres tentativas de conexao recusadas: 0,05, 0,1 e 0,2 s. Conectado, a
    # primeira linha sai (o broker confirma) e a conexao cai na segunda: a
    # espera volta ao minimo.
    conexoes.extend(
        [
            StreamLostError("recusada"),
            StreamLostError("recusada"),
            StreamLostError("recusada"),
            (ConexaoFalsa(), CanalFalso(None, StreamLostError("caiu"))),
            (ConexaoFalsa(), CanalFalso()),
        ]
    )
    mensagens = [_gravar(session_factory), _gravar(session_factory)]
    parar = EsperasRegistradas()

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path), parar):
        esperar_ate(
            lambda: all(_linha(engine, m).status == "entregue" for m in mensagens)
        )

    assert parar.esperas[:4] == [0.05, 0.1, 0.2, 0.05]
    assert [_linha(engine, m).tentativas for m in mensagens] == [0, 0]


class _LogEspiao:
    """Logger que guarda o span corrente de cada linha."""

    def __init__(self) -> None:
        self.linhas: list[tuple[str, Any]] = []

    def _registrar(self, evento: str, **_campos: object) -> None:
        from opentelemetry import trace

        self.linhas.append((evento, trace.get_current_span().get_span_context()))

    info = warning = error = _registrar

    def exception(self, evento: str, **campos: object) -> None:
        self._registrar(evento, **campos)


def test_logs_da_entrega_saem_dentro_do_span_da_publicacao(
    engine: Engine,
    session_factory: sessionmaker[Session],
    conexoes: list[Any],
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _LogEspiao()
    monkeypatch.setattr(modulo_relay, "_log", log)
    conexoes.append((ConexaoFalsa(), CanalFalso(NackError([]))))
    mensagem_id = _gravar(session_factory)

    with EmSegundoPlano(_relay(engine, rastreador, tmp_path)):
        esperar_ate(lambda: _linha(engine, mensagem_id).tentativas == 1)

    (span,) = rastreador.spans("publish SolicitarDiagnostico")
    contextos = {
        evento: contexto
        for evento, contexto in log.linhas
        if evento == "message publish failed; retry scheduled"
    }
    assert contextos["message publish failed; retry scheduled"] == (
        span.get_span_context()
    )


def test_poll_do_relay_acima_de_15_s_aborta_o_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTBOX_POLL_SEGUNDOS", "16")

    with pytest.raises(RuntimeError, match="OUTBOX_POLL_SEGUNDOS"):
        ConfigRelay.do_ambiente()
