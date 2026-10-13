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

    from sqlalchemy import Engine
    from sqlalchemy.sql.elements import TextClause

# Os privilegios de tabela que o papel da aplicacao pode ter: DML nas tabelas
# do dono; nenhum dos outros (o TRUNCATE esvazia a tabela sem passar por DML).
_DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
_OUTROS = ("TRUNCATE", "REFERENCES", "TRIGGER")
# O papel do banco e a pergunta de privilegio de cada tabela e sequencia do
# schema public. MATERIALIZED: sem ele o planner pode avaliar a funcao de
# privilegio antes do filtro e falhar num objeto de outro tipo ou de outro schema.
_PRIVILEGIO_NAS_TABELAS = text(
    "WITH t AS MATERIALIZED (SELECT oid, relname FROM pg_class WHERE relkind = 'r' "
    "AND relnamespace = 'public'::regnamespace) "
    "SELECT relname, privilegio, has_table_privilege(:papel, oid, privilegio) "
    "FROM t CROSS JOIN unnest(CAST(:privilegios AS text[])) AS privilegio"
)
_PRIVILEGIO_NAS_SEQUENCIAS = text(
    "WITH s AS MATERIALIZED (SELECT oid, relname FROM pg_class WHERE relkind = 'S' "
    "AND relnamespace = 'public'::regnamespace) "
    "SELECT relname, privilegio, has_sequence_privilege(:papel, oid, privilegio) "
    "FROM s CROSS JOIN unnest(CAST(:privilegios AS text[])) AS privilegio"
)
_ATRIBUTOS_DOS_PAPEIS = text(
    "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
    "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(:papeis)"
)
_PAPEIS = ("os", "os_app", "os_exporter")
_NEGADO = r"permission denied|must be owner"


@pytest.fixture(scope="module")
def banco() -> Iterator[Banco]:
    """O banco do StatefulSet, ja migrado e semeado pelo Job (como no cluster)."""
    with subir_banco(migrar=True) as banco:
        yield banco


@pytest.fixture(scope="module")
def administrador(banco: Banco) -> Iterator[Engine]:
    """O superusuario, so para perguntar ao catalogo o que cada papel pode."""
    engine = create_engine(banco.url_do_superusuario())
    try:
        yield engine
    finally:
        engine.dispose()


def _negado(url: str, comando: str) -> None:
    """O ``comando``, com o papel da ``url``, e recusado por falta de privilegio."""
    engine = create_engine(url)
    try:
        with (
            engine.connect() as conexao,
            pytest.raises(ProgrammingError, match=_NEGADO),
        ):
            conexao.execute(text(comando))
    finally:
        engine.dispose()


def _privilegios(
    administrador: Engine,
    consulta: TextClause,
    papel: str,
    privilegios: tuple[str, ...],
) -> dict[tuple[str, str], bool]:
    with administrador.connect() as conexao:
        linhas = conexao.execute(
            consulta, {"papel": papel, "privilegios": list(privilegios)}
        ).all()
    return {(tabela, privilegio): permitido for tabela, privilegio, permitido in linhas}


def test_nenhuma_senha_chega_ao_log_do_servidor(banco: Banco) -> None:
    # O psql troca o \getenv pela senha antes de enviar o CREATE ROLE: so os
    # SET do inicio do papeis.sql a tiram do log, com todo log ligado.
    log = banco.log_do_servidor()

    # O proprio SET sai no log: o log de comandos valia na sessao do script.
    assert "statement: SET log_statement = 'none';" in log
    assert [chave for chave, senha in banco.senhas.items() if senha in log] == []


def test_papeis_fazem_login_e_nao_administram_nada(administrador: Engine) -> None:
    with administrador.connect() as conexao:
        atributos = {
            papel: tuple(linha)
            for papel, *linha in conexao.execute(
                _ATRIBUTOS_DOS_PAPEIS, {"papeis": list(_PAPEIS)}
            ).all()
        }

    # Login sim; superusuario, CREATEROLE, CREATEDB, replicacao e BYPASSRLS nao.
    assert atributos == dict.fromkeys(
        _PAPEIS, (True, False, False, False, False, False)
    )


