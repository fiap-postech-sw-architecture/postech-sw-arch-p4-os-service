"""Persistencia da saga no Postgres: JSONB, lock otimista e o contexto de trace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.ordem_servico.aplicacao.saga.modelo import (
    Envio,
    EtapaSaga,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.infraestrutura.repository import SagaSQLAlchemyRepository
from src.ordem_servico.interfaces.dependencies import obter_obter_ordem
from tests.eventos import evento
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
)
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.rastreamento import Rastreador

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)
# OS ja em diagnostico: o DiagnosticoConcluido se classifica para processar.
_MARCOS = MarcosDaOrdem(
    diagnostico_iniciado=True, checkout_aberto=False, encerrada=False
)


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
    concluido = evento("DiagnosticoConcluido", saga.ordem_id)
    saga.avancar(
        concluido,
        _MARCOS,
        agora=AGORA + timedelta(minutes=1),
        ator="consumidor",
        envio=Envio(
            tipo=Comando.GERAR_ORCAMENTO,
            id=uuid4(),
            dados={
                "ordem_id": str(saga.ordem_id),
                "itens": itens_do_diagnostico(concluido.dados),
            },
            prazo_resposta_em=AGORA + timedelta(minutes=3),
        ),
    )


def test_round_trip_com_os_jsonb_e_os_instantes(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as sess:
        saga = _iniciada(_ordem(sess))
        SagaSQLAlchemyRepository(sess).salvar(saga)
        _ate_aguardando_orcamento(saga)
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()
        ordem_id, em_voo, itens = saga.ordem_id, saga.comando_em_voo, saga.itens

    # Sessao nova: a saga vem do banco, nao da identity map de quem gravou.
    with session_factory() as sess:
        lida = SagaSQLAlchemyRepository(sess).obter(ordem_id)
        assert lida is not None
        assert lida.etapa is EtapaSaga.AGUARDANDO_ORCAMENTO
        assert lida.versao == 2
        assert [p["gatilho"] for p in lida.passos] == [
            "abertura",
            "DiagnosticoConcluido",
        ]
        assert (lida.comando_em_voo, lida.itens) == (em_voo, itens)
        assert lida.prazo_resposta_em == AGORA + timedelta(minutes=3)
        assert lida.iniciada_em == AGORA
        assert lida.etapa_desde == lida.atualizada_em == AGORA + timedelta(minutes=1)
        assert (lida.passos_concluidos, lida.plano_compensacao, lida.reenvios) == (
            (),
            (),
            0,
        )
        # O OrcamentoGerado marca o T3 e limpa o comando em voo.
        lida.avancar(
            evento("OrcamentoGerado", ordem_id),
            _MARCOS,
            agora=AGORA + timedelta(minutes=2),
            ator="consumidor",
        )
        SagaSQLAlchemyRepository(sess).salvar(lida)
        sess.commit()

    # Cada JSONB conferido pelo SQL: mutacao no lugar nao chegaria ao banco.
    with session_factory() as sess:
        linha = sess.execute(
            text(
                "SELECT passos, passos_concluidos, comando_em_voo, itens, "
                "plano_compensacao FROM sagas WHERE ordem_id = :id"
            ),
            {"id": ordem_id},
        ).one()
    passos, concluidos, comando, itens_gravados, plano = linha
    assert [p["gatilho"] for p in passos] == [
        "abertura",
        "DiagnosticoConcluido",
        "OrcamentoGerado",
    ]
    assert passos[1]["comando"] == "GerarOrcamento"
    assert (concluidos, comando, plano) == (["T3"], None, [])
    assert itens_gravados == list(itens)


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


def test_consulta_da_os_le_a_saga_no_mesmo_select(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as sess:
        saga = _iniciada(_ordem(sess))
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()
    lidas: list[str] = []

    def registrar(_conexao: Any, _cursor: Any, sql: str, *_: Any) -> None:
        if "sagas" in sql:
            lidas.append(sql)

    event.listen(engine, "before_cursor_execute", registrar)
    try:
        with session_factory() as sess:
            ordem = obter_obter_ordem(sess).executar(saga.ordem_id)
    finally:
        event.remove(engine, "before_cursor_execute", registrar)

    # Status e etapa do mesmo instante: um commit do consumidor entre duas
    # leituras nao faz a consulta mostrar o status velho com a etapa nova.
    (sql,) = lidas
    assert "ordens_de_servico" in sql
    assert (ordem.status, ordem.etapa) == ("recebida", "aguardando_diagnostico")
