"""O PostgreSQL dos manifestos Kubernetes na imagem real, sem cluster.

O banco sobe com a imagem, o ambiente, o ``papeis.sql`` e o securityContext do
StatefulSet de ``k8s/base`` (as senhas, que no cluster vem do Secret
``os-postgres``, sao geradas aqui), e o Job de migracao roda o comando dele como
o dono. Prova o que os manifestos prometem (ADR-042): um papel por uso, nenhuma
senha no log do servidor, nenhum ``trust`` no loopback, a sonda ``pg_isready``
e a espera do ``aguarda-migracao``.
"""

from __future__ import annotations

import secrets
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from tests.integracao.banco_do_manifesto import Banco, no_host, subir_banco
from tests.manifestos import container, literais_do_servico, objeto
from tests.migracoes import revisao_anterior, revisao_head

if TYPE_CHECKING:
    from collections.abc import Iterator

# Privilegio do papel conectado em cada tabela e sequencia do schema public.
# MATERIALIZED: sem ele o planner pode avaliar a funcao de privilegio antes do
# filtro e falhar num objeto de outro tipo ou de outro schema.
_PRIVILEGIO_NAS_TABELAS = text(
    "WITH t AS MATERIALIZED (SELECT oid, relname FROM pg_class WHERE relkind = 'r' "
    "AND relnamespace = 'public'::regnamespace) "
    "SELECT relname, has_table_privilege(oid, 'SELECT,INSERT,UPDATE,DELETE') FROM t"
)
_PRIVILEGIO_NAS_SEQUENCIAS = text(
    "WITH s AS MATERIALIZED (SELECT oid, relname FROM pg_class WHERE relkind = 'S' "
    "AND relnamespace = 'public'::regnamespace) "
    "SELECT relname, has_sequence_privilege(oid, 'USAGE') FROM s"
)


@pytest.fixture(scope="module")
def banco() -> Iterator[Banco]:
    """O banco do StatefulSet, ja migrado e semeado pelo Job (como no cluster)."""
    with subir_banco(migrar=True) as banco:
        yield banco


def test_nenhuma_senha_chega_ao_log_do_servidor(banco: Banco) -> None:
    # O psql troca o \getenv pela senha antes de enviar o CREATE ROLE: so os
    # SET do inicio do papeis.sql a tiram do log, com todo log ligado.
    log = banco.log_do_servidor()

    # O proprio SET sai no log: o log de comandos valia na sessao do script.
    assert "statement: SET log_statement = 'none';" in log
    assert [chave for chave, senha in banco.senhas.items() if senha in log] == []


def test_papel_da_aplicacao_faz_dml_em_toda_tabela_e_nao_cria_tabela(
    banco: Banco,
) -> None:
    engine = create_engine(banco.url_do("Deployment", "os-service-api", "api"))
    try:
        with engine.connect() as conexao:
            tabelas = dict(conexao.execute(_PRIVILEGIO_NAS_TABELAS).all())
            sequencias = dict(conexao.execute(_PRIVILEGIO_NAS_SEQUENCIAS).all())
            versao = conexao.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            with pytest.raises(ProgrammingError, match="permission denied"):
                conexao.execute(text("CREATE TABLE intrusa (id int)"))
            conexao.rollback()
            with pytest.raises(ProgrammingError, match="must be owner"):
                conexao.execute(text("DROP TABLE outbox"))
    finally:
        engine.dispose()

    # Todas as tabelas da migracao, inclusive a alembic_version, e a sequencia
    # do id da outbox.
    assert {"alembic_version", "outbox", "usuarios"} <= tabelas.keys()
    assert all(tabelas.values()), tabelas
    assert sequencias
    assert all(sequencias.values()), sequencias
    assert versao == revisao_head()


def test_papel_do_exporter_le_as_estatisticas_e_nenhuma_tabela(banco: Banco) -> None:
    engine = create_engine(banco.url_do_exporter())
    try:
        with engine.connect() as conexao:
            # pg_monitor: le as estatisticas de todas as sessoes, inclusive o
            # texto das consultas dos outros papeis.
            monitor = conexao.execute(
                text("SELECT pg_has_role('pg_monitor', 'USAGE')")
            ).scalar_one()
            with pytest.raises(ProgrammingError, match="permission denied"):
                conexao.execute(text("SELECT 1 FROM usuarios"))
    finally:
        engine.dispose()

    assert monitor is True


def test_loopback_e_socket_pedem_senha_e_o_exporter_entra_com_a_dele(
    banco: Banco,
) -> None:
    # Sem trust: o sidecar, no mesmo pod, nao entra como postgres sem senha.
    loopback = banco.no_container(
        "psql", "-w", "-h", "127.0.0.1", "-U", "postgres", "-d", "os", "-c", "SELECT 1"
    )
    socket = banco.no_container(
        "psql", "-w", "-U", "postgres", "-d", "os", "-c", "SELECT 1"
    )
    exporter = banco.no_container(
        "psql",
        "-w",
        "-h",
        "127.0.0.1",
        "-U",
        "os_exporter",
        "-d",
        "os",
        "-c",
        "SELECT 1",
        senha=banco.senhas["POSTGRES_EXPORTER_PASSWORD"],
    )

    assert loopback.exit_code != 0
    assert b"password" in loopback.output
    assert socket.exit_code != 0
    assert b"password" in socket.output
    assert exporter.exit_code == 0


