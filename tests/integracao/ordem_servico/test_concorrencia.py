"""Lock otimista da OS (``versao``): escrita concorrente vira conflito, nunca
lost update (contramedida *reread value*, RFC-004 secao 4.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    TransicaoStatusInvalidaException,
)
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
    SagaSQLAlchemyRepository,
)
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_cancelar_ordem,
    obter_registrar_entrega,
)
from tests.fabricas import FLUXO, aplicar_fato
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    outbox_recusando_no_commit,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker


def _ordem_commitada(
    session_factory: sessionmaker[Session], ate: StatusOrdem = StatusOrdem.RECEBIDA
) -> UUID:
    """OS sem saga, levada a ``ate`` pelos fatos de dominio."""
    with session_factory() as sess:
        cliente = criar_cliente_com_veiculo(sess)
        ordem = OrdemDeServico.abrir(
            cliente_id=cliente.id,
            veiculo_id=cliente.veiculos[0].id,
            descricao_problema="Direcao pesada",
            ator="atendente-teste",
        )
        for proximo in FLUXO[1 : FLUXO.index(ate) + 1]:
            aplicar_fato(ordem, proximo)
        OrdemDeServicoSQLAlchemyRepository(sess).salvar(ordem)
        sess.commit()
        return ordem.id


def test_segunda_escrita_sobre_versao_lida_vira_conflito(
    session_factory: sessionmaker[Session],
) -> None:
    ordem_id = _ordem_commitada(session_factory)

    with session_factory() as sessao_a, session_factory() as sessao_b:
        repo_a = OrdemDeServicoSQLAlchemyRepository(sessao_a)
        repo_b = OrdemDeServicoSQLAlchemyRepository(sessao_b)
        # As duas leem a versao 1 antes de qualquer escrita.
        ordem_a = repo_a.obter_por_id(ordem_id)
        ordem_b = repo_b.obter_por_id(ordem_id)
        assert ordem_a is not None
        assert ordem_b is not None
        assert ordem_a.versao == ordem_b.versao == 1

        ordem_a.cancelar(
            "cliente desistiu", OrigemMudanca.ATENDIMENTO, ator="atendente-teste"
        )
        repo_a.salvar(ordem_a)
        sessao_a.commit()
        assert ordem_a.versao == 2

        # B decidiu sobre um estado que nao existe mais (RECEBIDA).
        ordem_b.registrar_diagnostico_iniciado(ator="consumidor")
        with pytest.raises(ConflitoDeConcorrenciaException) as exc:
            repo_b.salvar(ordem_b)
        assert str(ordem_id) in exc.value.mensagem
        sessao_b.rollback()

    with session_factory() as sess:
        final = OrdemDeServicoSQLAlchemyRepository(sess).obter_por_id(ordem_id)
        assert final is not None
        assert final.status is StatusOrdem.CANCELADA
        assert final.versao == 2
        assert [m.para for m in final.historico] == [
            StatusOrdem.RECEBIDA,
            StatusOrdem.CANCELADA,
        ]


def test_releitura_apos_conflito_decide_sobre_o_estado_novo(
    session_factory: sessionmaker[Session],
) -> None:
    ordem_id = _ordem_commitada(session_factory)
    with session_factory() as sessao_a, session_factory() as sessao_b:
        repo_a = OrdemDeServicoSQLAlchemyRepository(sessao_a)
        repo_b = OrdemDeServicoSQLAlchemyRepository(sessao_b)
        ordem_a = repo_a.obter_por_id(ordem_id)
        ordem_b = repo_b.obter_por_id(ordem_id)
        assert ordem_a is not None
        assert ordem_b is not None
        ordem_a.registrar_diagnostico_iniciado(ator="consumidor")
        repo_a.salvar(ordem_a)
        sessao_a.commit()

        ordem_b.registrar_diagnostico_iniciado(ator="consumidor")
        with pytest.raises(ConflitoDeConcorrenciaException):
            repo_b.salvar(ordem_b)
        sessao_b.rollback()

    # Reread value: relida, a OS ja esta em EM_DIAGNOSTICO e a versao e 2.
    with session_factory() as sess:
        relida = OrdemDeServicoSQLAlchemyRepository(sess).obter_por_id(ordem_id)
        assert relida is not None
        assert relida.status is StatusOrdem.EM_DIAGNOSTICO
        assert relida.versao == 2
        relida.cancelar("desistiu", OrigemMudanca.ATENDIMENTO, ator="atendente-teste")
        OrdemDeServicoSQLAlchemyRepository(sess).salvar(relida)
        sess.commit()
        assert relida.versao == 3


def test_cancelamentos_concorrentes_pela_uow_real_um_vence_e_o_outro_rele(
    session_factory: sessionmaker[Session],
) -> None:
    # Mesmo wiring da API (obter_cancelar_ordem): repositorios e UoW reais na
    # session da request. B le a OS e a saga antes de A cancelar e decide sobre
    # elas; o UPDATE condicional na versao barra B, o rollback desfaz a tentativa
    # dele e a releitura (*reread value*, RFC-004 secao 4.5) ja ve a compensacao
    # de A: o pedido de B responde com a etapa, sem texto, comando nem historico.
    ordem_id = _ordem_aberta_com_saga(session_factory)
    with session_factory() as sessao_a, session_factory() as sessao_b:
        lida_por_b = OrdemDeServicoSQLAlchemyRepository(sessao_b).obter_por_id(ordem_id)
        saga_de_b = SagaSQLAlchemyRepository(sessao_b).obter(ordem_id)
        assert lida_por_b is not None
        assert saga_de_b is not None
        assert (lida_por_b.versao, saga_de_b.versao) == (1, 1)

        obter_cancelar_ordem(sessao_a).executar(
            ordem_id, "cliente desistiu", ator="atendente-teste"
        )
        de_b = obter_cancelar_ordem(sessao_b).executar(
            ordem_id, "outro motivo", ator="atendente-teste"
        )

    assert (de_b.etapa, de_b.motivo_cancelamento) == ("compensando", "cliente desistiu")
    with session_factory() as sess:
        final = OrdemDeServicoSQLAlchemyRepository(sess).obter_por_id(ordem_id)
        saga = SagaSQLAlchemyRepository(sess).obter(ordem_id)
        comandos = sess.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox ORDER BY id")
        ).scalars()
        assert final is not None
        assert saga is not None
        assert final.status is StatusOrdem.RECEBIDA
        assert final.versao == 2
        assert final.motivo_cancelamento == "cliente desistiu"
        assert [(m.para, m.motivo) for m in final.historico] == [
            (StatusOrdem.RECEBIDA, None)
        ]
        assert (saga.etapa, saga.motivo) == (EtapaSaga.COMPENSANDO, "cancelamento")
        assert [p["gatilho"] for p in saga.passos] == [
            "abertura",
            "cancelamento_solicitado",
        ]
        assert list(comandos) == ["SolicitarDiagnostico", "DescartarDiagnostico"]


def test_cancelamento_falho_na_outbox_desfaz_a_os_e_a_saga(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _ordem_aberta_com_saga(session_factory)

    with outbox_recusando_no_commit(engine), session_factory() as sess:
        cancelar = obter_cancelar_ordem(sess)
        with pytest.raises(DBAPIError, match="outbox indisponivel"):
            cancelar.executar(ordem_id, "cliente desistiu", ator="atendente-teste")

    with session_factory() as sess:
        ordem = OrdemDeServicoSQLAlchemyRepository(sess).obter_por_id(ordem_id)
        saga = SagaSQLAlchemyRepository(sess).obter(ordem_id)
        assert ordem is not None
        assert saga is not None
        assert (ordem.motivo_cancelamento, ordem.versao) == (None, 1)
        assert (saga.etapa, saga.versao) == (EtapaSaga.AGUARDANDO_DIAGNOSTICO, 1)
        assert [p["gatilho"] for p in saga.passos] == ["abertura"]


def test_cancelamento_e_entrega_terminam_sem_transacao_aberta(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Le tudo dentro da unidade de trabalho: uma leitura depois do commit
    # abriria transacao nova, ociosa ate a sessao fechar, segurando locks.
    ordem_id = _ordem_aberta_com_saga(session_factory)
    with session_factory() as sess:
        obter_cancelar_ordem(sess).executar(
            ordem_id, "cliente desistiu", ator="atendente-teste"
        )
        assert not sess.in_transaction()
    # O pedido repetido devolve a etapa sem gravar nada.
    with session_factory() as sess:
        obter_cancelar_ordem(sess).executar(ordem_id, "de novo", ator="atendente-teste")
        assert not sess.in_transaction()

    # Depois do pivot, o 409.
    com_saga = _ordem_aberta_com_saga(session_factory)
    with engine.begin() as conexao:
        conexao.execute(
            text("UPDATE sagas SET etapa = 'em_execucao' WHERE ordem_id = :id"),
            {"id": com_saga},
        )
    with session_factory() as sess:
        cancelar = obter_cancelar_ordem(sess)
        with pytest.raises(TransicaoStatusInvalidaException):
            cancelar.executar(com_saga, "cliente desistiu", ator="atendente-teste")
        assert not sess.in_transaction()

    finalizada = _ordem_commitada(session_factory, ate=StatusOrdem.FINALIZADA)
    with session_factory() as sess:
        obter_registrar_entrega(sess).executar(finalizada, ator="atendente-teste")
        assert not sess.in_transaction()


def _ordem_aberta_com_saga(session_factory: sessionmaker[Session]) -> UUID:
    with session_factory() as sess:
        cliente = criar_cliente_com_veiculo(sess)
        sess.commit()
        cliente_id, veiculo_id = cliente.id, cliente.veiculos[0].id
    with session_factory() as sess:
        return (
            obter_abrir_ordem(sess)
            .executar(
                AbrirOrdemDTO(
                    cliente_id=cliente_id,
                    veiculo_id=veiculo_id,
                    descricao_problema="Direcao pesada",
                    ator="atendente-teste",
                )
            )
            .id
        )
