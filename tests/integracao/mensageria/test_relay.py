"""Relay da outbox contra Postgres e RabbitMQ reais, com a topologia do platform."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy import text

from src.compartilhado.infraestrutura import outbox_mapping
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo
from src.compartilhado.infraestrutura.mensageria.outbox import LinhaDaOutbox, Outbox
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

_FILA = "execucao.comandos"


def _dados(ordem_id: UUID) -> dict[str, Any]:
    exemplo = json.loads((CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text())
    return {**exemplo["dados"], "ordem_id": ordem_id}


def _publicar(
    session_factory: sessionmaker[Session], ordem_id: UUID | None = None
) -> UUID:
    """Grava um SolicitarDiagnostico pela UoW, como o caso de uso da saga fara."""
    ordem_id = ordem_id or uuid4()
    with SQLAlchemyUnitOfWork(session_factory) as uow:
        mensagem_id = uow.publicar_comando(
            "SolicitarDiagnostico", _dados(ordem_id), correlation_id=ordem_id
        )
        uow.commit()
    return mensagem_id


def _linha(engine: Engine, mensagem_id: UUID) -> Any:
    with engine.connect() as conexao:
        return conexao.execute(
            text(
                "SELECT status, tentativas, ultimo_erro, proxima_tentativa_em, "
                "criado_em, entregue_em, envelope FROM outbox WHERE mensagem_id = :id"
            ),
            {"id": mensagem_id},
        ).one()


def _relay(
    engine: Engine,
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    **config: Any,
) -> Relay:
    return Relay(
        engine=engine,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(**{"poll_s": 0.1, **config}, diretorio_de_saude=tmp_path),
    )


def _publicadas(tipo: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_publicadas_total", {"tipo": tipo}
    )
    return valor or 0.0


def test_publica_solicitar_diagnostico_de_ponta_a_ponta(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    # A requisicao HTTP que abre a OS e o span da API; a outbox guarda o contexto
    # dele e o relay publica como filho.
    ordem_id = uuid4()
    with rastreador.tracer.start_as_current_span("POST /api/v1/ordens-de-servico"):
        mensagem_id = _publicar(session_factory, ordem_id)
    antes = _publicadas("SolicitarDiagnostico")
    relay = _relay(engine, broker, rastreador, tmp_path)

    with EmSegundoPlano(relay):
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue")
        assert (tmp_path / "relay-pronto").exists()
        assert (tmp_path / "relay-heartbeat").exists()

    assert not (tmp_path / "relay-pronto").exists()
    entregue = esperar_ate(lambda: broker.pegar(_FILA))
    propriedades, corpo = entregue
    envelope = json.loads(corpo)
    catalogo().validar(envelope)
    assert envelope == _linha(engine, mensagem_id).envelope
    assert propriedades.message_id == str(mensagem_id) == envelope["id"]
    assert propriedades.correlation_id == str(ordem_id)
    assert propriedades.type == "SolicitarDiagnostico"
    assert propriedades.user_id == "os"
    assert propriedades.content_type == "application/json"
    assert propriedades.delivery_mode == 2
    # O header leva o contexto do span PRODUCER, filho do span da API.
    (api,) = rastreador.spans("POST /api/v1/ordens-de-servico")
    (producer,) = rastreador.spans("publish SolicitarDiagnostico")
    assert producer.kind is SpanKind.PRODUCER
    assert producer.parent is not None
    assert producer.parent.span_id == api.get_span_context().span_id
    assert producer.get_span_context().trace_id == api.get_span_context().trace_id
    assert propriedades.headers["traceparent"] == traceparent(producer)
    assert producer.attributes is not None
    assert producer.attributes["correlation_id"] == str(ordem_id)
    assert _publicadas("SolicitarDiagnostico") == antes + 1
    assert broker.contar(_FILA) == 0


def test_mensagem_sem_rota_conta_tentativa_e_nunca_vira_entregue(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    mensagem_id = _publicar(session_factory)
    # Sem o binding, o broker devolve a mensagem (mandatory) em vez de
    # confirma-la e descarta-la.
    with broker.canal() as canal:
        canal.queue_unbind(_FILA, "pytstop.comandos", "comando.execucao.#")
    relay = _relay(engine, broker, rastreador, tmp_path, atrasos_s=(0.5,) * 4)
    try:
        with EmSegundoPlano(relay):
            primeira = esperar_ate(
                lambda: (linha := _linha(engine, mensagem_id)).tentativas >= 1 and linha
            )
            assert primeira.status == "pendente"
            assert primeira.entregue_em is None
            assert "nenhuma fila" in primeira.ultimo_erro
            # A falha veio depois da gravacao: o atraso conta a partir dela.
            assert primeira.proxima_tentativa_em >= primeira.criado_em + timedelta(
                seconds=0.5
            )
            final = esperar_ate(
                lambda: (
                    (linha := _linha(engine, mensagem_id)).status == "dead" and linha
                )
            )
    finally:
        with broker.canal() as canal:
            canal.queue_bind(_FILA, "pytstop.comandos", "comando.execucao.#")

    assert final.tentativas == 5
    assert final.entregue_em is None
    assert REGISTRY.get_sample_value("outbox_dead") == 1
    assert REGISTRY.get_sample_value("outbox_pendentes") == 0
    (falha, *_) = rastreador.spans("publish SolicitarDiagnostico")
    assert falha.status.description is not None
    assert "nenhuma fila" in falha.status.description
    assert broker.contar(_FILA) == 0


def test_linha_gravada_com_o_relogio_do_processo_adiantado_sai_pelo_notify(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O processo que grava (a API) com o relogio uma hora adiantado: a linha
    # leva os tempos do banco, entao o NOTIFY acorda o relay e ela sai na hora,
    # sem esperar o poll de seguranca (10 s aqui).
    monkeypatch.setattr(outbox_mapping, "datetime", _RelogioAdiantado)
    relay = _relay(engine, broker, rastreador, tmp_path, poll_s=10)

    with EmSegundoPlano(relay):
        esperar_ate(lambda: _escutando(engine))
        mensagem_id = _publicar(session_factory)
        esperar_ate(lambda: _linha(engine, mensagem_id).status == "entregue", prazo_s=5)


class _RelogioAdiantado(datetime):
    """``datetime`` do processo uma hora a frente do banco."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        return datetime.now(tz) + timedelta(hours=1)


