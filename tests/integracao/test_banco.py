"""``python -m src.banco aguardar``: o initContainer que espera a migracao.

Contra um PostgreSQL de verdade, com o ``alembic_version`` na mao de cada teste.
A espera entre as tentativas e injetada: o teste muda o banco no meio dela, em vez
de esperar relogio.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, NoReturn

import pytest
from sqlalchemy import create_engine, inspect, text

from src import banco
from src.compartilhado.infraestrutura.database import criar_engine
from tests.migracoes import revisao_anterior, revisao_head

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Engine
    from testcontainers.community.postgres import PostgresContainer


class _EsperaInterrompidaError(Exception):
    """Fim do teste de um laco que, sem ela, nao terminaria."""


@pytest.fixture(scope="module")
def postgres() -> Iterator[PostgresContainer]:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as container:
        yield container


@pytest.fixture
def engine(postgres: PostgresContainer) -> Iterator[Engine]:
    """Um banco vazio por teste, no mesmo servidor."""
    nome = f"banco_{secrets.token_hex(4)}"
    url = postgres.get_connection_url()
    administrador = create_engine(url, isolation_level="AUTOCOMMIT")
    with administrador.connect() as conexao:
        conexao.execute(text(f"CREATE DATABASE {nome}"))
    engine = criar_engine(
        postgres.get_connection_url().rsplit("/", 1)[0] + f"/{nome}",
        pool_size=1,
        max_overflow=0,
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with administrador.connect() as conexao:
            conexao.execute(text(f"DROP DATABASE {nome} WITH (FORCE)"))
        administrador.dispose()


def _marcar(engine: Engine, revisao: str | None) -> None:
    """Deixa o ``alembic_version`` do banco na ``revisao`` (``None``: vazio)."""
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num varchar(32) NOT NULL PRIMARY KEY)"
            )
        )
        conexao.execute(text("DELETE FROM alembic_version"))
        if revisao is not None:
            conexao.execute(
                text("INSERT INTO alembic_version VALUES (:revisao)"),
                {"revisao": revisao},
            )


def _no_primeiro_intervalo(acao: Callable[[], None]) -> Callable[[float], None]:
    """A espera que, na primeira vez, executa ``acao`` (o Job que termina)."""
    chamadas: list[float] = []

    def esperar(intervalo: float) -> None:
        chamadas.append(intervalo)
        if len(chamadas) > 1:
            raise _EsperaInterrompidaError
        acao()

    return esperar


def _nunca_espera(_intervalo: float) -> NoReturn:
    pytest.fail("o pod nao devia esperar")


def test_banco_na_head_da_imagem_libera_o_pod(engine: Engine) -> None:
    _marcar(engine, revisao_head())

    banco.aguardar(engine, esperar=_nunca_espera)


def test_banco_adiante_da_imagem_libera_o_pod(engine: Engine) -> None:
    # Depois de um rollout undo a migracao nao desce: o banco fica numa revisao
    # que esta imagem nao conhece, e prender o pod ali travaria o rollback.
    _marcar(engine, "999")

    banco.aguardar(engine, esperar=_nunca_espera)


def test_banco_na_revisao_anterior_espera_o_job_chegar_na_head(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    _marcar(engine, revisao_anterior())

    banco.aguardar(
        engine, esperar=_no_primeiro_intervalo(lambda: _marcar(engine, revisao_head()))
    )

    assert capsys.readouterr().err == (
        "waiting for the migration Job: database at revision "
        f"{revisao_anterior()}, this image needs {revisao_head()}\n"
    )


def test_banco_sem_a_tabela_de_versao_espera(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    banco.aguardar(
        engine, esperar=_no_primeiro_intervalo(lambda: _marcar(engine, revisao_head()))
    )

    assert capsys.readouterr().err == "waiting for the migration Job: no revision yet\n"


def test_banco_com_a_tabela_de_versao_vazia_espera(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    _marcar(engine, None)

    banco.aguardar(
        engine, esperar=_no_primeiro_intervalo(lambda: _marcar(engine, revisao_head()))
    )

    assert capsys.readouterr().err == "waiting for the migration Job: no revision yet\n"


def test_banco_fora_do_alcance_espera_e_diz_a_causa_sem_a_senha(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    senha = secrets.token_hex(24)
    # Pelo IPv4, como o nome do banco no cluster: o localhost daqui tenta o ::1
    # antes, e a primeira linha do erro seria a recusa dele.
    errada = criar_engine(
        engine.url.set(password=senha, host="127.0.0.1").render_as_string(
            hide_password=False
        ),
        pool_size=1,
        max_overflow=0,
    )

    try:
        with pytest.raises(_EsperaInterrompidaError):
            banco.aguardar(errada, esperar=_no_primeiro_intervalo(lambda: None))
    finally:
        errada.dispose()

    erro = capsys.readouterr().err
    assert 'password authentication failed for user "test"' in erro
    assert erro.startswith("waiting for the migration Job: OperationalError: ")
    assert senha not in erro


def test_aguardar_so_le_o_banco(engine: Engine) -> None:
    # Com o banco vazio, esperar nao cria o alembic_version nem tabela nenhuma.
    with pytest.raises(_EsperaInterrompidaError):
        banco.aguardar(engine, esperar=_no_primeiro_intervalo(lambda: None))

    assert inspect(engine).get_table_names() == []


def test_revisoes_da_imagem_trazem_a_head_e_as_anteriores() -> None:
    head, conhecidas = banco.revisoes_da_imagem()

    assert head == revisao_head()
    assert {head, revisao_anterior(), "001"} <= conhecidas
    assert "999" not in conhecidas


def test_main_aguardar_usa_a_database_url_do_ambiente(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _marcar(engine, revisao_head())
    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))

    banco.main(["aguardar"])


@pytest.mark.parametrize(
    "args",
    [
        pytest.param([], id="sem-comando"),
        pytest.param(["preparar"], id="outro-comando"),
    ],
)
def test_main_so_aceita_aguardar(args: list[str]) -> None:
    with pytest.raises(SystemExit, match=r"usage: python -m src\.banco aguardar"):
        banco.main(args)
