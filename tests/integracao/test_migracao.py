"""A migracao Alembic e a fonte do schema: bate com o metadata e e reversivel."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

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