def test_so_o_exporter_pertence_a_um_papel_e_e_o_pg_monitor(
    administrador: Engine,
) -> None:
    # A aplicacao fora do pg_write_all_data (escrita em qualquer tabela) e do
    # pg_monitor; o dono sem membership nenhuma.
    with administrador.connect() as conexao:
        membros = conexao.execute(
            text(
                "SELECT membro.rolname, papel.rolname FROM pg_auth_members "
                "JOIN pg_roles membro ON membro.oid = member "
                "JOIN pg_roles papel ON papel.oid = roleid "
                "WHERE membro.rolname = ANY(:papeis)"
            ),
            {"papeis": list(_PAPEIS)},
        ).all()

    assert [tuple(linha) for linha in membros] == [("os_exporter", "pg_monitor")]


def test_papel_da_aplicacao_faz_dml_em_toda_tabela_e_nenhum_outro_privilegio(
    banco: Banco, administrador: Engine
) -> None:
    tabelas = _privilegios(
        administrador, _PRIVILEGIO_NAS_TABELAS, "os_app", (*_DML, *_OUTROS)
    )
    sequencias = _privilegios(
        administrador,
        _PRIVILEGIO_NAS_SEQUENCIAS,
        "os_app",
        ("USAGE", "SELECT", "UPDATE"),
    )

    nomes = {tabela for tabela, _ in tabelas}
    assert {"alembic_version", "outbox", "usuarios"} <= nomes
    # DML em toda tabela, e nenhum TRUNCATE, REFERENCES nem TRIGGER.
    assert tabelas == {
        (tabela, privilegio): privilegio in _DML
        for tabela in nomes
        for privilegio in (*_DML, *_OUTROS)
    }
    # Sequencias: ler e usar, nao reposicionar.
    assert {privilegio for (_, privilegio), ok in sequencias.items() if ok} == {
        "USAGE",
        "SELECT",
    }
    assert {nome for nome, _ in sequencias}


@pytest.mark.parametrize(
    "comando",
    [
        pytest.param("CREATE TABLE intrusa (id int)", id="cria-tabela"),
        pytest.param("DROP TABLE outbox", id="apaga-tabela"),
        pytest.param("ALTER TABLE outbox ADD COLUMN intrusa int", id="altera-tabela"),
        pytest.param("CREATE INDEX ix_intrusa ON outbox (id)", id="cria-indice"),
        pytest.param("TRUNCATE outbox", id="trunca-tabela"),
        pytest.param("CREATE SCHEMA intrusa", id="cria-schema"),
        pytest.param("CREATE EXTENSION pg_trgm", id="cria-extensao"),
        pytest.param("CREATE TEMP TABLE intrusa (id int)", id="cria-tabela-temporaria"),
        pytest.param("CREATE ROLE intrusa LOGIN", id="cria-papel"),
        pytest.param("SET ROLE os", id="assume-o-dono"),
        pytest.param("COPY (SELECT 1) TO PROGRAM 'id'", id="copy-para-programa"),
        pytest.param("SELECT pg_read_file('/etc/passwd')", id="le-arquivo-do-servidor"),
    ],
)
def test_papel_da_aplicacao_nao_altera_schema_nem_chega_ao_servidor(
    banco: Banco, comando: str
) -> None:
    _negado(banco.url_do("Deployment", "os-service-api", "api"), comando)


