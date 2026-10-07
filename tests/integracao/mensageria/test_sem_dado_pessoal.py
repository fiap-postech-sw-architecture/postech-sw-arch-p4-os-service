"""Placa, texto livre e senha nao saem pelo log, pelos spans nem pela outbox (LGPD).

O texto das mensagens e o das excecoes dos handlers ficam fora do log do
servico, do log do pika (que em WARNING imprime o corpo da mensagem que o
broker devolve), dos atributos e do status dos spans, dos rotulos das metricas
e do ``ultimo_erro`` da outbox; o das excecoes de fora do handler (falha
inesperada, queda da conexao), fora do log do servico e dos spans. Tudo e
conferido sobre o que cada caminho produz de verdade: broker e banco reais,
logs e spans capturados.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

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
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from tests.eventos import envelope_de_evento
from tests.integracao.broker import EmSegundoPlano, esperar_ate

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem
    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador, Saidas

_PLACA = "QZX7W42"
_TEXTO_LIVRE = f"texto livre com a placa {_PLACA}"
_DLQ = "os.eventos.dlq"
_QUEDA = "broker connection lost; reconnecting"


def _gravar_com_placa(session_factory: sessionmaker[Session]) -> UUID:
    exemplo = json.loads((CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text())
    dados = exemplo["dados"]
    dados["veiculo"]["placa"] = _PLACA
    dados["descricao_problema"] = _TEXTO_LIVRE
    with SQLAlchemyUnitOfWork(session_factory) as uow:
        mensagem_id = uow.publicar_comando(
            Comando.SOLICITAR_DIAGNOSTICO,
            dados,
            correlation_id=UUID(dados["ordem_id"]),
        )
        uow.commit()
    return mensagem_id


@pytest.fixture
def execucao_sem_rota(broker: Broker) -> Iterator[None]:
    with broker.canal() as canal:
        canal.queue_unbind(
            "execucao.comandos", "pytstop.comandos", "comando.execucao.#"
        )
    try:
        yield
    finally:
        with broker.canal() as canal:
            canal.queue_bind(
                "execucao.comandos", "pytstop.comandos", "comando.execucao.#"
            )


@pytest.mark.usefixtures("execucao_sem_rota")
def test_relay_com_mensagem_devolvida_nao_poe_placa_nem_texto_livre_em_lugar_nenhum(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    saidas: Saidas,
) -> None:
    mensagem_id = _gravar_com_placa(session_factory)
    relay = Relay(
        engine=engine,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(
            poll_s=0.1, atrasos_s=(0.1,) * 4, diretorio_de_saude=tmp_path
        ),
    )

    with EmSegundoPlano(relay):
        esperar_ate(lambda: _ultimo_erro(engine, mensagem_id)[0] == "dead")

    assert _ultimo_erro(engine, mensagem_id) == (
        "dead",
        "devolvida pelo broker: nenhuma fila para a routing key",
    )
    eventos = [evento["event"] for evento in saidas.eventos]
    assert "message publish failed; outbox row dead" in eventos
    assert _PLACA not in saidas.texto(rastreador)
    assert "Published message was returned" not in saidas.texto(rastreador)


def _ultimo_erro(engine: Engine, mensagem_id: UUID) -> tuple[str, str | None]:
    with engine.connect() as conexao:
        linha = conexao.execute(
            text("SELECT status, ultimo_erro FROM outbox WHERE mensagem_id = :id"),
            {"id": mensagem_id},
        ).one()
    return linha.status, linha.ultimo_erro


def _consumidor(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    handler: Any,
) -> Consumidor:
    return Consumidor(
        session_factory=session_factory,
        parametros=broker.parametros("os"),
        despachante=dict.fromkeys(catalogo().consumidos, handler),
        tracer=rastreador.tracer,
        config=ConfigConsumidor(inatividade_s=0.1, diretorio_de_saude=tmp_path),
    )


def _diagnostico_com_texto_livre() -> dict[str, Any]:
    """``DiagnosticoConcluido`` com o texto livre no comeco do corpo.

    O pika imprime so os 255 primeiros bytes da mensagem devolvida; a ordem
    das chaves no JSON e livre, e o texto vem primeiro para cair nesse trecho.
    """
    envelope = envelope_de_evento("DiagnosticoConcluido")
    dados = envelope.pop("dados")
    dados.pop("observacoes")
    return {"dados": {"observacoes": _TEXTO_LIVRE, **dados}, **envelope}


def test_consumidor_com_falhas_do_handler_e_mensagens_recusadas_nao_vaza_o_texto(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    saidas: Saidas,
) -> None:
    transitoria = _diagnostico_com_texto_livre()
    com_bug = _diagnostico_com_texto_livre()
    fora_do_contrato = _diagnostico_com_texto_livre()
    fora_do_contrato["dados"]["itens"] = [{"tipo": _TEXTO_LIVRE}]
    falhas: dict[str, list[BaseException]] = {
        transitoria["id"]: [FalhaTransitoriaError(_TEXTO_LIVRE)],
        com_bug["id"]: [KeyError(_TEXTO_LIVRE)],
    }

    def handler(mensagem: MensagemRecebida, _t: TransacaoDaMensagem) -> Desfecho:
        pendentes = falhas.get(str(mensagem.id), [])
        if pendentes:
            raise pendentes.pop()
        return Desfecho.PROCESSADA

    antes = _consumidas("TipoForaDoCatalogo", "dlq")
    with EmSegundoPlano(
        _consumidor(session_factory, broker, rastreador, tmp_path, handler)
    ):
        for envelope in (transitoria, com_bug, fora_do_contrato):
            broker.publicar_evento(envelope)
        broker.publicar_evento(
            _diagnostico_com_texto_livre(),
            tipo="TipoForaDoCatalogo",
            routing_key="evento.execucao.tipo_fora_do_catalogo",
        )
        esperar_ate(lambda: broker.contar(_DLQ) == 3)
        esperar_ate(lambda: not falhas[transitoria["id"]])

    eventos = [evento["event"] for evento in saidas.eventos]
    assert "message processing failed; retry scheduled" in eventos
    assert "message rejected to dlq" in eventos
    assert _PLACA not in saidas.texto(rastreador)
    # Tipo fora do catalogo vira o rotulo `desconhecido` (cardinalidade fixa).
    assert _consumidas("TipoForaDoCatalogo", "dlq") == antes == 0.0


def _consumidas(tipo: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total", {"tipo": tipo, "resultado": resultado}
    )
    return valor or 0.0


@pytest.fixture
def retry_sem_rota(broker: Broker) -> Iterator[None]:
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


@pytest.mark.usefixtures("retry_sem_rota")
def test_copia_de_retry_devolvida_nao_poe_o_corpo_no_log_do_pika(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    saidas: Saidas,
) -> None:
    def falhar(_m: MensagemRecebida, _t: TransacaoDaMensagem) -> Desfecho:
        raise FalhaTransitoriaError(_TEXTO_LIVRE)

    with EmSegundoPlano(
        _consumidor(session_factory, broker, rastreador, tmp_path, falhar)
    ):
        broker.publicar_evento(_diagnostico_com_texto_livre())
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    eventos = [evento["event"] for evento in saidas.eventos]
    assert "retry copy refused by the broker; rejected to dlq" in eventos
    assert _PLACA not in saidas.texto(rastreador)


def test_conexao_derrubada_pelo_broker_loga_so_o_tipo_do_erro(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    saidas: Saidas,
) -> None:
    # O texto da excecao de queda vem de fora do servico: o motivo que o broker
    # manda ao fechar a conexao (que o pika loga em ERROR) ou o erro do decoder
    # do pika. O log do servico leva so o tipo.
    consumidor = _consumidor(
        session_factory, broker, rastreador, tmp_path, lambda *_: Desfecho.PROCESSADA
    )

    with EmSegundoPlano(consumidor):
        esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
        broker.rabbitmqctl("close_all_connections", _TEXTO_LIVRE)
        esperar_ate(
            lambda: any(e["event"] == _QUEDA for e in saidas.eventos), prazo_s=30
        )

    queda = next(e for e in saidas.eventos if e["event"] == _QUEDA)
    assert (queda["erro"], queda["codigo"]) == ("ConnectionClosedByBroker", 320)
    assert _PLACA not in repr(saidas.eventos)


def test_sexta_falha_transitoria_com_texto_livre_loga_so_o_tipo_do_erro(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    saidas: Saidas,
) -> None:
    def falhar(_m: MensagemRecebida, _t: TransacaoDaMensagem) -> Desfecho:
        raise FalhaTransitoriaError(_TEXTO_LIVRE)

    with EmSegundoPlano(
        _consumidor(session_factory, broker, rastreador, tmp_path, falhar)
    ):
        broker.publicar_evento(_diagnostico_com_texto_livre())
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    esgotadas = [
        (e["tentativas"], e["erro"])
        for e in saidas.eventos
        if e["event"] == "message retries exhausted; rejected to dlq"
    ]
    assert esgotadas == [(5, "FalhaTransitoriaError")]
    assert _PLACA not in saidas.texto(rastreador)


def test_falha_inesperada_fora_do_handler_nao_poe_o_texto_no_log_nem_no_span(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
    saidas: Saidas,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fora do handler a excecao nao e embrulhada em `erro_no_handler`: e o
    # ramo da falha inesperada que a leva para a DLQ.
    registrar = modulo_consumidor.registrar_processada
    falhas = [RuntimeError(_TEXTO_LIVRE)]

    def registrar_com_falha(sessao: Session, mensagem_id: UUID) -> bool:
        if falhas:
            raise falhas.pop()
        return registrar(sessao, mensagem_id)

    monkeypatch.setattr(modulo_consumidor, "registrar_processada", registrar_com_falha)

    with EmSegundoPlano(
        _consumidor(
            session_factory,
            broker,
            rastreador,
            tmp_path,
            lambda *_: Desfecho.PROCESSADA,
        )
    ):
        broker.publicar_evento(_diagnostico_com_texto_livre())
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    (span,) = rastreador.spans("process DiagnosticoConcluido")
    assert span.status.description == "RuntimeError"
    eventos = [evento["event"] for evento in saidas.eventos]
    assert "message processing crashed; rejected to dlq" in eventos
    assert _PLACA not in saidas.texto(rastreador)


def test_broker_inalcancavel_loga_so_o_tipo_do_erro_sem_a_senha(
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    tmp_path: Path,
    saidas: Saidas,
) -> None:
    senha = f"senha-{_PLACA}"
    consumidor = Consumidor(
        session_factory=session_factory,
        parametros=amqp.parametros(f"amqp://os:{senha}@127.0.0.1:1/%2F", "teste"),
        despachante={},
        tracer=rastreador.tracer,
        config=ConfigConsumidor(inatividade_s=0.1, diretorio_de_saude=tmp_path),
    )

    with EmSegundoPlano(consumidor):
        esperar_ate(
            lambda: any(e["event"] == "broker unavailable" for e in saidas.eventos)
        )

    falha = next(e for e in saidas.eventos if e["event"] == "broker unavailable")
    assert falha["erro"] == "AMQPConnectionError"
    assert senha not in saidas.texto(rastreador)