def test_sonda_do_manifesto_responde_com_o_banco_de_pe(banco: Banco) -> None:
    sonda = container(objeto("StatefulSet", "os-postgres"), "postgres")
    resultado = banco.no_container(*sonda["readinessProbe"]["exec"]["command"])

    assert resultado.exit_code == 0, resultado.output


def test_api_sobe_e_grava_com_o_papel_da_aplicacao(
    banco: Banco, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.main import criar_app
    from tests.chaves_jwt import CHAVE_PEM

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("JWT_PRIVATE_KEY", CHAVE_PEM)
    monkeypatch.delenv("JWT_PREVIOUS_PUBLIC_KEY", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", banco.url_do("Deployment", "os-service-api", "api")
    )

    with TestClient(criar_app()) as cliente:
        pronto = cliente.get("/api/v1/saude/pronto")
        login = cliente.post(
            "/api/v1/autenticacao/login",
            json={
                "email": literais_do_servico()["ADMIN_EMAIL"],
                "senha": banco.senhas["ADMIN_PASSWORD"],
            },
        )
        # O refresh revoga o anterior: um INSERT em tokens_revogados.
        refresh = cliente.post(
            "/api/v1/autenticacao/refresh",
            json={"refresh_token": login.json()["refresh_token"]},
        )

    assert pronto.status_code == 200
    assert login.status_code == 200
    assert refresh.status_code == 200


def _espera_do_pod() -> list[str]:
    espera = container(objeto("Deployment", "os-service-api"), "aguarda-migracao")
    return list(espera["command"])


def _da_aplicacao(banco: Banco) -> dict[str, str]:
    espera = banco.url_do("Deployment", "os-service-api", "aguarda-migracao")
    return {"DATABASE_URL": espera}


def _ainda_esperando(ambiente: dict[str, str]) -> bytes:
    """O log da espera, que nao termina enquanto a condicao nao vale."""
    # Duas voltas de 2 s mais a partida do Python.
    with pytest.raises(subprocess.TimeoutExpired) as em_espera:
        no_host(_espera_do_pod(), ambiente, prazo_s=6)
    return em_espera.value.stderr or b""


@contextmanager
def _banco_na_revisao(banco: Banco, revisao: str) -> Iterator[None]:
    """O ``alembic_version`` do banco do modulo em ``revisao``.

    A fixture e do modulo: ao sair, devolve a revisao que o Job deixou.
    """
    dono = create_engine(banco.url_do("Job", "os-migracao", "migracao"))

    def gravar(versao: str) -> None:
        with dono.begin() as conexao:
            conexao.execute(text("DELETE FROM alembic_version"))
            conexao.execute(
                text("INSERT INTO alembic_version VALUES (:versao)"),
                {"versao": versao},
            )

    try:
        with dono.connect() as conexao:
            original = conexao.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        gravar(revisao)
        try:
            yield
        finally:
            gravar(original)
    finally:
        dono.dispose()


@pytest.mark.parametrize(
    "revisao",
    [
        pytest.param(revisao_head(), id="head-desta-imagem"),
        pytest.param("999", id="rollback-banco-adiante"),
    ],
)
def test_aguarda_migracao_do_manifesto_libera_o_pod_com_o_banco_em_head_ou_adiante(
    banco: Banco, revisao: str
) -> None:
    with _banco_na_revisao(banco, revisao):
        assert no_host(_espera_do_pod(), _da_aplicacao(banco), prazo_s=60) == 0


def test_aguarda_migracao_do_manifesto_espera_o_banco_na_revisao_anterior(
    banco: Banco,
) -> None:
    with _banco_na_revisao(banco, revisao_anterior()):
        log = _ainda_esperando(_da_aplicacao(banco))

    esperado = (
        f"waiting for the migration Job: database at revision {revisao_anterior()}, "
        f"this image needs {revisao_head()}\n"
    )
    assert esperado.encode() in log


def test_aguarda_migracao_diz_no_log_por_que_nao_conecta(banco: Banco) -> None:
    # Pelo IPv4, como o nome do banco no cluster: o localhost daqui tenta o ::1
    # antes, e a primeira linha do erro seria a recusa dele.
    porta = banco.container.get_exposed_port(5432)
    errada = f"postgresql://os_app:{secrets.token_hex(24)}@127.0.0.1:{porta}/os"

    log = _ainda_esperando({"DATABASE_URL": errada})

    assert b'password authentication failed for user "os_app"' in log