@pytest.mark.parametrize(
    "comando",
    [
        pytest.param("CREATE ROLE intrusa LOGIN", id="cria-papel"),
        pytest.param("COPY (SELECT 1) TO PROGRAM 'id'", id="copy-para-programa"),
        pytest.param("SELECT pg_read_file('/etc/passwd')", id="le-arquivo-do-servidor"),
    ],
)
def test_dono_do_schema_nao_cria_papel_nem_chega_ao_servidor(
    banco: Banco, comando: str
) -> None:
    _negado(banco.url_do("Job", "os-migracao", "migracao"), comando)


@pytest.mark.parametrize(
    ("papel", "banco_alvo", "privilegio", "permitido"),
    [
        pytest.param("os", "os", "CONNECT", True, id="dono-conecta-no-proprio-banco"),
        pytest.param("os", "os", "CREATE", True, id="dono-cria-no-proprio-banco"),
        pytest.param("os", "postgres", "CONNECT", False, id="dono-fora-do-postgres"),
        pytest.param("os_app", "os", "CONNECT", True, id="app-conecta"),
        pytest.param(
            "os_app", "os", "TEMPORARY", False, id="app-sem-tabela-temporaria"
        ),
        pytest.param("os_app", "os", "CREATE", False, id="app-sem-create-no-banco"),
        pytest.param("os_app", "postgres", "CONNECT", False, id="app-fora-do-postgres"),
        pytest.param("os_exporter", "os", "CONNECT", True, id="exporter-conecta"),
        pytest.param(
            "os_exporter", "os", "TEMPORARY", False, id="exporter-sem-tabela-temporaria"
        ),
        pytest.param(
            "os_exporter", "postgres", "CONNECT", False, id="exporter-fora-do-postgres"
        ),
    ],
)
def test_so_o_dono_tem_mais_que_o_connect_no_banco(
    administrador: Engine, papel: str, banco_alvo: str, privilegio: str, permitido: bool
) -> None:
    # O PUBLIC tem CONNECT e TEMPORARY em todo banco novo, e o GRANT CONNECT do
    # papeis.sql nao diria nada se o script nao os tirasse.
    with administrador.connect() as conexao:
        tem = conexao.execute(
            text("SELECT has_database_privilege(:papel, :banco, :privilegio)"),
            {"papel": papel, "banco": banco_alvo, "privilegio": privilegio},
        ).scalar_one()

    assert tem is permitido


def test_api_le_a_versao_do_banco(banco: Banco) -> None:
    engine = create_engine(banco.url_do("Deployment", "os-service-api", "api"))
    try:
        with engine.connect() as conexao:
            versao = conexao.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    finally:
        engine.dispose()

    assert versao == revisao_head()


@pytest.mark.parametrize(
    "comando",
    [
        pytest.param("CREATE TABLE intrusa (id int)", id="cria-tabela"),
        pytest.param("CREATE TEMP TABLE intrusa (id int)", id="cria-tabela-temporaria"),
        pytest.param("SELECT 1 FROM usuarios", id="le-tabela-da-aplicacao"),
        pytest.param("SELECT 1 FROM alembic_version", id="le-a-versao"),
        pytest.param("COPY (SELECT 1) TO PROGRAM 'id'", id="copy-para-programa"),
        pytest.param("SELECT pg_read_file('/etc/passwd')", id="le-arquivo-do-servidor"),
        pytest.param("SET ROLE os", id="assume-o-dono"),
    ],
)
def test_papel_do_exporter_nao_le_tabela_nem_escreve_nem_chega_ao_servidor(
    banco: Banco, comando: str
) -> None:
    _negado(banco.url_do_exporter(), comando)


def test_papel_do_exporter_le_as_estatisticas_do_servidor(banco: Banco) -> None:
    engine = create_engine(banco.url_do_exporter())
    try:
        with engine.connect() as conexao:
            sessoes = conexao.execute(
                text("SELECT count(*) FROM pg_stat_activity")
            ).scalar_one()
            monitor = conexao.execute(
                text("SELECT pg_has_role('pg_monitor', 'USAGE')")
            ).scalar_one()
    finally:
        engine.dispose()

    assert sessoes >= 1
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
