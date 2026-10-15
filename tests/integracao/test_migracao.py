"""A migracao Alembic e a fonte do schema: bate com o metadata e e reversivel."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from src.compartilhado.infraestrutura.database import metadata
from tests.integracao.conftest import alembic

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

_TABELAS = {
    "clientes",
    "veiculos",
    "consentimentos",
    "usuarios",
    "tokens_revogados",
    "ordens_de_servico",
    "historico_status_ordem",
    "outbox",
    "mensagens_processadas",
    "sagas",
    "notificacoes",
}


def test_migracao_bate_com_o_metadata(engine: Engine) -> None:
    # O engine da sessao foi criado por `alembic upgrade head` (conftest).
    with engine.connect() as conn:
        diferencas = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diferencas == []


def test_schema_da_fase_4(engine: Engine) -> None:
    inspetor = inspect(engine)
    assert set(inspetor.get_table_names()) == _TABELAS | {"alembic_version"}
    colunas_os = {c["name"] for c in inspetor.get_columns("ordens_de_servico")}
    assert "versao" in colunas_os
    assert {"orcamento_id", "pagamento_id", "motivo_cancelamento"} <= colunas_os
    assert not {"orcamento_json", "escopo_aprovado_json"} & colunas_os
    assert "ator" in {c["name"] for c in inspetor.get_columns("historico_status_ordem")}


def test_indices_parciais_da_saga(engine: Engine) -> None:
    # O compare_metadata nao confere o WHERE de indice parcial.
    with engine.connect() as conn:
        definicoes = dict(
            conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename = 'sagas' AND indexname LIKE 'ix_%'"
                )
            ).all()
        )
    assert definicoes["ix_sagas_prazo"].endswith(
        "(prazo_resposta_em) WHERE (prazo_resposta_em IS NOT NULL)"
    )
    assert definicoes["ix_sagas_ativas"].endswith(
        "(etapa, etapa_desde) WHERE ((etapa)::text <> ALL "
        "((ARRAY['concluida'::character varying, "
        "'compensada'::character varying])::text[]))"
    )


@pytest.fixture
def banco_vazio(engine: Engine) -> Iterator[str]:
    """Banco novo no mesmo servidor do container (sem subir outro container)."""
    nome = "migracao_ida_volta"
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {nome}"))
        conn.execute(text(f"CREATE DATABASE {nome}"))
    url = engine.url.set(database=nome).render_as_string(hide_password=False)
    yield url
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {nome} WITH (FORCE)"))


def _migrar_com_prazo(url: str, prazo_s: float = 15) -> Exception | None:
    """``alembic upgrade head`` numa thread; devolve o erro dele (ou ``None``).

    Uma migracao sem ``lock_timeout`` ficaria presa no lock que o teste segura e
    o pytest nao a interromperia: o job de CI penduraria em vez de reprovar. Com
    o prazo, a migracao presa reprova o teste e o ``with`` do teste solta o lock.
    """
    erros: list[Exception] = []

    def rodar() -> None:
        try:
            alembic(url)
        except Exception as erro:  # devolvido ao teste
            erros.append(erro)

    thread = threading.Thread(target=rodar, daemon=True)
    thread.start()
    thread.join(prazo_s)
    if thread.is_alive():
        pytest.fail(f"a migracao ficou presa no lock por mais de {prazo_s} s")
    return erros[0] if erros else None


def test_upgrade_downgrade_upgrade(banco_vazio: str) -> None:
    eng = create_engine(banco_vazio)
    try:
        alembic(banco_vazio)
        assert set(inspect(eng).get_table_names()) >= _TABELAS

        alembic(banco_vazio, "base", descer=True)
        assert set(inspect(eng).get_table_names()) == {"alembic_version"}

        alembic(banco_vazio)
        assert set(inspect(eng).get_table_names()) >= _TABELAS
    finally:
        eng.dispose()


def test_migracao_da_saga_desiste_do_lock_em_segundos(banco_vazio: str) -> None:
    # Uma leitura longa segura o historico: o ALTER TABLE da 003 espera o
    # lock_timeout e falha (o Job tenta de novo), sem enfileirar as leituras
    # da app atras dele indefinidamente.
    eng = create_engine(banco_vazio)
    try:
        alembic(banco_vazio, "002")
        with eng.connect() as leitura, leitura.begin():
            leitura.execute(text("SELECT count(*) FROM historico_status_ordem"))
            erro = _migrar_com_prazo(banco_vazio)
        assert isinstance(erro, OperationalError)
        assert "lock" in str(erro)
        alembic(banco_vazio)
        assert "sagas" in inspect(eng).get_table_names()
    finally:
        eng.dispose()


def test_notificacoes_sem_dado_pessoal_e_com_os_indices_do_claim(
    engine: Engine,
) -> None:
    inspetor = inspect(engine)
    colunas = {c["name"] for c in inspetor.get_columns("notificacoes")}
    indices = {
        i["name"]: i["column_names"] for i in inspetor.get_indexes("notificacoes")
    }

    assert colunas == {
        "id",
        "ordem_id",
        "modelo",
        "status_da_ordem",
        "motivo",
        "criada_em",
        "situacao",
        "tentativas",
        "proxima_tentativa_em",
        "entregue_em",
        "ultimo_erro",
    }
    assert indices == {
        "ix_notificacoes_claim": ["situacao", "proxima_tentativa_em"],
        "ix_notificacoes_ordem": ["ordem_id", "id", "situacao"],
    }


def test_migracao_das_notificacoes_desiste_do_lock_em_segundos(
    banco_vazio: str,
) -> None:
    # Uma escrita longa em ordens_de_servico segura o lock que a chave
    # estrangeira da 004 pede: a migracao falha em segundos (o Job tenta de
    # novo), sem enfileirar as escritas da app atras dela.
    eng = create_engine(banco_vazio)
    try:
        alembic(banco_vazio, "003")
        with eng.connect() as escrita, escrita.begin():
            escrita.execute(text("LOCK TABLE ordens_de_servico IN ROW EXCLUSIVE MODE"))
            erro = _migrar_com_prazo(banco_vazio)
        assert isinstance(erro, OperationalError)
        assert "lock" in str(erro)
        alembic(banco_vazio)
        assert "notificacoes" in inspect(eng).get_table_names()
    finally:
        eng.dispose()


@pytest.mark.parametrize("tabela", ["outbox", "historico_status_ordem"])
def test_migracao_dos_indices_da_observabilidade_desiste_do_lock_em_segundos(
    banco_vazio: str, tabela: str
) -> None:
    # Uma escrita longa na tabela segura o lock que o CREATE INDEX da 005 pede: a
    # migracao espera o lock_timeout (5 s) e falha (o Job tenta de novo), sem
    # enfileirar as escritas da app atras dela.
    eng = create_engine(banco_vazio)
    try:
        alembic(banco_vazio, "004")
        with eng.connect() as escrita, escrita.begin():
            escrita.execute(text(f"LOCK TABLE {tabela} IN ROW EXCLUSIVE MODE"))
            erro = _migrar_com_prazo(banco_vazio)
        assert isinstance(erro, OperationalError)
        assert "lock" in str(erro)
        alembic(banco_vazio)
        assert "ix_outbox_status_criado_em" in {
            i["name"] for i in inspect(eng).get_indexes("outbox")
        }
    finally:
        eng.dispose()
