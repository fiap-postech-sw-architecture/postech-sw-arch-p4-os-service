"""Consumidor de os.eventos contra Postgres e RabbitMQ reais (topologia do platform).

As filas de retry do broker de teste tem TTL de 100 ms, entao o ciclo inteiro
(cinco tentativas e a DLQ) cabe num teste.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy import text

from src.compartilhado.aplicacao.mensageria import (
    Comando,
    Desfecho,
    FalhaTransitoriaError,
    MensagemRecebida,
)
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
)
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.integracao.broker import EmSegundoPlano, envelope_de_evento, esperar_ate
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
) -> None:
    espiao = Espiao(*[FalhaTransitoriaError("banco fora")] * 6)
    envelope = envelope_de_evento("OrcamentoGerado")
    antes = (
        _consumidas("OrcamentoGerado", "retry"),
        _consumidas("OrcamentoGerado", "dlq"),
    )
    retries = _retries()

    with EmSegundoPlano(consumidor(espiao)):
        broker.publicar_evento(envelope)
        morta = esperar_ate(lambda: broker.pegar(_DLQ))

    assert len(espiao.recebidas) == 6
    assert {m.id for m in espiao.recebidas} == {UUID(envelope["id"])}
    niveis = ["1s", "5s", "15s", "60s", "300s"]
    assert [(e, r) for e, r, _ in publicacoes] == [
        ("pytstop.retry", f"os.eventos.retry.{nivel}") for nivel in niveis
    ]
    for tentativa, (_, _, copia) in enumerate(publicacoes, start=1):
        assert copia.headers["x-tentativa"] == tentativa
        # O atraso e o TTL da fila do nivel: a copia nao leva expiration.
        assert copia.expiration is None
        assert copia.user_id == "os"
        assert copia.message_id == envelope["id"]
        assert copia.type == "OrcamentoGerado"
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


@pytest.mark.parametrize(
    ("depois", "motivo"),
    [
        pytest.param(lambda t: t.session.commit(), "commit", id="handler-comita"),
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


@pytest.mark.parametrize(
    ("descricao", "montar"),
    [
        pytest.param(
            "evento de billing publicado pela execucao",
            lambda b, e: b.publicar_evento(
                e,
                usuario="execucao",
                routing_key="evento.execucao.orcamento_gerado",
            ),
            id="user-id-divergente",
        ),
        pytest.param(
            "copia de retry do proprio usuario sem x-tentativa",
            lambda b, e: b.publicar_evento(
                e,
                usuario="os",
                exchange="pytstop.retry",
                routing_key="os.eventos.retry.1s",
            ),
            id="copia-de-retry-sem-tentativa",
        ),
        pytest.param(
            "tipo fora do catalogo",
            lambda b, e: b.publicar_evento(
                e,
                usuario="execucao",
                routing_key="evento.execucao.inexistente",
                tipo="Inexistente",
            ),
            id="tipo-desconhecido",
        ),
        pytest.param(
            "versao desconhecida",
            lambda b, e: b.publicar_evento({**e, "versao": 2}),
            id="versao-2",
        ),
        pytest.param(
            "dados sem campo obrigatorio",
            lambda b, e: b.publicar_evento({**e, "dados": {"ordem_id": e["id"]}}),
            id="dados-invalidos",
        ),
        pytest.param(
            "corpo que nao e JSON",
            lambda b, e: b.publicar_evento(e, corpo=b"{nao e json"),
            id="json-invalido",
        ),
        pytest.param(
            "message_id diferente do id do envelope",
            lambda b, e: b.publicar_evento(e, message_id=str(uuid4())),
            id="propriedades-divergentes",
        ),
        pytest.param(
            "x-tentativa fora de 0 a 5",
            lambda b, e: b.publicar_evento(e, cabecalhos={"x-tentativa": 6}),
            id="tentativa-invalida",
        ),
    ],
)
def test_mensagem_fora_do_contrato_ou_da_origem_vai_direto_para_a_dlq(
    engine: Engine,
    broker: Broker,
    consumidor: Callable[..., Consumidor],
    descricao: str,
    montar: Callable[[Broker, dict[str, Any]], None],
) -> None:
    espiao = Espiao()
    # OrcamentoGerado e do billing: os casos acima mexem em um aspecto so.
    envelope = envelope_de_evento("OrcamentoGerado")
    retries = _retries()

    with EmSegundoPlano(consumidor(espiao)):
        montar(broker, envelope)
        propriedades, _ = esperar_ate(lambda: broker.pegar(_DLQ))

    assert espiao.recebidas == [], descricao
    assert _processadas(engine) == []
    # Direto: o consumidor nao republicou (com 100 ms por nivel, um desvio pelas
    # cinco filas de retry tambem chegaria a DLQ dentro do prazo do teste).
    assert _retries() == retries
    rejeicoes = [
        morte["count"]
        for morte in propriedades.headers["x-death"]
        if (morte["queue"], morte["reason"]) == ("os.eventos", "rejected")
    ]
    assert rejeicoes == [1]
    assert propriedades.headers.get("x-tentativa") in {None, 6}


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