def _escutando(engine: Engine) -> bool:
    """O relay ja esta no LISTEN (antes disso o NOTIFY se perderia)."""
    with engine.connect() as conexao:
        total: int = conexao.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query = 'LISTEN outbox_novo'"
            )
        ).scalar_one()
    return total > 0


@pytest.mark.usefixtures("session_factory")
def test_atrasos_entre_tentativas_sao_1_4_16_e_64_s_e_a_quinta_falha_e_dead(
    engine: Engine,
) -> None:
    linha_id = _inserir(engine)
    outbox = Outbox(engine)

    for tentativas, atraso in enumerate([1, 4, 16, 64]):
        with engine.begin() as conexao:
            morta = outbox.registrar_falha(conexao, _reler(conexao, linha_id), "x")
            folga = conexao.execute(
                text(
                    "SELECT extract(epoch FROM proxima_tentativa_em - "
                    "clock_timestamp()) FROM outbox WHERE id = :id"
                ),
                {"id": linha_id},
            ).scalar_one()
        assert not morta, tentativas
        assert atraso - 1 < folga <= atraso, (tentativas, folga)
    with engine.begin() as conexao:
        assert outbox.registrar_falha(conexao, _reler(conexao, linha_id), "x")
    assert _status(engine, linha_id) == "dead"


def _reler(conexao: Any, linha_id: int) -> LinhaDaOutbox:
    row = conexao.execute(
        text(
            "SELECT id, mensagem_id, correlation_id, exchange, routing_key, envelope, "
            "traceparent, tracestate, tentativas FROM outbox WHERE id = :id"
        ),
        {"id": linha_id},
    ).one()
    return LinhaDaOutbox(**row._mapping)


@pytest.fixture
def reconexao_rapida(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.5)


@pytest.fixture
def broker_parado(broker: Broker) -> Iterator[Broker]:
    broker.rabbitmqctl("stop_app")
    try:
        yield broker
    finally:
        broker.rabbitmqctl("start_app")
        broker.esperar_topologia()


