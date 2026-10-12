"""Fixtures de integracao: Postgres 16 efemero (testcontainers) com o schema
criado pela migracao Alembic real (nao ``create_all``): toda a suite valida a
migracao, e ``test_migracao.py`` confere que ela bate com o metadata.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog
from structlog.testing import capture_logs

from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria import amqp, consumidor, relay
from src.ordem_servico.aplicacao import use_cases as use_cases_os
from src.ordem_servico.aplicacao.saga import orquestrador
from src.ordem_servico.infraestrutura import metricas_da_saga
from tests.integracao.broker import Broker, subir_broker
from tests.integracao.seed_helpers import criar_usuario
from tests.rastreamento import RegistrosDoLog, Saidas

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from alembic.config import Config
    from fastapi.testclient import TestClient
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.autenticacao.dominio.usuario import Usuario

RAIZ = Path(__file__).resolve().parents[2]

# Colima (macOS): o socket nao fica em /var/run/docker.sock no host; o Ryuk
# monta o socket de dentro da VM, onde ele existe. Assim `uv run pytest` funciona
# direto, sem exportar variaveis. No CI e no Docker Desktop nada muda.
_SOCKET_COLIMA = Path.home() / ".colima/default/docker.sock"
if "DOCKER_HOST" not in os.environ and _SOCKET_COLIMA.exists():
    os.environ["DOCKER_HOST"] = f"unix://{_SOCKET_COLIMA}"
    os.environ.setdefault(
        "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock"
    )


def config_alembic(database_url: str) -> Config:
    """Config do Alembic apontando para ``database_url`` (sem mexer no logging)."""
    from alembic.config import Config

    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    # O fileConfig do env.py reconfiguraria o logging global do processo.
    cfg.attributes["configure_logger"] = False
    return cfg


def alembic(database_url: str, revisao: str = "head", *, descer: bool = False) -> None:
    """``alembic upgrade``/``downgrade`` programatico contra ``database_url``.

    O env.py da precedencia a DATABASE_URL do ambiente sobre o alembic.ini,
    entao a variavel e fixada durante o comando: um DATABASE_URL de dev no
    shell nunca vira alvo de um downgrade de teste.
    """
    from alembic import command

    anterior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        cfg = config_alembic(database_url)
        if descer:
            command.downgrade(cfg, revisao)
        else:
            command.upgrade(cfg, revisao)
    finally:
        if anterior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = anterior


@pytest.fixture(scope="session")
def engine() -> Generator[Engine]:
    from testcontainers.community.postgres import PostgresContainer

    from src.compartilhado.infraestrutura.bootstrap import iniciar_todos_mapeamentos
    from src.compartilhado.infraestrutura.database import criar_engine

    iniciar_todos_mapeamentos()
    with PostgresContainer("postgres:16") as postgres:
        url = postgres.get_connection_url()
        alembic(url)
        eng = criar_engine(url)
        yield eng
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Generator[Session]:
    """Session isolada: transacao externa revertida no teardown.

    ``join_transaction_mode="create_savepoint"`` faz o ``commit()`` do codigo
    sob teste liberar um SAVEPOINT em vez de commitar a transacao externa.
    ``expire_on_commit=False`` como na factory da aplicacao (``database.py``).
    """
    from sqlalchemy.orm import Session as SASession

    connection = engine.connect()
    transaction = connection.begin()
    sess = SASession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )

    yield sess

    sess.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


_EMAIL_ADMIN = "admin-integ@test.com"


@pytest.fixture
def session_factory(engine: Engine) -> Generator[sessionmaker[Session]]:
    """Factory com commit real; o teardown trunca todas as tabelas."""
    from sqlalchemy import text

    from src.compartilhado.infraestrutura.database import (
        criar_session_factory,
        metadata,
    )

    factory = criar_session_factory(engine)
    yield factory

    # Testes de API e de concorrencia commitam de verdade: o rollback da
    # fixture `session` nao os alcanca. Uma sessao esquecida ociosa em
    # transacao seguraria o TRUNCATE para sempre: com o lock_timeout ele
    # falha em segundos e aponta o teste.
    tabelas = ", ".join(t.name for t in reversed(metadata.sorted_tables))
    with factory() as sess:
        sess.execute(text("SET LOCAL lock_timeout = '5s'"))
        sess.execute(text(f"TRUNCATE TABLE {tabelas} CASCADE"))
        sess.commit()


@pytest.fixture
def admin_user(session_factory: sessionmaker[Session]) -> Usuario:
    """Semeia (com commit real) um usuario admin para autenticacao via API."""
    from src.autenticacao.dominio.papel import Papel

    return criar_usuario(session_factory, email=_EMAIL_ADMIN, papel=Papel.ADMIN)


@pytest.fixture(scope="module")
def api_client(engine: Engine) -> Generator[TestClient]:
    """TestClient contra o app REAL (lifespan real) apontando para o banco de teste.

    Escopo de modulo: o app e criado uma vez por arquivo; a limpeza segue por
    teste via teardown de ``session_factory``.
    """
    from fastapi.testclient import TestClient

    from src.main import criar_app

    with ambiente_da_app(engine), TestClient(criar_app()) as client:
        yield client


@pytest.fixture(scope="module")
def url_base_da_app(engine: Engine) -> Generator[str]:
    """A app real (lifespan, banco efemero) servida por HTTP de verdade.

    Para quem busca por urllib, como o ``PyJWKClient`` do validador independente.
    """
    from src.main import criar_app
    from tests.servidor_http import servir

    with ambiente_da_app(engine), servir(criar_app(), lifespan="on") as url:
        yield url


@contextmanager
def ambiente_da_app(engine: Engine) -> Iterator[None]:
    """Chave RSA da sessao de testes e banco efemero no ambiente da app."""
    from tests.chaves_jwt import CHAVE_PEM

    mp = pytest.MonkeyPatch()
    mp.setenv("JWT_PRIVATE_KEY", CHAVE_PEM)
    mp.delenv("JWT_PREVIOUS_PUBLIC_KEY", raising=False)
    mp.setenv("ENVIRONMENT", "test")
    mp.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="session")
def _broker_da_sessao() -> Iterator[Broker]:
    container, broker = subir_broker()
    try:
        yield broker
    finally:
        container.stop()


@pytest.fixture
def broker(_broker_da_sessao: Broker) -> Iterator[Broker]:
    """RabbitMQ com a topologia do platform; as filas comecam e terminam vazias."""
    _broker_da_sessao.esvaziar()
    yield _broker_da_sessao
    _broker_da_sessao.esvaziar()


@pytest.fixture(scope="session")
def _broker_avulso_da_sessao() -> Iterator[Broker]:
    container, broker = subir_broker()
    try:
        yield broker
    finally:
        container.stop()


@pytest.fixture
def broker_avulso(_broker_avulso_da_sessao: Broker) -> Iterator[Broker]:
    """Um segundo RabbitMQ, para os testes que o poem em alarme de memoria.

    O alarme bloqueia todo publicador do broker: num broker so deles, ele nao
    alcanca os outros testes. O teardown tira o alarme e esvazia as filas.
    """
    _broker_avulso_da_sessao.esvaziar()
    try:
        yield _broker_avulso_da_sessao
    finally:
        _broker_avulso_da_sessao.rabbitmqctl("set_vm_memory_high_watermark", "0.4")
        _broker_avulso_da_sessao.esvaziar()


@pytest.fixture
def saidas(monkeypatch: pytest.MonkeyPatch) -> Iterator[Saidas]:
    """Logs dos processos capturados no teste, para as provas de LGPD.

    O logging como nos processos (o pika so em ERROR), com um registrador a mais
    no root, e os eventos do structlog capturados antes de renderizar (sem o
    scrubber, que mascararia o que o codigo nao deveria ter posto no log). O
    ``_log`` de cada modulo do caminho de uma mensagem e trocado: um logger ja em
    cache escaparia do ``capture_logs``.
    """
    configurar_logging()
    registros = RegistrosDoLog()
    logging.getLogger().addHandler(registros)
    caminho = (amqp, consumidor, relay, orquestrador, use_cases_os, metricas_da_saga)
    for modulo in caminho:
        monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    try:
        with capture_logs() as eventos:
            yield Saidas(registros, eventos)
    finally:
        logging.getLogger().removeHandler(registros)
