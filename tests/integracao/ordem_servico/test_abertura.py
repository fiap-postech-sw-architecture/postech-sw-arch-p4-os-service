"""Abertura da OS (T1): OS, saga e ``SolicitarDiagnostico`` num commit so."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, table, text
from sqlalchemy.exc import DBAPIError

from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.dominio.exceptions import VeiculoNaoEncontradoException
from src.ordem_servico.interfaces.dependencies import obter_abrir_ordem
from tests.integracao.seed_helpers import criar_cliente_com_veiculo
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.rastreamento import Rastreador

_ATOR = "6a1d3c0e-8f7b-4b8e-9f51-2d6c1e0a9b77"


@contextmanager
def outbox_recusando_insert(engine: Engine) -> Iterator[None]:
    """Trigger que falha todo INSERT na outbox: a transacao inteira tem de cair."""
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "CREATE FUNCTION recusar_outbox() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'outbox indisponivel'; END $$"
            )
        )
        conexao.execute(
            text(
                "CREATE TRIGGER recusar_outbox BEFORE INSERT ON outbox "
                "FOR EACH ROW EXECUTE FUNCTION recusar_outbox()"
            )
        )
    try:
        yield
    finally:
        with engine.begin() as conexao:
            conexao.execute(text("DROP TRIGGER recusar_outbox ON outbox"))
            conexao.execute(text("DROP FUNCTION recusar_outbox()"))


def _cliente(
    session_factory: sessionmaker[Session], placa: str | None = None
) -> tuple[UUID, UUID]:
    with session_factory() as sess:
        cliente = criar_cliente_com_veiculo(sess, placa=placa)
        sess.commit()
        return cliente.id, cliente.veiculos[0].id


def _abrir(
    session_factory: sessionmaker[Session], cliente_id: UUID, veiculo_id: UUID
) -> UUID:
    with session_factory() as sess:
        resultado = obter_abrir_ordem(sess).executar(
            AbrirOrdemDTO(
                cliente_id=cliente_id,
                veiculo_id=veiculo_id,
                descricao_problema="Freio rangendo",
                ator=_ATOR,
            )
        )
        return resultado.id


def _contar(engine: Engine, tabela: str) -> int:
    with engine.connect() as conexao:
        total: int = conexao.execute(
            select(func.count()).select_from(table(tabela))
        ).scalar_one()
    return total


def test_abertura_grava_os_saga_e_comando_juntos(
    engine: Engine,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
) -> None:
    cliente_id, veiculo_id = _cliente(session_factory, placa="ABC1D23")

    with rastreador.tracer.start_as_current_span("POST /api/v1/ordens-de-servico"):
        ordem_id = _abrir(session_factory, cliente_id, veiculo_id)

    (post,) = rastreador.spans()
    with engine.connect() as conexao:
        saga = conexao.execute(
            text(
                "SELECT etapa, passos, comando_em_voo, prazo_resposta_em, "
                "traceparent, versao FROM sagas WHERE ordem_id = :id"
            ),
            {"id": ordem_id},
        ).one()
        outbox = conexao.execute(
            text("SELECT mensagem_id, envelope, traceparent FROM outbox")
        ).one()
        status = conexao.execute(
            text("SELECT status FROM ordens_de_servico WHERE id = :id"),
            {"id": ordem_id},
        ).scalar_one()

    assert status == "recebida"
    assert (saga.etapa, saga.comando_em_voo, saga.prazo_resposta_em, saga.versao) == (
        "aguardando_diagnostico",
        None,
        None,
        1,
    )
    (passo,) = saga.passos
    assert passo["comando_id"] == str(outbox.mensagem_id)
    assert passo["ator"] == _ATOR
    envelope = outbox.envelope
    catalogo().validar(envelope)
    assert envelope["tipo"] == "SolicitarDiagnostico"
    assert envelope["correlation_id"] == str(ordem_id)
    assert envelope["causation_id"] is None
    assert envelope["dados"]["veiculo"] == {
        "placa": "ABC1D23",
        "marca": "Fiat",
        "modelo": "Uno",
        "ano": 2020,
    }
    assert envelope["dados"]["descricao_problema"] == "Freio rangendo"
    # A abertura e a raiz do trace da saga: comando e saga levam o contexto do POST.
    assert outbox.traceparent == saga.traceparent == traceparent(post)


def test_insert_da_outbox_que_falha_desfaz_os_e_saga(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    cliente_id, veiculo_id = _cliente(session_factory)

    with outbox_recusando_insert(engine), pytest.raises(DBAPIError):
        _abrir(session_factory, cliente_id, veiculo_id)

    assert [_contar(engine, t) for t in ("ordens_de_servico", "sagas", "outbox")] == [
        0,
        0,
        0,
    ]
    # Sem a falha, a mesma abertura passa: o trigger era a unica causa.
    _abrir(session_factory, cliente_id, veiculo_id)
    assert _contar(engine, "sagas") == 1


def test_veiculo_de_outro_cliente_nao_abre_nada(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    cliente_id, _ = _cliente(session_factory)
    _, veiculo_alheio = _cliente(session_factory)

    for veiculo_id in (veiculo_alheio, uuid4()):
        with pytest.raises(VeiculoNaoEncontradoException):
            _abrir(session_factory, cliente_id, veiculo_id)

    assert [_contar(engine, t) for t in ("ordens_de_servico", "sagas", "outbox")] == [
        0,
        0,
        0,
    ]
