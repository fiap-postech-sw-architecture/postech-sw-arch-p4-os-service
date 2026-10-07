"""O PostgreSQL dos manifestos Kubernetes na imagem real, sem cluster.

O banco sobe com a imagem, o ambiente e o ``papeis.sql`` do StatefulSet de
``k8s/base`` (as senhas, que no cluster vem do Secret ``os-postgres``, sao
geradas aqui), e o Job de migracao roda o comando dele como o dono. Prova o que
os manifestos prometem (ADR-042): um papel por uso, nenhum ``trust`` no
loopback, a sonda ``pg_isready`` e a espera do ``aguarda-migracao``.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from testcontainers.community.postgres import PostgresContainer

_RAIZ = Path(__file__).resolve().parents[2]
_BASE = _RAIZ / "k8s/base"
_EMAIL_ADMIN = "admin@pytstop.dev"


def _objeto(tipo: str, nome: str) -> dict[str, Any]:
    # Todo YAML de k8s/base, menos o kustomization.yaml (sem metadata).
    for arquivo in sorted(set(_BASE.glob("*.yaml")) - {_BASE / "kustomization.yaml"}):
        for documento in yaml.safe_load_all(arquivo.read_text()):
            if (documento["kind"], documento["metadata"]["name"]) == (tipo, nome):
                return dict(documento)
    msg = f"{tipo}/{nome} nao esta em k8s/base"
    raise AssertionError(msg)


def _container(objeto: dict[str, Any], nome: str) -> dict[str, Any]:
    especificacao = objeto["spec"]["template"]["spec"]
    containers = especificacao["containers"] + especificacao.get("initContainers", [])
    return next(c for c in containers if c["name"] == nome)


def _comando(objeto: dict[str, Any], nome: str) -> str:
    """O script do ``sh -c`` de um container do manifesto."""
    sh, opcao, script = _container(objeto, nome)["command"]
    assert (sh, opcao) == ("sh", "-c")
    return str(script)


@dataclass(frozen=True)
class Banco:
    container: PostgresContainer
    senhas: dict[str, str]

    def url(self, papel: str, chave: str) -> str:
        host = self.container.get_container_host_ip()
        porta = self.container.get_exposed_port(5432)
        return f"postgresql://{papel}:{self.senhas[chave]}@{host}:{porta}/os"

    def no_container(self, *comando: str, senha: str | None = None) -> Any:
        """Roda ``comando`` dentro do container do banco (resultado do exec)."""
        ambiente = {"PGPASSWORD": senha} if senha else {}
        return self.container.get_wrapped_container().exec_run(
            list(comando), environment=ambiente
        )


def _no_host(script: str, ambiente: dict[str, str], prazo_s: float) -> int:
    """Roda o ``sh -c`` de um manifesto aqui, com o alembic e o python do venv."""
    caminho = f"{Path(sys.executable).parent}:{os.environ['PATH']}"
    # Argumentos fixos: o sh do sistema e o script do manifesto do repositorio.
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", script],
        env={"PATH": caminho, **ambiente},
        cwd=_RAIZ,
        capture_output=True,
        timeout=prazo_s,
        check=False,
    ).returncode


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
    from testcontainers.community.postgres import PostgresContainer

    postgres = _container(_objeto("StatefulSet", "os-postgres"), "postgres")
    ambiente = {item["name"]: item["value"] for item in postgres["env"]}
    senhas = {
        chave: secrets.token_hex(24)
        for chave in (
            "POSTGRES_PASSWORD",
            "POSTGRES_OWNER_PASSWORD",
            "POSTGRES_APP_PASSWORD",
            "POSTGRES_EXPORTER_PASSWORD",
            "ADMIN_PASSWORD",
        )
    }
    # O servidor loga todo comando, o pior caso para o script de init (a imagem
    # passa os argumentos tambem ao servidor temporario em que ele roda).
    container = (
        PostgresContainer(
            postgres["image"],
            username=ambiente["POSTGRES_USER"],
            password=senhas["POSTGRES_PASSWORD"],
            dbname=ambiente["POSTGRES_DB"],
        )
        .with_command("postgres -c log_statement=all")
        .with_volume_mapping(
            str(_BASE / "papeis.sql"), "/docker-entrypoint-initdb.d/papeis.sql", "ro"
        )
    )
    for nome, valor in {**ambiente, **senhas}.items():
        container.with_env(nome, valor)
    with container:
        banco = Banco(container, senhas)
        job = {
            "ENVIRONMENT": "production",
            "DATABASE_URL": banco.url("os", "POSTGRES_OWNER_PASSWORD"),
            "ADMIN_EMAIL": _EMAIL_ADMIN,
            "ADMIN_PASSWORD": senhas["ADMIN_PASSWORD"],
        }
        comando = _comando(_objeto("Job", "os-migracao"), "migracao")
        # Duas vezes: o Job roda a cada implantacao e tem de ser idempotente.
        assert _no_host(comando, job, prazo_s=120) == 0
        assert _no_host(comando, job, prazo_s=120) == 0
        yield banco


def test_nenhuma_senha_chega_ao_log_do_servidor(banco: Banco) -> None:
    # O psql troca o \getenv pela senha antes de enviar o CREATE ROLE: so os
    # SET do inicio do papeis.sql a tiram do log, com o log de comandos ligado.
    log = banco.container.get_wrapped_container().logs().decode()

    # O proprio SET sai no log: o log de comandos valia na sessao do script.
    assert "statement: SET log_statement = 'none';" in log
    assert [chave for chave, senha in banco.senhas.items() if senha in log] == []


def test_papel_da_aplicacao_faz_dml_em_toda_tabela_e_nao_cria_tabela(
    banco: Banco,
) -> None:
    engine = create_engine(banco.url("os_app", "POSTGRES_APP_PASSWORD"))
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
    assert versao == "002"


def test_papel_do_exporter_le_as_estatisticas_e_nenhuma_tabela(banco: Banco) -> None:
    engine = create_engine(banco.url("os_exporter", "POSTGRES_EXPORTER_PASSWORD"))
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
    sonda = _container(_objeto("StatefulSet", "os-postgres"), "postgres")
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
    monkeypatch.setenv("DATABASE_URL", banco.url("os_app", "POSTGRES_APP_PASSWORD"))

    with TestClient(criar_app()) as cliente:
        pronto = cliente.get("/api/v1/saude/pronto")
        login = cliente.post(
            "/api/v1/autenticacao/login",
            json={"email": _EMAIL_ADMIN, "senha": banco.senhas["ADMIN_PASSWORD"]},
        )
        # O refresh revoga o anterior: um INSERT em tokens_revogados.
        refresh = cliente.post(
            "/api/v1/autenticacao/refresh",
            json={"refresh_token": login.json()["refresh_token"]},
        )

    assert pronto.status_code == 200
    assert login.status_code == 200
    assert refresh.status_code == 200


@pytest.mark.parametrize(
    ("revisao", "libera"),
    [
        pytest.param(None, False, id="banco-sem-migracao"),
        pytest.param("001", False, id="revisao-anterior"),
        pytest.param("002", True, id="head-desta-imagem"),
        pytest.param("999", True, id="rollback-banco-adiante"),
    ],
)
def test_aguarda_migracao_libera_o_pod_so_com_o_banco_em_head_ou_adiante(
    banco: Banco, revisao: str | None, libera: bool
) -> None:
    dono = create_engine(banco.url("os", "POSTGRES_OWNER_PASSWORD"))
    espera = _comando(_objeto("Deployment", "os-service-api"), "aguarda-migracao")
    aplicacao = {"DATABASE_URL": banco.url("os_app", "POSTGRES_APP_PASSWORD")}
    try:
        with dono.begin() as conexao:
            conexao.execute(text("DELETE FROM alembic_version"))
            if revisao is not None:
                conexao.execute(
                    text("INSERT INTO alembic_version VALUES (:revisao)"),
                    {"revisao": revisao},
                )
        if libera:
            assert _no_host(espera, aplicacao, prazo_s=60) == 0
        else:
            # Segue esperando: o laco nao termina enquanto a condicao nao vale.
            with pytest.raises(subprocess.TimeoutExpired):
                _no_host(espera, aplicacao, prazo_s=6)
    finally:
        with dono.begin() as conexao:
            conexao.execute(text("UPDATE alembic_version SET version_num = '002'"))
            conexao.execute(
                text(
                    "INSERT INTO alembic_version SELECT '002' "
                    "WHERE NOT EXISTS (SELECT 1 FROM alembic_version)"
                )
            )
        dono.dispose()
