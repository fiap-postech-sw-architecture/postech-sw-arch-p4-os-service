"""Consumidor de os.eventos contra Postgres e RabbitMQ reais (topologia do platform).

As filas de retry do broker de teste tem TTL de 100 ms, entao o ciclo inteiro
(cinco tentativas e a DLQ) cabe num teste.
"""

from __future__ import annotations

import json
import struct
import threading
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pika
import pytest
import structlog
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pika.exceptions import NackError
from prometheus_client import REGISTRY
from sqlalchemy import text
from structlog.testing import capture_logs

from src.compartilhado.aplicacao.mensageria import (
    Comando,
    Desfecho,
    FalhaTransitoriaError,
    MensagemRecebida,
)
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria import consumidor as modulo_consumidor
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
)
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo
from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.integracao.broker import (
    SENHAS,
    TTL_DE_RETRY_MS,
    EmSegundoPlano,
    envelope_de_evento,
    esperar_ate,
)
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
)
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem
    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

_DLQ = "os.eventos.dlq"


class Espiao:
    """Handler que registra cada chamada e falha com as excecoes programadas."""

    def __init__(self, *falhas: BaseException | None) -> None:
        self._falhas = list(falhas)
        self.recebidas: list[MensagemRecebida] = []
        self.contextos: list[dict[str, Any]] = []
        self.spans: list[Any] = []

    def __call__(
        self, mensagem: MensagemRecebida, _transacao: TransacaoDaMensagem
    ) -> Desfecho:
        self.recebidas.append(mensagem)
        self.contextos.append(structlog.contextvars.get_contextvars())
        self.spans.append(trace.get_current_span().get_span_context())
        falha = self._falhas.pop(0) if self._falhas else None
        if falha is not None:
            raise falha
        return Desfecho.PROCESSADA


def _despachante(handler: Callable[..., Desfecho]) -> dict[str, Any]:
    return dict.fromkeys(catalogo().consumidos, handler)


@pytest.fixture
def consumidor(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> Callable[..., Consumidor]:
    def _criar(handler: Callable[..., Desfecho], **config: Any) -> Consumidor:
        return Consumidor(
            session_factory=session_factory,
            parametros=broker.parametros("os"),
            despachante=_despachante(handler),
            tracer=rastreador.tracer,
            config=ConfigConsumidor(
                inatividade_s=0.1, diretorio_de_saude=tmp_path, **config
            ),
        )

    return _criar


def _consumidas(tipo: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total", {"tipo": tipo, "resultado": resultado}
    )
    return valor or 0.0


def _retries() -> float:
    """Total de copias publicadas na fila de retry, de qualquer tipo."""
    return sum(
        amostra.value
        for metrica in REGISTRY.collect()
        if metrica.name == "pytstop_mensagens_consumidas"
        for amostra in metrica.samples
        if amostra.name.endswith("_total") and amostra.labels["resultado"] == "retry"
    )


def _processadas(engine: Engine) -> list[UUID]:
    with engine.connect() as conexao:
        return list(
            conexao.execute(text("SELECT mensagem_id FROM mensagens_processadas"))
            .scalars()
            .all()
        )


def test_evento_e_processado_uma_vez_e_a_reentrega_do_mesmo_id_nao_repete(
    engine: Engine,
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    tmp_path: Path,
) -> None:
    espiao = Espiao()
    envelope = envelope_de_evento("DiagnosticoIniciado")
    antes = (
        _consumidas("DiagnosticoIniciado", "processada"),
        _consumidas("DiagnosticoIniciado", "duplicada"),
    )

    with EmSegundoPlano(consumidor(espiao)):
        esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
        broker.publicar_evento(envelope)
        broker.publicar_evento(envelope)
        esperar_ate(
            lambda: _consumidas("DiagnosticoIniciado", "duplicada") == antes[1] + 1
        )

    assert not (tmp_path / "consumidor-pronto").exists()
    (mensagem,) = espiao.recebidas
    assert mensagem.id == UUID(envelope["id"])
    assert mensagem.tipo == "DiagnosticoIniciado"
    assert mensagem.origem == "execution-service"
    assert mensagem.correlation_id == UUID(envelope["correlation_id"])
    assert mensagem.dados == envelope["dados"]
    assert _processadas(engine) == [mensagem.id]
    assert _consumidas("DiagnosticoIniciado", "processada") == antes[0] + 1
    assert broker.contar("os.eventos") == 0
    assert broker.contar(_DLQ) == 0


def test_span_e_log_do_consumo_sao_filhos_do_traceparent_recebido(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    rastreador: Rastreador,
) -> None:
    espiao = Espiao()
    envelope = envelope_de_evento("PecasReservadas")
    with rastreador.tracer.start_as_current_span("publish PecasReservadas") as origem:
        cabecalhos = {"traceparent": traceparent(origem)}

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope, cabecalhos=cabecalhos)
        esperar_ate(lambda: rastreador.spans("process PecasReservadas"))

    (consumo,) = rastreador.spans("process PecasReservadas")
    assert consumo.kind is SpanKind.CONSUMER
    assert consumo.parent is not None
    assert consumo.parent.span_id == origem.get_span_context().span_id
    assert consumo.get_span_context().trace_id == origem.get_span_context().trace_id
    assert consumo.attributes is not None
    assert consumo.attributes["correlation_id"] == envelope["correlation_id"]
    # O handler roda dentro do span do consumo e com o correlation_id nos logs.
    assert espiao.spans == [consumo.get_span_context()]
    (contexto,) = espiao.contextos
    assert contexto["correlation_id"] == envelope["correlation_id"]
    assert contexto["message_id"] == envelope["id"]


