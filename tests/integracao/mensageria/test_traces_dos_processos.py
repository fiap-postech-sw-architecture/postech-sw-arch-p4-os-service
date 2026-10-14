"""Trace do relay, do consumidor e do prazos contra o PostgreSQL e o RabbitMQ.

O banco entra no trace da mensagem ou da saga (cada comando SQL dela vira span
filho), e o laco ocioso, que so faz consulta sem span em volta, nao abre trace
algum: o amostrador descarta o span de cliente sem pai. O provider e o de
producao (``Rastreador``) e o engine, o da sessao de testes instrumentado por ele
(``engine_rastreado``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from opentelemetry.trace import SpanKind
from sqlalchemy import event

from src.compartilhado.aplicacao.mensageria import Desfecho
from src.compartilhado.infraestrutura.database import BANCO_FORA
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
)
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.vencer_prazos import (
    ResultadoDoCiclo,
    VencerPrazos,
)
from src.ordem_servico.infraestrutura.rastreamento_da_saga import span_da_saga
from src.ordem_servico.infraestrutura.transacao_dos_prazos import (
    TransacaoDosPrazosSQLAlchemy,
)
from tests.eventos import envelope_de_evento
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.ordem_servico.test_prazos_no_banco import (
    MAXIMO,
    PRAZO,
    _entregue,
    _semear_e_comitar,
)
from tests.unitarios.fakes import PresencaFake

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

_FILA_DA_EXECUCAO = "execucao.comandos"


@pytest.fixture
def comandos_sql(engine_rastreado: Engine) -> Iterator[list[str]]:
    """Os comandos SQL que o engine executou: prova que o laco usou o banco."""
    executados: list[str] = []

    def guardar(_c: object, _k: object, comando: str, *_: object) -> None:
        executados.append(comando)

    event.listen(engine_rastreado, "before_cursor_execute", guardar)
    yield executados
    event.remove(engine_rastreado, "before_cursor_execute", guardar)


def _publicar_no_span_da_api(
    session_factory: sessionmaker[Session], rastreador: Rastreador
) -> UUID:
    """Grava um comando na outbox dentro de um span, que a linha guarda."""
    exemplo = json.loads((CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text())
    dados = {**exemplo["dados"], "ordem_id": uuid4()}
    with (
        rastreador.tracer.start_as_current_span(
            "POST /api/v1/ordens-de-servico", kind=SpanKind.SERVER
        ),
        SQLAlchemyUnitOfWork(session_factory) as uow,
    ):
        mensagem_id = uow.publicar_comando(
            "SolicitarDiagnostico", dados, correlation_id=dados["ordem_id"]
        )
        uow.commit()
    return mensagem_id


def _consumidor(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> Consumidor:
    return Consumidor(
        session_factory=session_factory,
        parametros=broker.parametros("os"),
        despachante=dict.fromkeys(
            catalogo().consumidos, lambda *_: Desfecho.PROCESSADA
        ),
        tracer=rastreador.tracer,
        config=ConfigConsumidor(inatividade_s=0.1, diretorio_de_saude=tmp_path),
    )


def test_relay_ocioso_nao_exporta_span(
    engine_rastreado: Engine,
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    comandos_sql: list[str],
) -> None:
    relay = Relay(
        engine=engine_rastreado,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.05, diretorio_de_saude=tmp_path),
    )

    with EmSegundoPlano(relay):
        # Dezenas de consultas do poll e da limpeza horaria, todas sem span em volta.
        esperar_ate(lambda: len(comandos_sql) >= 10)

    assert rastreador.spans() == []


def test_relay_publicando_leva_o_banco_para_o_trace_da_mensagem(
    engine_rastreado: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    mensagem_id = _publicar_no_span_da_api(session_factory, rastreador)
    relay = Relay(
        engine=engine_rastreado,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.05, diretorio_de_saude=tmp_path),
    )

    with EmSegundoPlano(relay):
        esperar_ate(lambda: broker.pegar(_FILA_DA_EXECUCAO))

    (api,) = rastreador.spans("POST /api/v1/ordens-de-servico")
    (producer,) = rastreador.spans("publish SolicitarDiagnostico")
    consultas = [s for s in rastreador.spans() if s.kind is SpanKind.CLIENT]
    assert producer.parent is not None
    assert producer.parent.span_id == api.get_span_context().span_id
    # A marcacao da linha como entregue e o INSERT da API entram no trace; a
    # reivindicacao e o resto do poll, sem span em volta, nao.
    assert all(c.parent is not None for c in consultas)
    do_relay = [
        c for c in consultas if c.parent.span_id == producer.get_span_context().span_id
    ]  # type: ignore[union-attr]  # pai conferido acima
    assert any(
        "UPDATE outbox" in str(c.attributes.get("db.statement")) for c in do_relay
    )
    assert {c.get_span_context().trace_id for c in consultas} == {
        producer.get_span_context().trace_id
    }
    assert mensagem_id  # a linha publicada e a que o teste gravou


def test_consumidor_ocioso_nao_exporta_span(
    engine_rastreado: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    comandos_sql: list[str],
) -> None:
    consumidor = _consumidor(session_factory, broker, rastreador, tmp_path)

    with EmSegundoPlano(consumidor):
        # A limpeza horaria roda na primeira volta; depois o laco so espera.
        esperar_ate(lambda: any("mensagens_processadas" in c for c in comandos_sql))

    assert rastreador.spans() == []


def test_consumidor_leva_o_banco_para_o_trace_da_mensagem(
    engine_rastreado: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    comandos_sql: list[str],
) -> None:
    consumidor = _consumidor(session_factory, broker, rastreador, tmp_path)
    envelope = envelope_de_evento("DiagnosticoConcluido")

    with EmSegundoPlano(consumidor):
        broker.publicar_evento(envelope)
        esperar_ate(lambda: rastreador.spans("process DiagnosticoConcluido"))

    (consumo,) = rastreador.spans("process DiagnosticoConcluido")
    consultas = [s for s in rastreador.spans() if s.kind is SpanKind.CLIENT]
    assert consumo.kind is SpanKind.CONSUMER
    assert any(
        "INSERT INTO mensagens_processadas" in str(c.attributes.get("db.statement"))
        for c in consultas
    )
    assert all(c.parent is not None for c in consultas)
    assert {c.get_span_context().trace_id for c in consultas} == {
        consumo.get_span_context().trace_id
    }


class TestPrazos:
    def _ciclo(
        self,
        session_factory: sessionmaker[Session],
        rastreador: Rastreador,
        presenca: PresencaFake,
    ) -> VencerPrazos:
        return VencerPrazos(
            abrir=lambda: TransacaoDosPrazosSQLAlchemy(session_factory),
            presenca=presenca,
            contexto=ContextoDeEnvio(
                PRAZO,
                lambda: datetime.now(UTC),
                span=span_da_saga(rastreador.tracer),
            ),
            max_reenvios=MAXIMO,
            banco_fora=BANCO_FORA,
        )

    def test_ciclo_ocioso_nao_exporta_span(
        self,
        engine_rastreado: Engine,
        session_factory: sessionmaker[Session],
        rastreador: Rastreador,
        comandos_sql: list[str],
    ) -> None:
        ciclo = self._ciclo(session_factory, rastreador, PresencaFake())

        resultados = [ciclo.executar() for _ in range(3)]

        assert resultados == [ResultadoDoCiclo()] * 3
        # O relogio do banco e a busca de saga vencida, tres vezes.
        assert len(comandos_sql) >= 6
        assert rastreador.spans() == []

    def test_ciclo_pausado_com_saga_vencida_nao_exporta_span(
        self,
        engine_rastreado: Engine,
        session_factory: sessionmaker[Session],
        rastreador: Rastreador,
    ) -> None:
        # A saga vencida e reivindicada, mas sem consumidor o ciclo desfaz e pausa:
        # nao tratou nada, nao abre span.
        agora = datetime.now(UTC)
        _semear_e_comitar(
            session_factory,
            [_entregue(agora - timedelta(seconds=200))],
            prazo_em=agora - timedelta(seconds=80),
            enviado_em=agora - timedelta(seconds=200),
        )
        ciclo = self._ciclo(
            session_factory, rastreador, PresencaFake(ha_consumidor=False)
        )

        assert ciclo.executar() == ResultadoDoCiclo(pausado=True)

        assert rastreador.spans() == []

    def test_saga_reenviada_tem_o_banco_no_span_do_prazo(
        self,
        engine_rastreado: Engine,
        session_factory: sessionmaker[Session],
        rastreador: Rastreador,
    ) -> None:
        agora = datetime.now(UTC)
        _semear_e_comitar(
            session_factory,
            [_entregue(agora - timedelta(seconds=200))],
            prazo_em=agora - timedelta(seconds=80),
            enviado_em=agora - timedelta(seconds=200),
        )
        ciclo = self._ciclo(session_factory, rastreador, PresencaFake())

        assert ciclo.executar() == ResultadoDoCiclo(reenviadas=1)

        (prazo,) = rastreador.spans("saga prazo")
        consultas = [s for s in rastreador.spans() if s.kind is SpanKind.CLIENT]
        assert prazo.kind is SpanKind.INTERNAL
        assert any(
            "UPDATE sagas" in str(c.attributes.get("db.statement")) for c in consultas
        )
        assert all(c.parent is not None for c in consultas)
        assert {c.parent.span_id for c in consultas if c.parent} == {  # type: ignore[union-attr]  # filtrado
            prazo.get_span_context().span_id
        }
