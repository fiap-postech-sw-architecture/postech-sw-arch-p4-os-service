"""Erasure LGPD do texto livre das OS e a trava contra abertura concorrente.

Contra Postgres real e com commit de verdade (``session_factory``): o caso de
uso roda na sua propria session e o estado e conferido por outra. Tres
mutacoes do codigo (UPDATE sem ``WHERE cliente_id``, motivo nulo virando
``ANONIMIZADO`` e historico sem ``ordem_id IN``) quebram estes testes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from src.cliente_veiculo.aplicacao.lgpd_use_cases import ExcluirDadosPessoais
from src.cliente_veiculo.infraestrutura.adapters import (
    OrdemDeServicoSQLAlchemyAdapter,
)
from src.cliente_veiculo.infraestrutura.repository import ClienteSQLAlchemyRepository
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.adapters import ClienteSQLAlchemyAdapter
from src.ordem_servico.infraestrutura.mapping import (
    historico_status_ordem_table,
    ordens_de_servico_table,
)
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.fabricas import FLUXO, aplicar_fato
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

    from src.cliente_veiculo.dominio.cliente import Cliente

_ANONIMIZADO = "ANONIMIZADO"


class _EstadoDaOrdem(NamedTuple):
    descricao: str
    motivo: str | None
    versao: int
    motivos_do_historico: list[str | None]
    atualizado_em: datetime


def _abrir(
    sess: Session,
    cliente: Cliente,
    *,
    cancelar_com: str | None = None,
    ate: StatusOrdem | None = None,
) -> UUID:
    ordem = OrdemDeServico.abrir(
        cliente_id=cliente.id,
        veiculo_id=cliente.veiculos[0].id,
        descricao_problema=f"Relato de {cliente.nome}",
    )
    repo = OrdemDeServicoSQLAlchemyRepository(sess)
    repo.salvar(ordem)
    if cancelar_com is not None:
        ordem.cancelar(cancelar_com, OrigemMudanca.ATENDIMENTO)
        repo.salvar(ordem)
    if ate is not None:
        for proximo in FLUXO[1 : FLUXO.index(ate) + 1]:
            aplicar_fato(ordem, proximo)
            repo.salvar(ordem)
    return ordem.id


def _estado(factory: sessionmaker[Session], ordem_id: UUID) -> _EstadoDaOrdem:
    o = ordens_de_servico_table
    h = historico_status_ordem_table
    with factory() as sess:
        linha = sess.execute(
            select(
                o.c.descricao_problema,
                o.c.motivo_cancelamento,
                o.c.versao,
                o.c.atualizado_em,
            ).where(o.c.id == ordem_id)
        ).one()
        motivos = sess.scalars(
            select(h.c.motivo).where(h.c.ordem_id == ordem_id).order_by(h.c.sequencia)
        ).all()
    return _EstadoDaOrdem(
        linha.descricao_problema,
        linha.motivo_cancelamento,
        linha.versao,
        list(motivos),
        linha.atualizado_em,
    )


def _excluir(factory: sessionmaker[Session], cliente_id: UUID) -> None:
    sess = factory()
    ExcluirDadosPessoais(
        repo=ClienteSQLAlchemyRepository(sess),
        uow=SQLAlchemyUnitOfWork(session_factory=lambda: sess),
        os_port=OrdemDeServicoSQLAlchemyAdapter(sess),
    ).executar(cliente_id)


class TestIsolamentoDoErasure:
    def test_apaga_so_o_texto_do_cliente_e_preserva_os_nulos(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as sess:
            cliente_a = criar_cliente_com_veiculo(sess, nome="Cliente A")
            cliente_b = criar_cliente_com_veiculo(sess, nome="Cliente B")
            cancelada_a = _abrir(sess, cliente_a, cancelar_com="A desistiu")
            entregue_a = _abrir(sess, cliente_a, ate=StatusOrdem.ENTREGUE)
            cancelada_b = _abrir(sess, cliente_b, cancelar_com="B desistiu")
            sess.commit()
            id_a = cliente_a.id
        antes = {
            ordem_id: _estado(session_factory, ordem_id)
            for ordem_id in (cancelada_a, entregue_a, cancelada_b)
        }

        _excluir(session_factory, id_a)

        depois = {
            ordem_id: _estado(session_factory, ordem_id)
            for ordem_id in (cancelada_a, entregue_a, cancelada_b)
        }
        # Cliente B: nada muda, nem a versao.
        assert depois[cancelada_b] == antes[cancelada_b]
        assert depois[cancelada_b].motivos_do_historico == [None, "B desistiu"]
        # Cliente A: o texto sai, o nulo continua nulo, a versao sobe.
        assert depois[cancelada_a].descricao == _ANONIMIZADO
        assert depois[cancelada_a].motivo == _ANONIMIZADO
        assert depois[cancelada_a].motivos_do_historico == [None, _ANONIMIZADO]
        assert depois[entregue_a].descricao == _ANONIMIZADO
        assert depois[entregue_a].motivo is None
        assert set(depois[entregue_a].motivos_do_historico) == {None}
        for ordem_id in (cancelada_a, entregue_a):
            assert depois[ordem_id].versao == antes[ordem_id].versao + 1
            assert depois[ordem_id].atualizado_em > antes[ordem_id].atualizado_em


class TestTravaEntreErasureEAberturaDeOS:
    """O ``FOR UPDATE`` do erasure e o ``FOR SHARE`` da abertura se excluem.

    ``lock_timeout`` curto transforma a espera em erro: prova que a segunda
    transacao bloquearia ate a primeira terminar (sem janela check-then-act).
    """

    @pytest.fixture
    def cliente_id(self, session_factory: sessionmaker[Session]) -> UUID:
        with session_factory() as sess:
            cliente = criar_cliente_com_veiculo(sess)
            sess.commit()
            return cliente.id

    def test_abertura_espera_o_erasure_que_travou_o_cliente(
        self, session_factory: sessionmaker[Session], cliente_id: UUID
    ) -> None:
        with session_factory() as erasure, session_factory() as abertura:
            assert ClienteSQLAlchemyRepository(erasure).bloquear_cliente(cliente_id)
            abertura.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                ClienteSQLAlchemyAdapter(abertura).cliente_existe(cliente_id)

    def test_erasure_espera_a_abertura_que_leu_o_cliente(
        self, session_factory: sessionmaker[Session], cliente_id: UUID
    ) -> None:
        with session_factory() as abertura, session_factory() as erasure:
            assert ClienteSQLAlchemyAdapter(abertura).cliente_existe(cliente_id)
            erasure.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                ClienteSQLAlchemyRepository(erasure).bloquear_cliente(cliente_id)

    def test_cliente_inexistente_nao_trava_nada(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as sess:
            assert ClienteSQLAlchemyRepository(sess).bloquear_cliente(uuid4()) is False