@pytest.fixture
def publicacoes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    """Espiona o que o consumidor publica (o broker recebe tudo de verdade)."""
    registro: list[tuple[str, str, Any]] = []
    conectar = amqp.conectar

    def conectar_com_espiao(params: Any) -> tuple[Any, Any]:
        conexao, canal = conectar(params)
        publicar = canal.basic_publish

        def basic_publish(
            exchange: str,
            routing_key: str,
            body: bytes,
            properties: Any = None,
            mandatory: bool = False,
        ) -> None:
            registro.append((exchange, routing_key, properties))
            publicar(exchange, routing_key, body, properties, mandatory)

        canal.basic_publish = basic_publish
        return conexao, canal

    monkeypatch.setattr(amqp, "conectar", conectar_com_espiao)
    return registro


def test_erro_transitorio_passa_pelas_cinco_filas_de_retry_e_a_sexta_falha_e_dlq(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    publicacoes: list[tuple[str, str, Any]],
    rastreador: Rastreador,
) -> None:
    espiao = Espiao(*[FalhaTransitoriaError("banco fora")] * 6)
    envelope = envelope_de_evento("OrcamentoGerado")
    antes = (
        _consumidas("OrcamentoGerado", "retry"),
        _consumidas("OrcamentoGerado", "dlq"),
    )
    retries = _retries()
    with rastreador.tracer.start_as_current_span("publish OrcamentoGerado") as origem:
        cabecalhos = {"traceparent": traceparent(origem), "tracestate": "billing=t61"}

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope, cabecalhos=cabecalhos)
        morta = esperar_ate(lambda: broker.pegar(_DLQ))

    assert len(espiao.recebidas) == 6
    assert {m.id for m in espiao.recebidas} == {UUID(envelope["id"])}
    niveis = ["1s", "5s", "15s", "60s", "300s"]
    assert [(e, r) for e, r, _ in publicacoes] == [
        ("pytstop.retry", f"os.eventos.retry.{nivel}") for nivel in niveis
    ]
    passadas = sorted(
        rastreador.spans("process OrcamentoGerado"), key=lambda s: s.start_time or 0
    )
    assert len(passadas) == 6
    for tentativa, (_, _, copia) in enumerate(publicacoes, start=1):
        assert copia.headers["x-tentativa"] == tentativa
        # O atraso e o TTL da fila do nivel: a copia nao leva expiration.
        assert copia.expiration is None
        assert copia.user_id == "os"
        # O resto e o da original.
        assert copia.message_id == envelope["id"]
        assert copia.correlation_id == envelope["correlation_id"]
        assert copia.type == "OrcamentoGerado"
        assert copia.content_type == "application/json"
        assert copia.delivery_mode == 2
        # Mesmo trace: a copia leva o contexto do consumo que a gerou, e a
        # passada seguinte e filha dele.
        assert copia.headers["traceparent"] == traceparent(passadas[tentativa - 1])
        assert copia.headers["tracestate"] == "billing=t61"
        seguinte = passadas[tentativa]
        assert seguinte.parent is not None
        assert seguinte.parent.span_id == passadas[tentativa - 1].context.span_id
    assert {p.context.trace_id for p in passadas} == {
        origem.get_span_context().trace_id
    }
    propriedades, corpo = morta
    assert json.loads(corpo) == envelope
    assert propriedades.headers["x-tentativa"] == 5
    # A quinta copia passou mesmo pela fila de 300 s antes de voltar.
    mortes = {(m["queue"], m["reason"]) for m in propriedades.headers["x-death"]}
    assert ("os.eventos.retry.300s", "expired") in mortes
    assert ("os.eventos", "rejected") in mortes
    assert _consumidas("OrcamentoGerado", "retry") == antes[0] + 5
    assert _retries() == retries + 5
    assert _consumidas("OrcamentoGerado", "dlq") == antes[1] + 1


@pytest.fixture
def retry_1s_sem_rota(broker: Broker) -> Iterator[None]:
    with broker.canal() as canal:
        canal.queue_unbind(
            "os.eventos.retry.1s", "pytstop.retry", "os.eventos.retry.1s"
        )
    try:
        yield
    finally:
        with broker.canal() as canal:
            canal.queue_bind(
                "os.eventos.retry.1s", "pytstop.retry", "os.eventos.retry.1s"
            )


@pytest.mark.usefixtures("retry_1s_sem_rota")
def test_copia_de_retry_sem_rota_manda_a_original_para_a_dlq(
    broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    # Sem fila para a routing key, a copia com mandatory volta: a original nao
    # recebe ack e vai para a DLQ, nunca some (sem mandatory o broker
    # confirmaria a copia e a descartaria).
    espiao = Espiao(FalhaTransitoriaError("dependencia fora"))
    envelope = envelope_de_evento("PecasReservadas")
    antes = _consumidas("PecasReservadas", "dlq")
    retries = _retries()

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope)
        propriedades, _ = esperar_ate(lambda: broker.pegar(_DLQ))

    assert propriedades.message_id == envelope["id"]
    assert len(espiao.recebidas) == 1
    assert broker.contar("os.eventos") == 0
    assert broker.contar("os.eventos.retry.1s") == 0
    assert _consumidas("PecasReservadas", "dlq") == antes + 1
    assert _retries() == retries


@pytest.fixture
def retry_1s_cheia(broker: Broker) -> Iterator[None]:
    """os.eventos.retry.1s cheia: teto de 1 mensagem e ``reject-publish``.

    A fila e redeclarada com TTL de 10 min (com o de 100 ms do broker de teste
    ela esvaziaria entre uma publicacao e outra) e recebe mensagens ate o broker
    recusar (a fila quorum aceita uma alem do teto). O teardown a recria como no
    definitions de teste.
    """
    fila = "os.eventos.retry.1s"
    definicoes = json.loads((CONTRATOS / "rabbitmq/definitions.json").read_text())
    (original,) = [f for f in definicoes["queues"] if f["name"] == fila]

    def recriar(argumentos: dict[str, Any]) -> None:
        with broker.canal() as canal:
            canal.queue_delete(fila)
            canal.queue_declare(fila, durable=True, arguments=argumentos)
            canal.queue_bind(fila, "pytstop.retry", fila)

    recriar(
        {
            **original["arguments"],
            "x-message-ttl": 600_000,
            "x-max-length": 1,
            "x-overflow": "reject-publish",
        }
    )
    try:
        with broker.canal() as canal:
            for _ in range(5):
                try:
                    canal.basic_publish("pytstop.retry", fila, b"{}", mandatory=True)
                except NackError:
                    break
            else:
                pytest.fail("a fila de retry nao encheu")
        yield
    finally:
        recriar({**original["arguments"], "x-message-ttl": TTL_DE_RETRY_MS})


