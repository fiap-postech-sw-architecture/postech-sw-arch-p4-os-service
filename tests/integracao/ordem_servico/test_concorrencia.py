"""Lock otimista da OS (``versao``): escrita concorrente vira conflito, nunca
lost update (contramedida *reread value*, RFC-004 secao 4.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    TransicaoStatusInvalidaException,
)
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_cancelar_ordem,
    obter_registrar_entrega,
)
from tests.fabricas import FLUXO, aplicar_fato
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
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


def test_cancelamentos_concorrentes_pela_uow_real_um_vence_e_o_historico_so_tem_o_dele(
    session_factory: sessionmaker[Session],
) -> None:
    # Mesmo wiring da API (obter_cancelar_ordem): repositorio e UoW reais na
    # session da request. B le a versao 1 antes de A cancelar e decide sobre
    # ela; o UPDATE condicional na versao barra B e a transicao dele nao entra
    # no historico (rollback da transacao inteira).
    ordem_id = _ordem_commitada(session_factory)
    with session_factory() as sessao_a, session_factory() as sessao_b:
        lida_por_b = OrdemDeServicoSQLAlchemyRepository(sessao_b).obter_por_id(ordem_id)
        assert lida_por_b is not None
        assert lida_por_b.versao == 1

        obter_cancelar_ordem(sessao_a).executar(
            ordem_id, "cliente desistiu", ator="atendente-teste"
        )
        with pytest.raises(ConflitoDeConcorrenciaException):
            obter_cancelar_ordem(sessao_b).executar(
                ordem_id, "outro motivo", ator="atendente-teste"
            )

    with session_factory() as sess:
        final = OrdemDeServicoSQLAlchemyRepository(sess).obter_por_id(ordem_id)
        assert final is not None
        assert final.status is StatusOrdem.CANCELADA
        assert final.versao == 2
        assert final.motivo_cancelamento == "cliente desistiu"
        assert [(m.para, m.motivo) for m in final.historico] == [
            (StatusOrdem.RECEBIDA, None),
            (StatusOrdem.CANCELADA, "cliente desistiu"),
        ]


def test_cancelamento_e_entrega_terminam_sem_transacao_aberta(
    session_factory: sessionmaker[Session],
) -> None:
    # Le tudo dentro da unidade de trabalho: uma leitura depois do commit
    # abriria transacao nova, ociosa ate a sessao fechar, segurando locks.
    sem_saga = _ordem_commitada(session_factory)
    with session_factory() as sess:
        obter_cancelar_ordem(sess).executar(
            sem_saga, "cliente desistiu", ator="atendente-teste"
        )
        assert not sess.in_transaction()

    com_saga = _ordem_aberta_com_saga(session_factory)
    with session_factory() as sess:
        with pytest.raises(TransicaoStatusInvalidaException):
            obter_cancelar_ordem(sess).executar(
                com_saga, "cliente desistiu", ator="atendente-teste"
            )
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
