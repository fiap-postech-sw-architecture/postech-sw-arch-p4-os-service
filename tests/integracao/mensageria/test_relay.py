"""Relay da outbox contra Postgres e RabbitMQ reais, com a topologia do platform."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID, uuid4

import psycopg2
import pytest
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy import text

from src.compartilhado.infraestrutura import outbox_mapping
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo
from src.compartilhado.infraestrutura.mensageria.outbox import LinhaDaOutbox, Outbox
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_dos_cabecalhos,
)
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
    # O contexto da requisicao traz um tracestate, que segue ate o header.
    recebido = contexto_dos_cabecalhos(
        {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "kong=t61",
        }
    )
    with rastreador.tracer.start_as_current_span(
        "POST /api/v1/ordens-de-servico", context=recebido
    ):
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
    assert propriedades.headers["tracestate"] == "kong=t61"
    assert _linha(engine, mensagem_id).entregue_em is not None
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
    def now(cls, tz: tzinfo | None = None) -> Self:
        return super().now(tz) + timedelta(hours=1)


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
        desfecho = outbox.registrar_falha(_reler(engine, linha_id), "x")
        with engine.connect() as conexao:
            folga = conexao.execute(
                text(
                    "SELECT extract(epoch FROM proxima_tentativa_em - "
                    "clock_timestamp()) FROM outbox WHERE id = :id"
                ),
                {"id": linha_id},
            ).scalar_one()
        assert desfecho == "nova_tentativa", tentativas
        assert atraso - 1 < folga <= atraso, (tentativas, folga)
    assert outbox.registrar_falha(_reler(engine, linha_id), "x") == "dead"
    assert _status(engine, linha_id) == "dead"


def _reler(engine: Engine, linha_id: int) -> LinhaDaOutbox:
    """A linha como o claim a entregaria (o lease atual e o token)."""
    with engine.connect() as conexao:
        row = conexao.execute(
            text(
                "SELECT id, mensagem_id, correlation_id, exchange, routing_key, "
                "envelope, traceparent, tracestate, tentativas, "
                "proxima_tentativa_em AS lease_ate FROM outbox WHERE id = :id"
            ),
            {"id": linha_id},
        ).one()
    return LinhaDaOutbox(**row._mapping)


def _transacoes_paradas(engine: Engine) -> int:
    """Sessoes com transacao aberta e parada ha mais de 1 s, esperando o cliente.

    Transacao curta tambem fica ``idle in transaction`` entre dois comandos, mas
    por milissegundos; a que espera um publish bloqueado fica segundos.
    """
    with engine.connect() as conexao:
        total: int = conexao.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'idle in transaction' AND datname = current_database() "
                "AND now() - state_change > interval '1 second'"
            )
        ).scalar_one()
    return total


def _reconexoes(processo: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_reconexoes_ao_broker_total", {"processo": processo}
    )
    return valor or 0.0


def test_alarme_de_memoria_do_broker_nao_gasta_tentativa_nem_segura_transacao(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker_avulso: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Com o alarme, o broker bloqueia a conexao de quem publica. O relay nao
    # reivindica linha com a conexao bloqueada, o timeout do bloqueio (2 s
    # aqui, 30 s em producao) derruba a conexao como uma queda, sem contar
    # tentativa, e nenhuma transacao do banco fica aberta esperando o broker
    # (o idle_in_transaction_session_timeout a mataria).
    monkeypatch.setattr(amqp, "_BLOQUEIO_MAXIMO_S", 2)
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.5)
    mensagem_id = _publicar(session_factory)
    broker_avulso.rabbitmqctl("set_vm_memory_high_watermark", "0.0001")
    antes = _reconexoes("relay")
    paradas: list[int] = []

    def caiu_duas_vezes_sem_transacao_parada() -> bool:
        paradas.append(_transacoes_paradas(engine))
        return _reconexoes("relay") >= antes + 2

    with EmSegundoPlano(_relay(engine, broker_avulso, rastreador, tmp_path)):
        esperar_ate(caiu_duas_vezes_sem_transacao_parada, prazo_s=60)
        bloqueada = _linha(engine, mensagem_id)
        assert (bloqueada.status, bloqueada.tentativas) == ("pendente", 0)

        broker_avulso.rabbitmqctl("set_vm_memory_high_watermark", "0.4")
        esperar_ate(
            lambda: _linha(engine, mensagem_id).status == "entregue", prazo_s=60
        )

    assert max(paradas) == 0
    assert _linha(engine, mensagem_id).tentativas == 0


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


def _detalhes(engine: Engine, linha_id: int) -> Any:
    with engine.connect() as conexao:
        return conexao.execute(
            text("SELECT status, tentativas, ultimo_erro FROM outbox WHERE id = :id"),
            {"id": linha_id},
        ).one()


@pytest.mark.usefixtures("session_factory")
def test_linha_com_envelope_fora_do_contrato_vira_dead_e_o_relay_segue(
    engine: Engine, broker: Broker, rastreador: Rastreador, tmp_path: Path
) -> None:
    invalida = _inserir(engine, envelope="{}")
    valida = _inserir(engine)

    with EmSegundoPlano(_relay(engine, broker, rastreador, tmp_path)):
        esperar_ate(lambda: _status(engine, valida) == "entregue")
        esperar_ate(lambda: _status(engine, invalida) == "dead")

    linha = _detalhes(engine, invalida)
    assert (linha.tentativas, linha.ultimo_erro) == (1, "envelope fora do contrato")
    assert broker.contar(_FILA) == 1


@pytest.mark.usefixtures("session_factory")
def test_publish_em_exchange_inexistente_conta_tentativa_e_o_relay_segue(
    engine: Engine, broker: Broker, rastreador: Rastreador, tmp_path: Path
) -> None:
    # O broker fecha o canal; a linha conta tentativa ate `dead` e o relay abre
    # outro canal para as demais.
    inexistente = _inserir(engine, exchange="pytstop.inexistente")
    valida = _inserir(engine)
    relay = _relay(engine, broker, rastreador, tmp_path, atrasos_s=(0.1,) * 4)

    with EmSegundoPlano(relay):
        esperar_ate(lambda: _status(engine, valida) == "entregue")
        esperar_ate(lambda: _status(engine, inexistente) == "dead")

    linha = _detalhes(engine, inexistente)
    assert linha.tentativas == 5
    assert linha.ultimo_erro.startswith("canal fechado pelo broker")


@pytest.mark.usefixtures("session_factory")
def test_excecao_inesperada_na_publicacao_conta_tentativa_e_nao_derruba_o_relay(
    engine: Engine,
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linha_id = _inserir(engine)
    falhas = [TypeError("propriedade invalida")]
    propriedades = amqp.propriedades

    def propriedades_com_falha(*args: Any, **kwargs: Any) -> Any:
        if falhas:
            raise falhas.pop()
        return propriedades(*args, **kwargs)

    monkeypatch.setattr(amqp, "propriedades", propriedades_com_falha)
    # A nova tentativa sai 1 s depois: tempo para a espera abaixo ver a linha
    # com o erro, antes de a entrega limpar o `ultimo_erro`.
    relay = _relay(engine, broker, rastreador, tmp_path, atrasos_s=(1.0,) * 4)

    with EmSegundoPlano(relay):
        primeira = esperar_ate(
            lambda: (linha := _detalhes(engine, linha_id)).tentativas == 1 and linha
        )
        esperar_ate(lambda: _status(engine, linha_id) == "entregue")

    assert primeira.ultimo_erro == "falha ao publicar (TypeError)"
    assert _detalhes(engine, linha_id).tentativas == 1


@pytest.mark.usefixtures("session_factory")
def test_apaga_entregues_com_mais_de_7_dias_e_dead_com_mais_de_30_e_nunca_pendente(
    engine: Engine, broker: Broker, rastreador: Rastreador, tmp_path: Path
) -> None:
    entregue_antiga = _inserir(engine, status="entregue")
    _envelhecer(engine, entregue_antiga, "entregue_em", "7 days 1 minute")
    entregue_recente = _inserir(engine, status="entregue")
    _envelhecer(engine, entregue_recente, "entregue_em", "6 days 23 hours 59 minutes")
    # A janela da `dead` conta do fim do ultimo lease (a morte), nao da criacao:
    # a recente ficou pendente 10 dias antes de morrer.
    dead_antiga = _inserir(engine, status="dead")
    _envelhecer(engine, dead_antiga, "proxima_tentativa_em", "30 days 1 minute")
    dead_recente = _inserir(engine, status="dead")
    _envelhecer(engine, dead_recente, "criado_em", "40 days")
    _envelhecer(
        engine, dead_recente, "proxima_tentativa_em", "29 days 23 hours 59 minutes"
    )
    # Pendente criada ha 40 dias (e so elegivel amanha): nunca expira.
    pendente = _inserir(engine)
    _envelhecer(engine, pendente, "criado_em", "40 days")
    _envelhecer(engine, pendente, "proxima_tentativa_em", "-1 day")
    relay = _relay(engine, broker, rastreador, tmp_path)

    with EmSegundoPlano(relay):
        esperar_ate(
            lambda: (
                _status(engine, entregue_antiga) is None
                and _status(engine, dead_antiga) is None
            )
        )

    assert _status(engine, entregue_recente) == "entregue"
    assert _status(engine, dead_recente) == "dead"
    assert _status(engine, pendente) == "pendente"
    # O laco ocioso e a limpeza nao abrem span.
    assert rastreador.spans() == []


def _envelhecer(engine: Engine, linha_id: int, coluna: str, idade: str) -> None:
    """Recua a ``coluna`` da linha pelo relogio do banco (o mesmo da limpeza)."""
    assert coluna in {"entregue_em", "criado_em", "proxima_tentativa_em"}
    with engine.begin() as conexao:
        conexao.execute(
            text(
                f"UPDATE outbox SET {coluna} = now() - CAST(:idade AS interval) "  # noqa: S608  # coluna da lista acima
                "WHERE id = :id"
            ),
            {"id": linha_id, "idade": idade},
        )


@pytest.fixture
def execucao_cheia(broker: Broker) -> Iterator[None]:
    """execucao.comandos com teto de 1 mensagem e reject-publish: o broker da nack."""
    broker.rabbitmqctl(
        "set_policy",
        "--apply-to",
        "queues",
        "--priority",
        "10",
        "teste-execucao-cheia",
        "^execucao\\.comandos$",
        '{"max-length": 1, "overflow": "reject-publish"}',
    )
    try:
        esperar_ate(
            lambda: (
                "teste-execucao-cheia"
                in broker.rabbitmqctl("-q", "list_queues", "name", "policy")
            )
        )
        yield
    finally:
        broker.rabbitmqctl("clear_policy", "teste-execucao-cheia")


@pytest.mark.usefixtures("session_factory", "execucao_cheia")
def test_nack_do_broker_conta_tentativa_e_a_linha_espera_o_atraso(
    engine: Engine, broker: Broker, rastreador: Rastreador, tmp_path: Path
) -> None:
    # A fila cheia recusa a publicacao (nack): a linha conta tentativa e fica
    # pendente ate o atraso (longo aqui), sem virar entregue.
    linhas = [_inserir(engine) for _ in range(4)]
    relay = _relay(engine, broker, rastreador, tmp_path, atrasos_s=(60,) * 4)

    with EmSegundoPlano(relay):
        recusada = esperar_ate(
            lambda: next(
                (li for li in linhas if _detalhes(engine, li).tentativas == 1), None
            )
        )

    detalhes = _detalhes(engine, recusada)
    assert (detalhes.status, detalhes.ultimo_erro) == (
        "pendente",
        "recusada pelo broker (nack)",
    )


@pytest.mark.usefixtures("session_factory")
def test_relay_para_no_sinal_sem_esvaziar_a_outbox(
    engine: Engine, broker: Broker, rastreador: Rastreador, tmp_path: Path
) -> None:
    # Com lote de 1, o sinal de parada vale entre um lote e o seguinte: o
    # encerramento nao espera a outbox inteira.
    for _ in range(100):
        _inserir(engine)
    fundo = EmSegundoPlano(_relay(engine, broker, rastreador, tmp_path, lote=1))

    with fundo:
        esperar_ate(lambda: _contagem(engine, "entregue") >= 1)
        fundo.parar.set()

    assert _contagem(engine, "pendente") > 0


def _contagem(engine: Engine, status: str) -> int:
    with engine.connect() as conexao:
        total: int = conexao.execute(
            text("SELECT count(*) FROM outbox WHERE status = :status"),
            {"status": status},
        ).scalar_one()
    return total


def test_conexao_de_listen_tem_keepalives_e_timeout_de_conexao(
    engine: Engine,
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas: list[dict[str, Any]] = []
    conectar = psycopg2.connect

    def espiao(*args: Any, **kwargs: Any) -> Any:
        chamadas.append(kwargs)
        return conectar(*args, **kwargs)

    monkeypatch.setattr(psycopg2, "connect", espiao)

    with EmSegundoPlano(_relay(engine, broker, rastreador, tmp_path)):
        esperar_ate(lambda: _escutando(engine))

    (escuta,) = [kwargs for kwargs in chamadas if "keepalives" in kwargs]
    assert {chave: escuta[chave] for chave in _OPCOES_DE_LISTEN} == {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "connect_timeout": 5,
    }


_OPCOES_DE_LISTEN = (
    "keepalives",
    "keepalives_idle",
    "keepalives_interval",
    "keepalives_count",
    "connect_timeout",
)