@pytest.mark.usefixtures("retry_1s_cheia")
def test_copia_de_retry_recusada_pela_fila_cheia_manda_a_original_para_a_dlq(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fila de retry cheia recusa a copia (nack): a original nao recebe ack e
    # vai para a DLQ, nunca some.
    espiao = Espiao(FalhaTransitoriaError("dependencia fora"))
    envelope = envelope_de_evento("PecasReservadas")
    antes = _consumidas("PecasReservadas", "dlq")
    retries = _retries()
    monkeypatch.setattr(modulo_consumidor, "_log", structlog.get_logger())

    with capture_logs() as logs, EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope)
        propriedades, _ = esperar_ate(lambda: broker.pegar(_DLQ))

    assert propriedades.message_id == envelope["id"]
    assert len(espiao.recebidas) == 1
    assert broker.contar("os.eventos") == 0
    assert _consumidas("PecasReservadas", "dlq") == antes + 1
    assert _retries() == retries
    assert [
        (log["event"], log.get("erro")) for log in logs if "dlq" in log["event"]
    ] == [("retry copy refused by the broker; rejected to dlq", "NackError")]


def test_falha_transitoria_volta_da_retry_e_e_processada_uma_vez(
    engine: Engine, broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    espiao = Espiao(FalhaTransitoriaError("dependencia fora"))
    envelope = envelope_de_evento("ExecucaoIniciada")
    antes = _consumidas("ExecucaoIniciada", "processada")

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: _consumidas("ExecucaoIniciada", "processada") > antes)

    assert len(espiao.recebidas) == 2
    # A primeira falha desfez a transacao: so a segunda passada ficou registrada.
    assert _processadas(engine) == [UUID(envelope["id"])]


