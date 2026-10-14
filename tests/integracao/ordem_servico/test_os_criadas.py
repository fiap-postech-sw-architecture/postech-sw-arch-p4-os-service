"""``pytstop_os_criadas_total``: conta a abertura da OS depois do commit (ADR-043).

O painel "Volume diario de OS" (o pedido da banca da fase 3) soma este contador:
uma OS que nao foi gravada nao pode contar. A regra e a das metricas da saga
(``metricas_da_saga.registrar``): so o commit da transacao raiz conta.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.infraestrutura import metrics as metricas_os
from src.ordem_servico.infraestrutura.metrics import instrumentar_metricas_de_ordens
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.conftest import valor_da_metrica
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from sqlalchemy.orm import sessionmaker

_CRIADAS = "pytstop_os_criadas_total"


@pytest.fixture(autouse=True)
def _listener_so_neste_teste() -> Iterator[None]:
    """O listener e da classe ``Session`` (global): sai ao fim, para nao medir os
    flushes dos testes seguintes."""
    ja_havia = event.contains(Session, "before_flush", metricas_os._observar_flush)
    instrumentar_metricas_de_ordens()
    yield
    if not ja_havia:
        event.remove(Session, "before_flush", metricas_os._observar_flush)
        metricas_os._instrumentado = False


def _abrir(sess: Session) -> OrdemDeServico:
    cliente = criar_cliente_com_veiculo(sess)
    ordem = OrdemDeServico.abrir(
        cliente_id=cliente.id,
        veiculo_id=cliente.veiculos[0].id,
        descricao_problema="Vazamento de oleo",
        ator="atendente-teste",
    )
    OrdemDeServicoSQLAlchemyRepository(sess).salvar(ordem)
    return ordem


def test_conta_a_abertura_so_depois_do_commit(
    session_factory: sessionmaker[Session],
) -> None:
    antes = valor_da_metrica(_CRIADAS)

    with session_factory() as sess:
        _abrir(sess)
        # Gravada na transacao (o flush do repositorio), mas ainda nao comitada.
        assert valor_da_metrica(_CRIADAS) == antes
        sess.commit()

    assert valor_da_metrica(_CRIADAS) == antes + 1


def test_rollback_descarta_a_abertura(session_factory: sessionmaker[Session]) -> None:
    antes = valor_da_metrica(_CRIADAS)

    with session_factory() as sess:
        _abrir(sess)
        sess.rollback()

    assert valor_da_metrica(_CRIADAS) == antes


def test_commit_que_falha_nao_conta(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # O COMMIT falha (trigger deferido), depois de a OS ja ter sido gravada na
    # transacao: o contador antigo, do `before_flush`, contava essa OS.
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "CREATE FUNCTION recusar_ordem() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'ordem recusada no commit'; END $$"
            )
        )
        conexao.execute(
            text(
                "CREATE CONSTRAINT TRIGGER recusar_ordem AFTER INSERT ON "
                "ordens_de_servico DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION recusar_ordem()"
            )
        )
    antes = valor_da_metrica(_CRIADAS)
    try:
        with session_factory() as sess:
            _abrir(sess)
            with pytest.raises(DBAPIError, match="ordem recusada no commit"):
                sess.commit()
    finally:
        with engine.begin() as conexao:
            conexao.execute(text("DROP TRIGGER recusar_ordem ON ordens_de_servico"))
            conexao.execute(text("DROP FUNCTION recusar_ordem()"))

    assert valor_da_metrica(_CRIADAS) == antes


def test_cada_ordem_aberta_na_transacao_conta_uma_vez(
    session_factory: sessionmaker[Session],
) -> None:
    antes = valor_da_metrica(_CRIADAS)

    with session_factory() as sess:
        _abrir(sess)
        _abrir(sess)
        sess.commit()

    assert valor_da_metrica(_CRIADAS) == antes + 2


def test_savepoint_desfeito_nao_conta_e_o_da_raiz_conta(
    session_factory: sessionmaker[Session],
) -> None:
    antes = valor_da_metrica(_CRIADAS)

    with session_factory() as sess:
        _abrir(sess)
        with sess.begin_nested() as interno:
            _abrir(sess)
            interno.rollback()
        sess.commit()

    assert valor_da_metrica(_CRIADAS) == antes + 1


def test_transicao_de_status_nao_conta_ordem_nova(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as sess:
        ordem = _abrir(sess)
        sess.commit()
        antes = valor_da_metrica(_CRIADAS)

        ordem.registrar_diagnostico_iniciado(ator="consumidor")
        OrdemDeServicoSQLAlchemyRepository(sess).salvar(ordem)
        sess.commit()

    assert valor_da_metrica(_CRIADAS) == antes


def test_instrumentar_duas_vezes_registra_um_listener_so(
    session_factory: sessionmaker[Session],
) -> None:
    instrumentar_metricas_de_ordens()
    antes = valor_da_metrica(_CRIADAS)

    with session_factory() as sess:
        _abrir(sess)
        sess.commit()

    assert valor_da_metrica(_CRIADAS) == antes + 1
