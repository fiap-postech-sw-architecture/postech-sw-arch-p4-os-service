"""Os indices da migracao 005: o plano das duas consultas do coletor da API.

Cada replica da API raspa essas consultas a cada 15 s. Sem o indice, a idade da
outbox le todas as linhas pendentes (a outbox cresce justo quando o relay ou o
broker param) e a janela de 30 dias varre o historico inteiro. O teste captura o
SQL que o codigo roda de verdade, com os parametros, e pede o plano ao
PostgreSQL com tabelas grandes o bastante para o planejador escolher.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event, text

from src.compartilhado.infraestrutura.mensageria.outbox import (
    idade_da_pendente_mais_antiga,
)
from src.ordem_servico.infraestrutura.consultas import ler_metricas_das_ordens
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Connection, Engine
    from sqlalchemy.orm import Session, sessionmaker

AGORA = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture
def tabelas_grandes(engine: Engine, session_factory: sessionmaker[Session]) -> None:
    """20 mil linhas na outbox, 1% pendente."""
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO outbox (mensagem_id, correlation_id, exchange, "
                "routing_key, envelope, status, criado_em) "
                "SELECT gen_random_uuid(), gen_random_uuid(), 'x', 'y', '{}', "
                "CASE WHEN n % 100 = 0 THEN 'pendente' ELSE 'entregue' END, "
                "now() - make_interval(secs => n) FROM generate_series(1, 20000) n"
            )
        )
    with engine.connect() as conexao:
        conexao.execution_options(isolation_level="AUTOCOMMIT")
        conexao.execute(text("ANALYZE outbox"))


def _capturar(
    engine: Engine, executar: Callable[[Connection], object]
) -> list[tuple[str, Any]]:
    capturados: list[tuple[str, Any]] = []

    def guardar(
        _c: object, _k: object, comando: str, parametros: Any, *_: object
    ) -> None:
        capturados.append((comando, parametros))

    event.listen(engine, "before_cursor_execute", guardar)
    try:
        with engine.connect() as conexao, conexao.begin():
            executar(conexao)
    finally:
        event.remove(engine, "before_cursor_execute", guardar)
    return capturados


def _nos(plano: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plano
    for filho in plano.get("Plans", []):
        yield from _nos(filho)


def _tipos_e_indices(engine: Engine, comando: str, parametros: Any) -> set[str]:
    with engine.connect() as conexao:
        linha = conexao.exec_driver_sql(
            f"EXPLAIN (FORMAT JSON) {comando}", parametros
        ).scalar_one()
    (raiz,) = linha
    return {
        f"{no['Node Type']}:{no.get('Index Name', '')}" for no in _nos(raiz["Plan"])
    }


def test_idade_da_outbox_le_o_indice_e_nao_a_tabela(
    engine: Engine, tabelas_grandes: None
) -> None:
    (consulta, parametros) = _capturar(engine, idade_da_pendente_mais_antiga)[-1]

    nos = _tipos_e_indices(engine, consulta, parametros)

    assert "Index Only Scan:ix_outbox_status_criado_em" in nos or (
        "Index Scan:ix_outbox_status_criado_em" in nos
    )
    # Nem varredura da tabela, nem ordenacao em memoria das pendentes.
    assert not {n for n in nos if n.startswith(("Seq Scan", "Sort", "Bitmap"))}


def test_janela_do_historico_le_o_indice_de_ocorrido_em(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Uma OS com 150 mil linhas de historico, uma a cada 10 minutos para tras (cerca
    # de tres anos): a janela de 30 dias pega 3% delas.
    with session_factory() as sess:
        cliente = criar_cliente_com_veiculo(sess)
        ordem = criar_ordem_recebida(
            sess, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
        )
        sess.execute(
            text("DELETE FROM historico_status_ordem WHERE ordem_id = :id"),
            {"id": ordem.id},
        )
        sess.execute(
            text(
                "INSERT INTO historico_status_ordem (id, ordem_id, sequencia, de, "
                "para, origem, ocorrido_em) SELECT gen_random_uuid(), :ordem, n, "
                "NULL, 'recebida', 'saga', :agora - make_interval(secs => n * 600) "
                "FROM generate_series(1, 150000) n"
            ),
            {"ordem": ordem.id, "agora": AGORA},
        )
        sess.commit()
    with engine.connect() as conexao:
        conexao.execution_options(isolation_level="AUTOCOMMIT")
        conexao.execute(text("ANALYZE historico_status_ordem"))

    capturados = _capturar(engine, lambda c: ler_metricas_das_ordens(c, AGORA))
    (comando, parametros) = next(c for c in capturados if "entradas" in c[0])

    nos = _tipos_e_indices(engine, comando, parametros)

    assert any(n.endswith(":ix_historico_status_ordem_ocorrido_em") for n in nos), nos
    assert not {n for n in nos if n.startswith("Seq Scan")}


def test_os_dois_indices_existem_no_banco_migrado(engine: Engine) -> None:
    with engine.connect() as conexao:
        indices = set(
            conexao.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE indexname IN "
                    "('ix_outbox_status_criado_em', "
                    "'ix_historico_status_ordem_ocorrido_em')"
                )
            ).scalars()
        )

    assert indices == {
        "ix_outbox_status_criado_em",
        "ix_historico_status_ordem_ocorrido_em",
    }