def test_erro_permanente_do_handler_vai_direto_para_a_dlq(
    engine: Engine, broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    espiao = Espiao(KeyError("bug"))
    envelope = envelope_de_evento("ReservaLiberada")

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    assert len(espiao.recebidas) == 1
    assert _processadas(engine) == []


class _Saga:
    """Handler como o da saga: a OS vai a EM_DIAGNOSTICO e sai um GerarOrcamento.

    ``depois`` roda dentro do handler, depois do efeito e do comando.
    """

    def __init__(self, depois: Callable[[TransacaoDaMensagem], Any]) -> None:
        self.chamadas = 0
        self._depois = depois

    def __call__(
        self, mensagem: MensagemRecebida, transacao: TransacaoDaMensagem
    ) -> Any:  # um dos casos devolve o que nao e Desfecho
        self.chamadas += 1
        ordens = OrdemDeServicoSQLAlchemyRepository(session=transacao.session)
        ordem = ordens.obter_por_id(mensagem.correlation_id)
        assert ordem is not None
        ordem.registrar_diagnostico_iniciado()
        ordens.salvar(ordem)
        transacao.publicar_comando(
            Comando.GERAR_ORCAMENTO,
            {
                "ordem_id": ordem.id,
                "itens": [{"tipo": "servico", "codigo": "SRV-01", "quantidade": 1}],
            },
            correlation_id=ordem.id,
            causation_id=mensagem.id,
        )
        return self._depois(transacao)


def _ordem_recebida(session_factory: sessionmaker[Session]) -> UUID:
    with session_factory() as sessao:
        cliente = criar_cliente_com_veiculo(sessao)
        ordem = criar_ordem_recebida(
            sessao, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
        )
        sessao.commit()
    return ordem.id


def _gravado(engine: Engine, ordem_id: UUID) -> Any:
    """Status da OS, linhas da outbox e de processadas, e o xmin de cada uma."""
    with engine.connect() as conexao:
        return conexao.execute(
            text(
                "SELECT o.status, o.xmin::text AS xmin_os, "
                "(SELECT count(*) FROM outbox) AS comandos, "
                "(SELECT min(xmin::text) FROM outbox) AS xmin_comando, "
                "(SELECT count(*) FROM mensagens_processadas) AS processadas, "
                "(SELECT min(xmin::text) FROM mensagens_processadas) "
                "AS xmin_processada "
                "FROM ordens_de_servico o WHERE o.id = :id"
            ),
            {"id": ordem_id},
        ).one()


def test_efeito_comando_e_registro_da_mensagem_entram_num_commit_so_do_consumidor(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    consumidor: Callable[..., Consumidor],
) -> None:
    ordem_id = _ordem_recebida(session_factory)
    saga = _Saga(depois=lambda _t: Desfecho.PROCESSADA)
    envelope = envelope_de_evento("DiagnosticoIniciado", correlation_id=ordem_id)

    with EmSegundoPlano(consumidor(saga)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: _gravado(engine, ordem_id).processadas == 1)

    gravado = _gravado(engine, ordem_id)
    assert (gravado.status, gravado.comandos) == ("em_diagnostico", 1)
    # Mesmo xmin: as tres linhas sao da mesma transacao.
    assert gravado.xmin_os == gravado.xmin_comando == gravado.xmin_processada
    with engine.connect() as conexao:
        comando = conexao.execute(text("SELECT envelope FROM outbox")).scalar_one()
    assert comando["tipo"] == "GerarOrcamento"
    assert comando["causation_id"] == envelope["id"]


def _consulta_num_savepoint(transacao: TransacaoDaMensagem) -> Desfecho:
    with transacao.session.begin_nested():
        transacao.session.execute(text("SELECT 1"))
    return Desfecho.PROCESSADA


def test_savepoint_do_handler_fica_na_transacao_da_mensagem(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    consumidor: Callable[..., Consumidor],
) -> None:
    # Liberar o savepoint dispara o before_commit da sessao, e nao e o commit
    # que o consumidor recusa.
    ordem_id = _ordem_recebida(session_factory)
    saga = _Saga(depois=_consulta_num_savepoint)
    envelope = envelope_de_evento("DiagnosticoIniciado", correlation_id=ordem_id)

    with EmSegundoPlano(consumidor(saga)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: _gravado(engine, ordem_id).processadas == 1)

    gravado = _gravado(engine, ordem_id)
    assert (gravado.status, gravado.comandos, broker.contar(_DLQ)) == (
        "em_diagnostico",
        1,
        0,
    )
    assert gravado.xmin_os == gravado.xmin_comando == gravado.xmin_processada


@pytest.fixture
def commit_que_falha_uma_vez(engine: Engine) -> Iterator[None]:
    """O banco recusa o primeiro commit que leva uma linha da outbox.

    Constraint trigger adiada: o erro (40001, transitorio) vem no COMMIT, depois
    de o handler ja ter gravado efeito e comando na transacao.
    """
    with engine.begin() as conexao:
        conexao.execute(text("CREATE SEQUENCE falha_no_commit"))
        conexao.execute(
            text(
                "CREATE FUNCTION falhar_no_primeiro_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "IF nextval('falha_no_commit') = 1 THEN "
                "RAISE EXCEPTION 'falha no commit' USING ERRCODE = '40001'; END IF; "
                "RETURN NULL; END $$"
            )
        )
        conexao.execute(
            text(
                "CREATE CONSTRAINT TRIGGER falha_no_commit AFTER INSERT ON outbox "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION falhar_no_primeiro_commit()"
            )
        )
    try:
        yield
    finally:
        with engine.begin() as conexao:
            conexao.execute(text("DROP TRIGGER falha_no_commit ON outbox"))
            conexao.execute(text("DROP FUNCTION falhar_no_primeiro_commit()"))
            conexao.execute(text("DROP SEQUENCE falha_no_commit"))


@pytest.mark.usefixtures("commit_que_falha_uma_vez")
def test_falha_no_commit_depois_do_handler_desfaz_tudo_e_a_mensagem_volta_pela_retry(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    consumidor: Callable[..., Consumidor],
) -> None:
    ordem_id = _ordem_recebida(session_factory)
    saga = _Saga(depois=lambda _t: Desfecho.PROCESSADA)
    envelope = envelope_de_evento("DiagnosticoIniciado", correlation_id=ordem_id)
    retries = _consumidas("DiagnosticoIniciado", "retry")

    with EmSegundoPlano(consumidor(saga)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: _gravado(engine, ordem_id).processadas == 1)

    gravado = _gravado(engine, ordem_id)
    # A primeira passada foi desfeita inteira: uma transicao e um comando so.
    assert saga.chamadas == 2
    assert (gravado.status, gravado.comandos) == ("em_diagnostico", 1)
    assert _consumidas("DiagnosticoIniciado", "retry") == retries + 1
    with engine.connect() as conexao:
        transicoes = conexao.execute(
            text("SELECT count(*) FROM historico_status_ordem WHERE ordem_id = :id"),
            {"id": ordem_id},
        ).scalar_one()
    assert transicoes == 2  # abertura (RECEBIDA) e EM_DIAGNOSTICO


def _comitar_no_savepoint(transacao: TransacaoDaMensagem) -> Desfecho:
    with transacao.session.begin_nested():
        transacao.session.commit()
    return Desfecho.PROCESSADA


@pytest.mark.parametrize(
    ("depois", "motivo"),
    [
        pytest.param(lambda t: t.session.commit(), "commit", id="handler-comita"),
        pytest.param(
            _comitar_no_savepoint,
            "commit no savepoint",
            id="handler-comita-dentro-do-savepoint",
        ),
        pytest.param(
            lambda t: (t.session.rollback(), Desfecho.PROCESSADA)[1],
            "rollback",
            id="handler-encerra-a-transacao",
        ),
        pytest.param(lambda _t: None, "desfecho", id="handler-devolve-nada"),
    ],
)
def test_handler_que_comita_encerra_a_transacao_ou_nao_devolve_desfecho_vai_para_a_dlq(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    depois: Callable[[TransacaoDaMensagem], Any],
    motivo: str,
) -> None:
    ordem_id = _ordem_recebida(session_factory)
    saga = _Saga(depois=depois)
    envelope = envelope_de_evento("DiagnosticoIniciado", correlation_id=ordem_id)

    with EmSegundoPlano(consumidor(saga)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    gravado = _gravado(engine, ordem_id)
    assert saga.chamadas == 1, motivo
    assert (gravado.status, gravado.comandos, gravado.processadas) == (
        "recebida",
        0,
        0,
    )


def _caso(
    id_: str, montar: Callable[[Broker, dict[str, Any]], None], motivo: str
) -> Any:
    return pytest.param(montar, motivo, id=id_)


@pytest.mark.parametrize(
    ("montar", "motivo"),
    [
        # Origem (o `user_id` que o broker confere contra a conexao).
        _caso(
            "user-id-divergente",
            lambda b, e: b.publicar_evento(
                e, usuario="execucao", routing_key="evento.execucao.orcamento_gerado"
            ),
            "produtor_divergente",
        ),
        _caso(
            "copia-de-retry-sem-tentativa",
            lambda b, e: b.publicar_evento(
                e,
                usuario="os",
                exchange="pytstop.retry",
                routing_key="os.eventos.retry.1s",
            ),
            "produtor_divergente",
        ),
        _caso(
            "x-tentativa-forjado-por-outro-produtor",
            lambda b, e: b.publicar_evento(
                e,
                usuario="execucao",
                routing_key="evento.execucao.orcamento_gerado",
                cabecalhos={"x-tentativa": 1},
            ),
            "produtor_divergente",
        ),
        _caso(
            "billing-com-o-tipo-da-execucao-e-tentativa-5",
            lambda b, e: b.publicar_evento(
                e,
                routing_key="evento.billing.execucao_iniciada",
                tipo="ExecucaoIniciada",
                cabecalhos={"x-tentativa": 5},
            ),
            "produtor_divergente",
        ),
        _caso(
            "sem-user-id",
            lambda b, e: b.publicar_evento(e, sem_user_id=True),
            "produtor_divergente",
        ),
        _caso(
            "tipo-desconhecido",
            lambda b, e: b.publicar_evento(
                e,
                usuario="execucao",
                routing_key="evento.execucao.inexistente",
                tipo="Inexistente",
            ),
            "tipo_desconhecido",
        ),
        # Corpo e contrato.
        _caso(
            "versao-2",
            lambda b, e: b.publicar_evento({**e, "versao": 2}),
            "contrato_invalido",
        ),
        _caso(
            "dados-invalidos",
            lambda b, e: b.publicar_evento({**e, "dados": {"ordem_id": e["id"]}}),
            "contrato_invalido",
        ),
        _caso(
            "data-com-quebra-de-linha-no-fim",
            lambda b, e: b.publicar_evento(
                {**e, "ocorrido_em": e["ocorrido_em"] + "\n"}
            ),
            "contrato_invalido",
        ),
        _caso(
            "json-invalido",
            lambda b, e: b.publicar_evento(e, corpo=b"{nao e json"),
            "json_invalido",
        ),
        _caso(
            "corpo-acima-do-teto",
            lambda b, e: b.publicar_evento(
                {**e, "dados": {**e["dados"], "anexo": "x" * 70_000}}
            ),
            "corpo_grande_demais",
        ),
        # Propriedades AMQP x envelope.
        _caso(
            "message-id-divergente",
            lambda b, e: b.publicar_evento(e, message_id=str(uuid4())),
            "propriedades_divergentes",
        ),
        _caso(
            "correlation-id-divergente",
            lambda b, e: b.publicar_evento(e, correlation_id=str(uuid4())),
            "propriedades_divergentes",
        ),
        _caso(
            "type-diferente-do-tipo-do-envelope",
            lambda b, e: b.publicar_evento(
                e,
                routing_key="evento.billing.orcamento_aprovado",
                tipo="OrcamentoAprovado",
            ),
            "propriedades_divergentes",
        ),
        # x-tentativa que nao e inteiro de 0 a 5.
        *[
            _caso(
                f"tentativa-{nome}",
                lambda b, e, valor=valor: b.publicar_evento(
                    e, cabecalhos={"x-tentativa": valor}
                ),
                "tentativa_invalida",
            )
            for nome, valor in [
                ("6", 6),
                ("negativa", -1),
                ("booleana", True),
                ("texto", "1"),
                ("decimal", Decimal("1.5")),
                ("nula", None),
            ]
        ],
    ],
)
def test_mensagem_fora_do_contrato_ou_da_origem_vai_direto_para_a_dlq(
    engine: Engine,
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    montar: Callable[[Broker, dict[str, Any]], None],
    motivo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    espiao = Espiao()
    # OrcamentoGerado e do billing: os casos acima mexem em um aspecto so.
    envelope = envelope_de_evento("OrcamentoGerado")
    retries = _retries()
    monkeypatch.setattr(modulo_consumidor, "_log", structlog.get_logger())

    with capture_logs() as logs, EmSegundoPlano(consumidor(espiao)):
        montar(broker, envelope)
        propriedades, _ = esperar_ate(lambda: broker.pegar(_DLQ))

    assert espiao.recebidas == []
    assert _processadas(engine) == []
    # Recusa classificada, com o motivo (nunca o caminho de falha inesperada).
    assert [
        (log["event"], log.get("motivo")) for log in logs if "dlq" in log["event"]
    ] == [("message rejected to dlq", motivo)]
    # Direto: o consumidor nao republicou (com 100 ms por nivel, um desvio pelas
    # cinco filas de retry tambem chegaria a DLQ dentro do prazo do teste).
    assert _retries() == retries
    rejeicoes = [
        morte["count"]
        for morte in propriedades.headers["x-death"]
        if (morte["queue"], morte["reason"]) == ("os.eventos", "rejected")
    ]
    assert rejeicoes == [1]


def test_apaga_as_mensagens_processadas_ha_mais_de_30_dias(
    engine: Engine, consumidor: Callable[..., Consumidor]
) -> None:
    antiga, recente = uuid4(), uuid4()
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO mensagens_processadas (mensagem_id, processada_em) VALUES "
                "(:antiga, now() - interval '30 days 1 minute'), "
                "(:recente, now() - interval '29 days 23 hours 59 minutes')"
            ),
            {"antiga": antiga, "recente": recente},
        )

    with EmSegundoPlano(consumidor(Espiao())):
        esperar_ate(lambda: antiga not in _processadas(engine))

    assert _processadas(engine) == [recente]


def test_encerramento_conclui_a_mensagem_em_curso_e_devolve_as_pre_buscadas(
    engine: Engine, broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    em_curso = threading.Event()
    liberar = threading.Event()

    def lento(mensagem: MensagemRecebida, _transacao: TransacaoDaMensagem) -> Desfecho:
        em_curso.set()
        liberar.wait(10)
        return Desfecho.PROCESSADA

    primeira = envelope_de_evento("ExecucaoAgendada")
    segunda = envelope_de_evento("ExecucaoAgendada")
    processo = EmSegundoPlano(consumidor(lento))
    with processo:
        broker.publicar_evento(primeira)
        broker.publicar_evento(segunda)
        esperar_ate(em_curso.is_set)
        processo.parar.set()
        liberar.set()

    assert _processadas(engine) == [UUID(primeira["id"])]
    propriedades, _ = esperar_ate(lambda: broker.pegar("os.eventos"))
    assert propriedades.message_id == segunda["id"]


@pytest.fixture
def broker_parado(broker: Broker) -> Iterator[Broker]:
    broker.rabbitmqctl("stop_app")
    try:
        yield broker
    finally:
        broker.rabbitmqctl("start_app")
        broker.esperar_topologia()


def test_sobe_sem_broker_fica_fora_de_pronto_e_consome_quando_ele_volta(
    broker_parado: Broker,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.5)
    espiao = Espiao()
    envelope = envelope_de_evento("PagamentoConfirmado")

    with EmSegundoPlano(consumidor(espiao)):
        esperar_ate(lambda: (tmp_path / "consumidor-heartbeat").exists())
        assert not (tmp_path / "consumidor-pronto").exists()
        broker_parado.rabbitmqctl("start_app")
        broker_parado.esperar_topologia()
        esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists(), prazo_s=30)
        broker_parado.publicar_evento(envelope)
        esperar_ate(lambda: espiao.recebidas)

    assert [m.id for m in espiao.recebidas] == [UUID(envelope["id"])]


def test_conexao_derrubada_pelo_broker_reconecta_e_segue_consumindo(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.5)
    espiao = Espiao()
    primeiro = envelope_de_evento("DiagnosticoConcluido")
    segundo = envelope_de_evento("DiagnosticoConcluido")

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(primeiro)
        esperar_ate(lambda: len(espiao.recebidas) == 1)
        broker.rabbitmqctl("close_all_connections", "teste de queda")
        broker.publicar_evento(segundo)
        esperar_ate(lambda: len(espiao.recebidas) == 2, prazo_s=30)

    assert [m.id for m in espiao.recebidas] == [
        UUID(primeiro["id"]),
        UUID(segundo["id"]),
    ]


def test_json_aninhado_sem_fim_vai_para_a_dlq_sem_derrubar_o_consumidor(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dentro do teto de 64 KiB o parser nao chega a estourar a pilha; acima
    # dele (o teto e uma constante), o RecursionError tambem vira recusa.
    monkeypatch.setattr(modulo_consumidor, "_CORPO_MAXIMO_BYTES", 1024 * 1024)
    monkeypatch.setattr(modulo_consumidor, "_log", structlog.get_logger())
    espiao = Espiao()
    envelope = envelope_de_evento("OrcamentoGerado")

    with capture_logs() as logs, EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope, corpo=b"[" * 200_000 + b"]" * 200_000)
        broker.publicar_evento(envelope_de_evento("OrcamentoGerado"))
        esperar_ate(lambda: espiao.recebidas)
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    assert ("message rejected to dlq", "json_invalido") in [
        (log["event"], log.get("motivo")) for log in logs
    ]


class _CabecalhoIlegivel(pika.BasicProperties):
    """Header de tipo timestamp (``T``) com o epoch em milissegundos.

    O broker repassa a mensagem, e o decoder do pika levanta ``ValueError`` ao
    ler o frame: a conexao do consumidor cai a cada entrega dela, antes de o
    codigo do servico a ver.
    """

    def encode(self) -> list[bytes]:
        # O frame comeca pelas flags; sem content_type, os headers vem logo
        # depois delas.
        guardados = self.content_type, self.headers
        self.content_type = self.headers = None
        try:
            flags, *pecas = super().encode()
        finally:
            self.content_type, self.headers = guardados
        chave: list[bytes] = []
        pika.data.encode_short_string(chave, "x-ilegivel")
        tabela = b"".join(chave) + b"T" + struct.pack(">Q", 1_760_000_000_000)
        (valor,) = struct.unpack(">H", flags)
        return [
            struct.pack(">H", valor | pika.spec.BasicProperties.FLAG_HEADERS),
            struct.pack(">I", len(tabela)) + tabela,
            *pecas,
        ]


def _reconexoes() -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_reconexoes_ao_broker_total", {"processo": "consumidor"}
    )
    return valor or 0.0