@pytest.mark.usefixtures("reconexao_rapida")
def test_broker_parado_nao_gasta_tentativa_e_a_linha_sai_quando_ele_volta(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker_parado: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    mensagem_id = _publicar(session_factory)
    relay = _relay(engine, broker_parado, rastreador, tmp_path)

    with EmSegundoPlano(relay):
        # Varias tentativas de conexao (heartbeat batendo) sem reivindicar nada.
        esperar_ate(lambda: (tmp_path / "relay-heartbeat").exists())
        heartbeat = (tmp_path / "relay-heartbeat").stat().st_mtime_ns
        esperar_ate(
            lambda: (tmp_path / "relay-heartbeat").stat().st_mtime_ns > heartbeat
        )
        parada = _linha(engine, mensagem_id)
        assert (parada.status, parada.tentativas) == ("pendente", 0)
        assert not (tmp_path / "relay-pronto").exists()

        broker_parado.rabbitmqctl("start_app")
        broker_parado.esperar_topologia()
        esperar_ate(
            lambda: _linha(engine, mensagem_id).status == "entregue", prazo_s=60
        )

    assert _linha(engine, mensagem_id).tentativas == 0
    propriedades, _ = esperar_ate(lambda: broker_parado.pegar(_FILA))
    assert propriedades.message_id == str(mensagem_id)


def _inserir(engine: Engine, **colunas: Any) -> int:
    """Linha da outbox montada a mao (estados e chaves que a UoW nao produz)."""
    mensagem_id = uuid4()
    correlation_id = colunas.pop("correlation_id", uuid4())
    envelope = json.loads(
        (CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text()
    )
    envelope.update(id=str(mensagem_id), correlation_id=str(correlation_id))
    valores = {
        "mensagem_id": mensagem_id,
        "correlation_id": correlation_id,
        "exchange": "pytstop.comandos",
        "routing_key": "comando.execucao.solicitar_diagnostico",
        "envelope": json.dumps(envelope),
        "status": "pendente",
        "entregue_em": None,
        **colunas,
    }
    with engine.begin() as conexao:
        linha_id: int = conexao.execute(
            text(
                "INSERT INTO outbox (mensagem_id, correlation_id, exchange, "
                "routing_key, envelope, status, entregue_em) VALUES (:mensagem_id, "
                ":correlation_id, :exchange, :routing_key, CAST(:envelope AS jsonb), "
                ":status, :entregue_em) RETURNING id"
            ),
            valores,
        ).scalar_one()
    return linha_id


def _status(engine: Engine, linha_id: int) -> str | None:
    with engine.connect() as conexao:
        status: str | None = conexao.execute(
            text("SELECT status FROM outbox WHERE id = :id"), {"id": linha_id}
        ).scalar_one_or_none()
    return status


@pytest.mark.usefixtures("session_factory")
def test_linha_em_falha_segura_as_seguintes_da_mesma_os_ate_morrer(
    engine: Engine, broker: Broker, rastreador: Rastreador, tmp_path: Path
) -> None:
    # Routing key fora da permissao de topico do usuario `os`: o broker fecha o
    # canal (403) e a linha conta tentativa; a seguinte da mesma OS espera, a de
    # outra OS segue (head-of-line por OS), e `dead` deixa de bloquear.
    ordem = uuid4()
    recusada = _inserir(
        engine, correlation_id=ordem, routing_key="comando.desconhecido.teste"
    )
    seguinte = _inserir(engine, correlation_id=ordem)
    outra_os = _inserir(engine)
    relay = _relay(engine, broker, rastreador, tmp_path, atrasos_s=(0.3,) * 4)

    with EmSegundoPlano(relay):
        esperar_ate(lambda: _status(engine, outra_os) == "entregue")
        assert _status(engine, seguinte) == "pendente"
        esperar_ate(lambda: _status(engine, recusada) == "dead")
        esperar_ate(lambda: _status(engine, seguinte) == "entregue")

    assert broker.contar(_FILA) == 2


@pytest.mark.usefixtures("session_factory")
def test_apaga_as_linhas_entregues_ha_mais_de_7_dias(
    engine: Engine, broker: Broker, rastreador: Rastreador, tmp_path: Path
) -> None:
    agora = datetime.now(UTC)
    antiga = _inserir(engine, status="entregue", entregue_em=agora - timedelta(days=8))
    recente = _inserir(engine, status="entregue", entregue_em=agora - timedelta(days=6))
    morta = _inserir(engine, status="dead")
    relay = _relay(engine, broker, rastreador, tmp_path)

    with EmSegundoPlano(relay):
        esperar_ate(lambda: _status(engine, antiga) is None)

    assert _status(engine, recente) == "entregue"
    assert _status(engine, morta) == "dead"
    # O laco ocioso e a limpeza nao abrem span.
    assert rastreador.spans() == []
