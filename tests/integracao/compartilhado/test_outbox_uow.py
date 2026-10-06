"""Transactional outbox: os eventos de integracao da OS saem no mesmo commit."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from src.compartilhado.infraestrutura.outbox_mapping import (
    mensagens_processadas_table,
)
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def test_tabelas_da_outbox_existem(session: Session) -> None:
    nomes = set(inspect(session.connection()).get_table_names())
    assert {"outbox", "mensagens_processadas"} <= nomes


def _abrir(session: Session) -> OrdemDeServico:
    cliente = criar_cliente_com_veiculo(session)
    return OrdemDeServico.abrir(
        cliente_id=cliente.id,
        veiculo_id=cliente.veiculos[0].id,
        descricao_problema="Pneu careca",
    )


def _linhas_outbox(session: Session, agregado_id: object) -> list:
    return session.execute(
        text(
            "SELECT tipo, status, tentativas, payload FROM outbox "
            "WHERE agregado_id = :id ORDER BY id"
        ),
        {"id": agregado_id},
    ).all()


def test_abertura_e_transicao_vao_para_a_outbox_no_commit(session: Session) -> None:
    repo = OrdemDeServicoSQLAlchemyRepository(session=session)
    ordem = _abrir(session)
    uow = SQLAlchemyUnitOfWork(session_factory=lambda: session)

    with uow:
        repo.salvar(ordem)
        ordem.registrar_diagnostico_iniciado()
        repo.salvar(ordem)
        uow.commit()

    linhas = _linhas_outbox(session, ordem.id)
    assert [linha.tipo for linha in linhas] == [
        "OrdemAbertaEvent",
        "StatusDaOrdemAlteradoEvent",
    ]
    assert {linha.status for linha in linhas} == {"pendente"}
    assert {linha.tentativas for linha in linhas} == {0}
    assert linhas[0].payload["cliente_id"] == str(ordem.cliente_id)
    assert linhas[1].payload["status_anterior"] == "recebida"
    assert linhas[1].payload["status_novo"] == "em_diagnostico"
    assert linhas[1].payload["origem"] == "execucao"
    # Os eventos enfileirados saem do agregado (nao reenviam no proximo commit).
    assert ordem.coletar_eventos() == []


def test_autoflush_antes_do_commit_nao_perde_evento(session: Session) -> None:
    repo = OrdemDeServicoSQLAlchemyRepository(session=session)
    ordem = _abrir(session)
    repo.salvar(ordem)
    uow = SQLAlchemyUnitOfWork(session_factory=lambda: session)

    with uow:
        # A query dispara autoflush: a ordem sai de `session.new` e, sem nova
        # alteracao, nao entra em `session.dirty`. A UoW varre o identity_map.
        session.execute(select(OrdemDeServico).limit(1)).all()
        uow.commit()

    assert [linha.tipo for linha in _linhas_outbox(session, ordem.id)] == [
        "OrdemAbertaEvent"
    ]


def test_mensagem_processada_nao_repete(session: Session) -> None:
    mensagem_id = uuid4()
    session.execute(
        mensagens_processadas_table.insert().values(id=mensagem_id, tipo="Teste")
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            mensagens_processadas_table.insert().values(id=mensagem_id, tipo="Teste")
        )