def test_mensagem_que_o_pika_nao_le_sai_pelo_delivery_limit_e_a_seguinte_e_processada(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Com prefetch 1 so a ilegivel derruba a conexao; a cada queda ela volta a
    # fila, e o delivery-limit 5 da policy do platform a manda para a DLQ. A
    # valida que vinha atras nao e arrastada junto.
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.2)
    espiao = Espiao()
    ilegivel = envelope_de_evento("PagamentoConfirmado")
    valida = envelope_de_evento("PagamentoConfirmado")
    antes = _reconexoes()

    broker.publicar_evento(ilegivel, propriedades=_CabecalhoIlegivel)
    broker.publicar_evento(valida)
    with EmSegundoPlano(consumidor(espiao)):
        esperar_ate(lambda: espiao.recebidas, prazo_s=60)
        esperar_ate(lambda: broker.contar(_DLQ) == 1, prazo_s=60)

    assert [m.id for m in espiao.recebidas] == [UUID(valida["id"])]
    assert broker.contar("os.eventos") == 0
    assert _reconexoes() > antes
    # Lida pelo management (o pika nao a decodifica nem na DLQ).
    codigo, saida = broker.container.get_wrapped_container().exec_run(
        [
            "rabbitmqadmin",
            "--username",
            "admin",
            "--password",
            SENHAS["admin"],
            "get",
            "messages",
            "--queue",
            _DLQ,
            "--count",
            "1",
            "--ack-mode",
            "ack_requeue_true",
        ],
        user="999:999",
    )
    assert codigo == 0
    assert "delivery_limit" in saida.decode()


