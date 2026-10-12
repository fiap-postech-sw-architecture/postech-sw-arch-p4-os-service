"""Duas replicas ao mesmo tempo, com Postgres e RabbitMQ reais.

Relay: o claim com ``SKIP LOCKED`` e o lease repartem as linhas sem repetir
nenhuma nem furar a ordem de cada OS, uma replica no meio do claim nao trava o
claim da outra, a linha reivindicada por quem caiu so volta depois do lease e a
replica cujo lease venceu nao grava nada na linha que outra reivindicou (o fim
do lease e o token). Consumidor: a mesma mensagem em dois consumidores ao mesmo
tempo tem efeito uma vez, decidido pela restricao unica de
``mensagens_processadas``, sem passar pela fila de retry.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, text

from src.compartilhado.aplicacao.mensageria import (
    Comando,
    Desfecho,
    MensagemRecebida,
)
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
)
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo
from src.compartilhado.infraestrutura.mensageria.outbox import Outbox
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from tests.eventos import envelope_de_evento
from tests.integracao.broker import EmSegundoPlano, esperar_ate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.compartilhado.infraestrutura.mensageria.outbox import LinhaDaOutbox
    from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem
    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

_FILA = "execucao.comandos"


def _gravar(session_factory: sessionmaker[Session], ordem_id: UUID) -> UUID:
    exemplo = json.loads((CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text())
    with SQLAlchemyUnitOfWork(session_factory) as uow:
        mensagem_id = uow.publicar_comando(
            Comando.SOLICITAR_DIAGNOSTICO,
            {**exemplo["dados"], "ordem_id": ordem_id},
            correlation_id=ordem_id,
        )
        uow.commit()
    return mensagem_id


def _relay(
    engine: Engine, broker: Broker, rastreador: Rastreador, saude: Path, **config: Any
) -> Relay:
    return Relay(
        engine=engine,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.1, diretorio_de_saude=saude, **config),
    )


def _pendentes(engine: Engine) -> int:
    with engine.connect() as conexao:
        total: int = conexao.execute(
            text("SELECT count(*) FROM outbox WHERE status <> 'entregue'")
        ).scalar_one()
    return total


def test_dois_relays_publicam_cada_linha_uma_vez_e_na_ordem_de_cada_os(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    ordens = [uuid4() for _ in range(10)]
    esperado: dict[str, list[str]] = {str(o): [] for o in ordens}
    for _ in range(10):
        for ordem in ordens:
            esperado[str(ordem)].append(str(_gravar(session_factory, ordem)))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    with (
        EmSegundoPlano(_relay(engine, broker, rastreador, tmp_path / "a", lote=3)),
        EmSegundoPlano(_relay(engine, broker, rastreador, tmp_path / "b", lote=3)),
    ):
        esperar_ate(lambda: _pendentes(engine) == 0, prazo_s=60)

    publicadas = broker.pegar_todas(_FILA)
    ids = [propriedades.message_id for propriedades, _ in publicadas]
    assert len(ids) == len(set(ids)) == 100
    por_os: dict[str, list[str]] = {str(o): [] for o in ordens}
    for propriedades, _ in publicadas:
        por_os[propriedades.correlation_id].append(propriedades.message_id)
    assert por_os == esperado


def test_linha_reivindicada_por_relay_que_caiu_so_volta_depois_do_lease(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    mensagem_id = _gravar(session_factory, uuid4())
    outbox = Outbox(engine)

    # Uma replica reivindica e cai antes de publicar.
    (reivindicada,) = outbox.reivindicar(10, timedelta(seconds=1))
    assert reivindicada.mensagem_id == mensagem_id
    assert outbox.reivindicar(10, timedelta(seconds=30)) == []

    (de_novo,) = esperar_ate(
        lambda: outbox.reivindicar(10, timedelta(seconds=30)), prazo_s=10
    )
    assert de_novo.mensagem_id == mensagem_id
    assert de_novo.lease_ate - reivindicada.lease_ate >= timedelta(seconds=29)


def _estado(engine: Engine, linha_id: int) -> tuple[Any, ...]:
    with engine.connect() as conexao:
        return tuple(
            conexao.execute(
                text(
                    "SELECT status, tentativas, proxima_tentativa_em, ultimo_erro "
                    "FROM outbox WHERE id = :id"
                ),
                {"id": linha_id},
            ).one()
        )


@pytest.mark.parametrize(
    ("marcar", "recusa"),
    [
        pytest.param(
            lambda outbox, linha: outbox.renovar(linha, timedelta(seconds=60)),
            None,
            id="renovar",
        ),
        pytest.param(
            lambda outbox, linha: outbox.marcar_entregue(linha),
            False,
            id="marcar-entregue",
        ),
        pytest.param(
            lambda outbox, linha: outbox.registrar_falha(linha, "recusada (nack)"),
            "perdida",
            id="registrar-falha",
        ),
        pytest.param(
            lambda outbox, linha: outbox.marcar_dead(linha, "fora do contrato"),
            False,
            id="marcar-dead",
        ),
        pytest.param(lambda outbox, linha: outbox.liberar([linha]), None, id="liberar"),
    ],
)
def test_replica_com_o_lease_vencido_nao_grava_nada_na_linha_que_outra_reivindicou(
    engine: Engine,
    session_factory: sessionmaker[Session],
    marcar: Callable[[Outbox, LinhaDaOutbox], object],
    recusa: object,
) -> None:
    # O lease de A vence; B reivindica e renova a linha, que segue `pendente`
    # enquanto B publica. O status e o mesmo para as duas replicas: so o token
    # (o fim do lease que cada uma gravou) barra A.
    _gravar(session_factory, uuid4())
    outbox = Outbox(engine)
    (atrasada,) = outbox.reivindicar(10, timedelta(seconds=0.2))
    (reivindicada,) = esperar_ate(
        lambda: outbox.reivindicar(10, timedelta(seconds=60)), prazo_s=10
    )
    vigente = outbox.renovar(reivindicada, timedelta(seconds=60))
    assert vigente is not None

    assert marcar(outbox, atrasada) == recusa
    assert _estado(engine, vigente.id) == ("pendente", 0, vigente.lease_ate, None)
    assert outbox.marcar_entregue(vigente)


@contextmanager
def _primeiro_claim_segura_as_linhas(
    engine: Engine,
) -> Iterator[tuple[threading.Event, threading.Event]]:
    """Deixa aberta a transacao do primeiro claim que reivindicar alguma linha.

    Claim e lease saem numa transacao so: o gancho para antes do UPDATE do
    lease, com as linhas ja travadas pelo ``SELECT ... FOR UPDATE``, ate o teste
    sinalizar ``liberar``.
    """
    segurando, liberar = threading.Event(), threading.Event()

    def segurar(_conexao: object, _cursor: object, sql: str, *_: object) -> None:
        if sql.startswith("UPDATE outbox SET proxima_tentativa_em = now() +") and (
            not segurando.is_set()
        ):
            segurando.set()
            liberar.wait(30)

    event.listen(engine, "before_cursor_execute", segurar)
    try:
        yield segurando, liberar
    finally:
        liberar.set()
        event.remove(engine, "before_cursor_execute", segurar)


def _status(engine: Engine, mensagem_id: UUID) -> str:
    with engine.connect() as conexao:
        status: str = conexao.execute(
            text("SELECT status FROM outbox WHERE mensagem_id = :id"),
            {"id": mensagem_id},
        ).scalar_one()
    return status


def test_relay_no_meio_do_claim_nao_trava_o_claim_da_outra_replica(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    # A reivindica a linha da primeira OS e fica com a transacao do claim
    # aberta; B pula a linha travada (SKIP LOCKED) e publica a da outra OS na
    # hora. Sem o SKIP LOCKED, B esperaria o lock de A.
    primeira = _gravar(session_factory, uuid4())
    segunda = _gravar(session_factory, uuid4())
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    with (
        _primeiro_claim_segura_as_linhas(engine) as (segurando, liberar),
        EmSegundoPlano(_relay(engine, broker, rastreador, tmp_path / "a", lote=1)),
    ):
        esperar_ate(segurando.is_set)
        with EmSegundoPlano(_relay(engine, broker, rastreador, tmp_path / "b")):
            try:
                esperar_ate(lambda: _status(engine, segunda) == "entregue", prazo_s=10)
                travada = _status(engine, primeira)
            finally:
                liberar.set()
            esperar_ate(lambda: _status(engine, primeira) == "entregue")

    assert travada == "pendente"
    publicadas = [p.message_id for p, _ in broker.pegar_todas(_FILA)]
    assert publicadas == [str(segunda), str(primeira)]


def _consumidas(tipo: str, resultado: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_consumidas_total", {"tipo": tipo, "resultado": resultado}
    )
    return valor or 0.0


def _alguem_espera_lock(engine: Engine) -> bool:
    with engine.connect() as conexao:
        total: int = conexao.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
        ).scalar_one()
    return total > 0


def test_mesma_mensagem_em_dois_consumidores_ao_mesmo_tempo_tem_efeito_uma_vez(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    # O primeiro segura a transacao ate o segundo estar esperando o lock da
    # chave em mensagens_processadas: a restricao unica decide, e o segundo vira
    # `duplicada` (nada de retry, nada de efeito repetido).
    chamadas: list[UUID] = []
    trava = threading.Lock()

    def handler(mensagem: MensagemRecebida, _t: TransacaoDaMensagem) -> Desfecho:
        with trava:
            chamadas.append(mensagem.id)
        esperar_ate(lambda: _alguem_espera_lock(engine), prazo_s=15)
        return Desfecho.PROCESSADA

    envelope = envelope_de_evento("OrcamentoAprovado")
    antes = {r: _consumidas("OrcamentoAprovado", r) for r in ("duplicada", "retry")}
    consumidores = []
    for nome in ("a", "b"):
        (tmp_path / nome).mkdir()
        consumidores.append(
            Consumidor(
                session_factory=session_factory,
                parametros=broker.parametros("os"),
                despachante=dict.fromkeys(catalogo().consumidos, handler),
                tracer=rastreador.tracer,
                config=ConfigConsumidor(
                    inatividade_s=0.1, diretorio_de_saude=tmp_path / nome
                ),
            )
        )

    with EmSegundoPlano(consumidores[0]), EmSegundoPlano(consumidores[1]):
        esperar_ate(lambda: (tmp_path / "a/consumidor-pronto").exists())
        esperar_ate(lambda: (tmp_path / "b/consumidor-pronto").exists())
        broker.publicar_evento(envelope)
        broker.publicar_evento(envelope)
        esperar_ate(
            lambda: (
                _consumidas("OrcamentoAprovado", "duplicada") == antes["duplicada"] + 1
            ),
            prazo_s=30,
        )

    assert chamadas == [UUID(envelope["id"])]
    assert _consumidas("OrcamentoAprovado", "retry") == antes["retry"]
    with engine.connect() as conexao:
        registradas = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas")
        ).scalar_one()
    assert registradas == 1
