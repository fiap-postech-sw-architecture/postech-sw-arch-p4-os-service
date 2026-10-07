"""Persistencia da saga no Postgres: JSONB, lock otimista e o contexto de trace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.ordem_servico.aplicacao.saga.saga import Envio, EtapaSaga, Saga
from src.ordem_servico.infraestrutura.repository import SagaSQLAlchemyRepository
from tests.eventos import evento
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
)
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

    from tests.rastreamento import Rastreador

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _ordem(session: Session) -> UUID:
    cliente = criar_cliente_com_veiculo(session)
    return criar_ordem_recebida(
        session, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
    ).id


def _iniciada(ordem_id: UUID) -> Saga:
    return Saga.iniciar(
        ordem_id,
        envio=Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=uuid4()),
        ator="atendente-teste",
        agora=AGORA,
    )


def _ate_aguardando_orcamento(saga: Saga) -> None:
    saga.avancar(
        evento("DiagnosticoConcluido", saga.ordem_id),
        agora=AGORA + timedelta(minutes=1),
        ator="consumidor",
        envio=Envio(
            tipo=Comando.GERAR_ORCAMENTO,
            id=uuid4(),
            dados={"ordem_id": str(saga.ordem_id), "itens": []},
            prazo_resposta_em=AGORA + timedelta(minutes=3),
        ),
    )


def test_round_trip_com_os_jsonb_e_os_instantes(session: Session) -> None:
    saga = _iniciada(_ordem(session))
    repo = SagaSQLAlchemyRepository(session)
    repo.salvar(saga)
    _ate_aguardando_orcamento(saga)
    repo.salvar(saga)
    session.expire_all()

    lida = repo.obter(saga.ordem_id)

    assert lida is not None
    assert lida.etapa is EtapaSaga.AGUARDANDO_ORCAMENTO
    assert lida.versao == 2
    assert lida.passos == saga.passos
    assert [p["gatilho"] for p in lida.passos] == ["abertura", "DiagnosticoConcluido"]
    assert lida.itens == saga.itens
    assert len(lida.itens) == 2
    assert lida.comando_em_voo == saga.comando_em_voo
    assert lida.prazo_resposta_em == AGORA + timedelta(minutes=3)
    assert lida.iniciada_em == AGORA
    assert lida.etapa_desde == lida.atualizada_em == AGORA + timedelta(minutes=1)
    assert (lida.passos_concluidos, lida.plano_compensacao, lida.reenvios) == (
        (),
        (),
        0,
    )
    # Releitura de verdade: os JSONB voltam do banco, nao da identidade em memoria.
    linha = session.execute(
        text("SELECT etapa, passos -> 1 ->> 'gatilho' FROM sagas WHERE ordem_id = :id"),
        {"id": saga.ordem_id},
    ).one()
    assert tuple(linha) == ("aguardando_orcamento", "DiagnosticoConcluido")


def test_obter_saga_inexistente_devolve_none(session: Session) -> None:
    assert SagaSQLAlchemyRepository(session).obter(uuid4()) is None


def test_segunda_escrita_sobre_a_versao_lida_vira_conflito(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as sess:
        saga = _iniciada(_ordem(sess))
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()
        ordem_id = saga.ordem_id

    with session_factory() as sessao_a, session_factory() as sessao_b:
        repo_a, repo_b = (
            SagaSQLAlchemyRepository(sessao_a),
            SagaSQLAlchemyRepository(sessao_b),
        )
        saga_a, saga_b = repo_a.obter(ordem_id), repo_b.obter(ordem_id)
        assert saga_a is not None
        assert saga_b is not None
        _ate_aguardando_orcamento(saga_a)
        repo_a.salvar(saga_a)
        sessao_a.commit()

        _ate_aguardando_orcamento(saga_b)
        with pytest.raises(ConflitoDeConcorrenciaException, match=str(ordem_id)):
            repo_b.salvar(saga_b)
        sessao_b.rollback()

    with session_factory() as sess:
        final = SagaSQLAlchemyRepository(sess).obter(ordem_id)
        assert final is not None
        assert (final.versao, len(final.passos)) == (2, 2)


def test_salvar_grava_o_contexto_do_span_corrente(
    session: Session, rastreador: Rastreador
) -> None:
    saga = _iniciada(_ordem(session))
    repo = SagaSQLAlchemyRepository(session)

    with rastreador.tracer.start_as_current_span("POST /api/v1/ordens-de-servico"):
        repo.salvar(saga)
    (abertura,) = rastreador.spans()
    assert saga.traceparent == traceparent(abertura)

    # Fora de um span, fica o contexto da ultima transicao.
    _ate_aguardando_orcamento(saga)
    repo.salvar(saga)
    gravado = session.execute(
        text("SELECT traceparent FROM sagas WHERE ordem_id = :id"),
        {"id": saga.ordem_id},
    ).scalar_one()
    assert gravado == traceparent(abertura)