def _fila(broker: Broker, nome: str) -> tuple[int, int]:
    """Mensagens prontas e sem ack da fila (pelo rabbitmqctl)."""
    saida = broker.rabbitmqctl(
        "-q", "list_queues", "name", "messages_ready", "messages_unacknowledged"
    )
    for linha in saida.splitlines():
        campos = linha.split()
        if campos and campos[0] == nome:
            return int(campos[1]), int(campos[2])
    raise AssertionError(nome)


def test_consumidor_segura_uma_mensagem_por_vez(
    broker: Broker, consumidor: Callable[..., Consumidor]
) -> None:
    em_curso, liberar = threading.Event(), threading.Event()

    def lento(_m: MensagemRecebida, _t: TransacaoDaMensagem) -> Desfecho:
        em_curso.set()
        liberar.wait(20)
        return Desfecho.PROCESSADA

    try:
        with EmSegundoPlano(consumidor(lento)):
            for _ in range(3):
                broker.publicar_evento(envelope_de_evento("ExecucaoAgendada"))
            esperar_ate(em_curso.is_set)
            esperar_ate(lambda: _fila(broker, "os.eventos") == (2, 1))
    finally:
        liberar.set()


@pytest.fixture
def os_sem_leitura_da_fila(broker: Broker) -> Iterator[Callable[[], None]]:
    """Tira do usuario `os` a leitura de os.eventos; devolve quem a restaura."""
    permissoes = json.loads((CONTRATOS / "rabbitmq/permissoes.json").read_text())
    (do_os,) = [p for p in permissoes["permissions"] if p["user"] == "os"]

    def restaurar() -> None:
        broker.rabbitmqctl(
            "set_permissions",
            "-p",
            "/",
            "os",
            do_os["configure"],
            do_os["write"],
            do_os["read"],
        )

    broker.rabbitmqctl(
        "set_permissions", "-p", "/", "os", do_os["configure"], do_os["write"], "^\\z"
    )
    try:
        yield restaurar
    finally:
        restaurar()


@pytest.fixture
def prontos(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Cada vez que um processo se marca pronto.

    Um pronto marcado e desfeito na mesma volta do laco dura milissegundos no
    disco; a lista o registra mesmo assim.
    """
    marcados: list[Path] = []
    original = Sinalizador.marcar_pronto

    def marcar(sinal: Sinalizador) -> None:
        marcados.append(sinal.pronto)
        original(sinal)

    monkeypatch.setattr(Sinalizador, "marcar_pronto", marcar)
    return marcados


def test_fila_sem_permissao_no_boot_espera_e_consome_quando_ela_volta(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    os_sem_leitura_da_fila: Callable[[], None],
    prontos: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A declaracao passiva de os.eventos recusada (403: sem leitura nem
    # configuracao da fila) deixa o consumidor fora de pronto, tentando de novo
    # com backoff, sem derrubar o processo.
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.2)
    espiao = Espiao()
    envelope = envelope_de_evento("ReservaLiberada")
    antes = _reconexoes()

    with EmSegundoPlano(consumidor(espiao)):
        esperar_ate(lambda: _reconexoes() >= antes + 2)
        assert prontos == []
        os_sem_leitura_da_fila()
        broker.publicar_evento(envelope)
        esperar_ate(lambda: espiao.recebidas, prazo_s=30)

    assert [m.id for m in espiao.recebidas] == [UUID(envelope["id"])]
    assert prontos == [tmp_path / "consumidor-pronto"]


@pytest.fixture
def sem_exchange_de_retry(broker: Broker) -> Iterator[Callable[[], None]]:
    """Apaga o pytstop.retry; devolve quem o recria com as ligacoes do contrato."""
    definicoes = json.loads((CONTRATOS / "rabbitmq/definitions.json").read_text())
    (exchange,) = [e for e in definicoes["exchanges"] if e["name"] == "pytstop.retry"]
    ligacoes = [b for b in definicoes["bindings"] if b["source"] == exchange["name"]]

    def recriar() -> None:
        with broker.canal() as canal:
            canal.exchange_declare(
                exchange["name"],
                exchange_type=exchange["type"],
                durable=exchange["durable"],
            )
            for ligacao in ligacoes:
                canal.queue_bind(
                    ligacao["destination"], exchange["name"], ligacao["routing_key"]
                )

    with broker.canal() as canal:
        canal.exchange_delete(exchange["name"])
    try:
        yield recriar
    finally:
        recriar()


def test_exchange_de_retry_ausente_no_boot_espera_e_consome_quando_ele_volta(
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    sem_exchange_de_retry: Callable[[], None],
    prontos: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Sem o pytstop.retry a copia de retry nao teria destino: a declaracao
    # passiva recusada (404) deixa o consumidor fora de pronto e sem consumir.
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.2)
    espiao = Espiao()
    envelope = envelope_de_evento("ReservaLiberada")
    antes = _reconexoes()

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: _reconexoes() >= antes + 2)
        assert (prontos, espiao.recebidas) == ([], [])
        sem_exchange_de_retry()
        esperar_ate(lambda: espiao.recebidas, prazo_s=30)

    assert [m.id for m in espiao.recebidas] == [UUID(envelope["id"])]
    assert prontos == [tmp_path / "consumidor-pronto"]


def test_handler_mais_lento_que_o_heartbeat_tem_efeito_uma_vez_e_nada_se_perde(
    engine: Engine,
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O handler roda na thread da conexao: enquanto ele trabalha, nenhum
    # heartbeat sai (1 s aqui). Se o broker derrubar a conexao, o commit ja
    # aconteceu: a mensagem volta, vira duplicada e recebe ack sem repetir o
    # efeito nem passar pela escada de retry.
    monkeypatch.setattr(amqp, "_HEARTBEAT_S", 1)
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.2)
    chamadas: list[UUID] = []

    def lento(mensagem: MensagemRecebida, _t: TransacaoDaMensagem) -> Desfecho:
        chamadas.append(mensagem.id)
        time.sleep(6)
        return Desfecho.PROCESSADA

    envelope = envelope_de_evento("ExecucaoFinalizada")
    antes = _resolvidas("ExecucaoFinalizada")
    retries = _retries()

    with EmSegundoPlano(consumidor(lento)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: _resolvidas("ExecucaoFinalizada") > antes, prazo_s=40)

    assert chamadas == [UUID(envelope["id"])]
    assert _processadas(engine) == [UUID(envelope["id"])]
    assert (broker.contar("os.eventos"), broker.contar(_DLQ)) == (0, 0)
    assert _retries() == retries


def _resolvidas(tipo: str) -> float:
    """Mensagens do tipo com ack: processadas ou duplicadas."""
    return _consumidas(tipo, "processada") + _consumidas(tipo, "duplicada")


@pytest.fixture
def retry_sem_permissao(broker: Broker) -> Iterator[Callable[[], None]]:
    """Tira do usuario `os` a escrita em pytstop.retry; devolve quem a restaura."""
    permissoes = json.loads((CONTRATOS / "rabbitmq/permissoes.json").read_text())
    (topico,) = [
        p
        for p in permissoes["topic_permissions"]
        if (p["user"], p["exchange"]) == ("os", "pytstop.retry")
    ]

    def restaurar() -> None:
        broker.rabbitmqctl(
            "set_topic_permissions",
            "-p",
            "/",
            "os",
            "pytstop.retry",
            topico["write"],
            topico["read"],
        )

    broker.rabbitmqctl(
        "set_topic_permissions", "-p", "/", "os", "pytstop.retry", "^\\z", "^\\z"
    )
    try:
        yield restaurar
    finally:
        restaurar()


def test_rota_de_retry_sem_permissao_reconecta_e_a_mensagem_nao_se_perde(
    engine: Engine,
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    retry_sem_permissao: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O broker fecha o canal (403) na copia de retry: a original fica sem ack,
    # volta para a fila e o consumidor reconecta com backoff. Com a permissao
    # de volta, a falha seguinte vai para a retry e a mensagem e processada.
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.2)
    espiao = Espiao(FalhaTransitoriaError("x"), FalhaTransitoriaError("y"))
    envelope = envelope_de_evento("OrcamentoRecusado")
    antes = _reconexoes()

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: _reconexoes() > antes)
        retry_sem_permissao()
        esperar_ate(lambda: _processadas(engine) == [UUID(envelope["id"])])

    assert [m.id for m in espiao.recebidas] == [UUID(envelope["id"])] * 3
    assert broker.contar(_DLQ) == 0


def test_conexao_que_cai_entre_o_commit_e_o_ack_nao_perde_nem_repete_o_efeito(
    engine: Engine,
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O broker derruba a conexao enquanto o handler trabalha: o commit
    # acontece, o ack nao. A mensagem volta e vira duplicada, com ack e sem
    # efeito.
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.2)
    chamadas: list[UUID] = []

    def derruba_a_conexao(m: MensagemRecebida, _t: TransacaoDaMensagem) -> Desfecho:
        chamadas.append(m.id)
        broker.rabbitmqctl("close_all_connections", "teste de queda")
        return Desfecho.PROCESSADA

    envelope = envelope_de_evento("PagamentoEstornado")
    antes = _consumidas("PagamentoEstornado", "duplicada")

    with EmSegundoPlano(consumidor(derruba_a_conexao)):
        broker.publicar_evento(envelope)
        esperar_ate(
            lambda: _consumidas("PagamentoEstornado", "duplicada") == antes + 1,
            prazo_s=30,
        )

    assert chamadas == [UUID(envelope["id"])]
    assert _processadas(engine) == [UUID(envelope["id"])]
    assert (broker.contar("os.eventos"), broker.contar(_DLQ)) == (0, 0)


def test_copia_de_retry_com_o_broker_em_alarme_nao_perde_a_mensagem(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker_avulso: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A copia de retry espera o broker em alarme ate o timeout do bloqueio (2 s
    # aqui), que derruba a conexao. Bloqueado, o broker nem le o fechamento: a
    # original fica sem ack na conexao antiga ate o alarme passar, volta para a
    # fila e e processada na entrega seguinte. Nada se perde nem vai para a DLQ.
    monkeypatch.setattr(amqp, "_BLOQUEIO_MAXIMO_S", 2)
    monkeypatch.setattr(amqp, "RECONEXAO_TETO_S", 0.5)
    chamadas: list[UUID] = []

    def falha_e_poe_o_broker_em_alarme(
        m: MensagemRecebida, _t: TransacaoDaMensagem
    ) -> Desfecho:
        chamadas.append(m.id)
        if len(chamadas) == 1:
            broker_avulso.rabbitmqctl("set_vm_memory_high_watermark", "0.0001")
            raise FalhaTransitoriaError("dependencia fora")
        return Desfecho.PROCESSADA

    envelope = envelope_de_evento("ExecucaoCancelada")
    consumidor = Consumidor(
        session_factory=session_factory,
        parametros=broker_avulso.parametros("os"),
        despachante=dict.fromkeys(
            catalogo().consumidos, falha_e_poe_o_broker_em_alarme
        ),
        tracer=rastreador.tracer,
        config=ConfigConsumidor(inatividade_s=0.1, diretorio_de_saude=tmp_path),
    )
    antes = _reconexoes()

    with EmSegundoPlano(consumidor):
        broker_avulso.publicar_evento(envelope)
        esperar_ate(lambda: _reconexoes() > antes, prazo_s=30)
        assert _processadas(engine) == []
        broker_avulso.rabbitmqctl("set_vm_memory_high_watermark", "0.4")
        esperar_ate(lambda: _processadas(engine) == [UUID(envelope["id"])], prazo_s=60)

    assert chamadas == [UUID(envelope["id"])] * 2
    assert broker_avulso.contar(_DLQ) == 0
