"""Integracao: fila de OS ordenada por prioridade de status (regra do p3).

Exercita ``OrdemDeServicoSQLAlchemyRepository.listar``/``contar`` contra
Postgres real:

- prioridade por proximidade da conclusao: EM_EXECUCAO > AGUARDANDO_EXECUCAO
  > AGUARDANDO_PAGAMENTO > AGUARDANDO_APROVACAO > EM_DIAGNOSTICO > RECEBIDA;
  no grupo, mais antiga primeiro (``criado_em ASC``) e desempate por ``id``;
- FINALIZADA/ENTREGUE/CANCELADA fora da listagem padrao (filtro de leitura);
- visao completa via ``incluir_encerradas=True`` (encerradas ao final).

As linhas sao inseridas direto na tabela (bypass deliberado do agregado)
para fixar ``status``/``criado_em`` sem percorrer a maquina: o alvo aqui e o
read-side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from src.cliente_veiculo.dominio.cliente import Cliente
from src.cliente_veiculo.dominio.contato import Contato
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.placa import Placa
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.mapping import ordens_de_servico_table
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

pytestmark = pytest.mark.integracao

_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _criar_cliente_com_veiculo(session: Session) -> tuple[UUID, UUID]:
    from src.cliente_veiculo.infraestrutura.repository import (
        ClienteSQLAlchemyRepository,
    )

    cliente = Cliente(
        _nome="Cliente Listagem",
        _documento=CPF(numero="21249722519"),
        _contato=Contato(valor="11999990000"),
    )
    ClienteSQLAlchemyRepository(session=session).salvar(cliente)
    cliente.adicionar_veiculo(
        placa=Placa(valor="LST1234"), marca="Fiat", modelo="Uno", ano=2020
    )
    session.flush()
    return cliente.id, cliente.veiculos[0].id


def _inserir_ordem(
    session: Session,
    *,
    cliente_id: UUID,
    veiculo_id: UUID,
    status: StatusOrdem,
    criado_em: datetime,
    ordem_id: UUID | None = None,
) -> UUID:
    ordem_id = ordem_id or uuid4()
    session.execute(
        ordens_de_servico_table.insert().values(
            id=ordem_id,
            cliente_id=cliente_id,
            veiculo_id=veiculo_id,
            descricao_problema="Revisao",
            status=status,
            versao=1,
            criado_em=criado_em,
            atualizado_em=criado_em,
        )
    )
    return ordem_id


@pytest.fixture
def cenario(session: Session) -> dict[str, UUID]:
    """Uma OS em cada status, com duplicatas para antiguidade.

    Os offsets de ``criado_em`` (minutos sobre ``_BASE``) contradizem a
    ordem cronologica: as encerradas sao as mais antigas e os estados mais
    avancados sao mais novos que os iniciais.
    """
    cliente_id, veiculo_id = _criar_cliente_com_veiculo(session)

    def inserir(status: StatusOrdem, minutos: int) -> UUID:
        return _inserir_ordem(
            session,
            cliente_id=cliente_id,
            veiculo_id=veiculo_id,
            status=status,
            criado_em=_BASE + timedelta(minutes=minutos),
        )

    ids = {
        "cancelada": inserir(StatusOrdem.CANCELADA, 0),
        "entregue": inserir(StatusOrdem.ENTREGUE, 5),
        "finalizada": inserir(StatusOrdem.FINALIZADA, 10),
        "recebida": inserir(StatusOrdem.RECEBIDA, 15),
        "diagnostico": inserir(StatusOrdem.EM_DIAGNOSTICO, 20),
        "aprovacao": inserir(StatusOrdem.AGUARDANDO_APROVACAO, 25),
        "pagamento": inserir(StatusOrdem.AGUARDANDO_PAGAMENTO, 30),
        "agendada": inserir(StatusOrdem.AGUARDANDO_EXECUCAO, 35),
        "execucao_velha": inserir(StatusOrdem.EM_EXECUCAO, 40),
        "execucao_nova": inserir(StatusOrdem.EM_EXECUCAO, 45),
    }
    session.flush()
    return ids


_ORDEM_ATIVAS = (
    "execucao_velha",
    "execucao_nova",
    "agendada",
    "pagamento",
    "aprovacao",
    "diagnostico",
    "recebida",
)
_ORDEM_ENCERRADAS = ("cancelada", "entregue", "finalizada")


class TestListagemOrdenadaPorPrioridade:
    def test_default_ordena_por_prioridade_e_antiguidade(
        self, session: Session, cenario: dict[str, UUID]
    ) -> None:
        """Prioridade de status, criado_em ASC dentro do grupo."""
        repo = OrdemDeServicoSQLAlchemyRepository(session=session)

        resultado = repo.listar(offset=0, limit=50)

        esperado = [cenario[nome] for nome in _ORDEM_ATIVAS]
        assert [ordem.id for ordem in resultado] == esperado

    def test_default_exclui_encerradas(
        self, session: Session, cenario: dict[str, UUID]
    ) -> None:
        """FINALIZADA/ENTREGUE/CANCELADA fora do default."""
        repo = OrdemDeServicoSQLAlchemyRepository(session=session)

        ids_listados = {ordem.id for ordem in repo.listar(offset=0, limit=50)}

        for nome in _ORDEM_ENCERRADAS:
            assert cenario[nome] not in ids_listados

    def test_incluir_encerradas_ao_final(
        self, session: Session, cenario: dict[str, UUID]
    ) -> None:
        """Visao administrativa completa: encerradas ao final da ordenacao.

        As encerradas sao as linhas mais antigas do cenario; ainda assim
        devem aparecer depois de todas as ativas (prioridade domina o
        criado_em), ordenadas entre si por criado_em ASC.
        """
        repo = OrdemDeServicoSQLAlchemyRepository(session=session)

        resultado = repo.listar(offset=0, limit=50, incluir_encerradas=True)

        esperado = [cenario[nome] for nome in (*_ORDEM_ATIVAS, *_ORDEM_ENCERRADAS)]
        assert [ordem.id for ordem in resultado] == esperado

    def test_paginacao_fatiando_a_mesma_ordenacao(
        self, session: Session, cenario: dict[str, UUID]
    ) -> None:
        """A paginacao fatia a mesma ordenacao (deterministica)."""
        repo = OrdemDeServicoSQLAlchemyRepository(session=session)

        pagina = repo.listar(offset=2, limit=3)

        esperado = [cenario[nome] for nome in _ORDEM_ATIVAS[2:5]]
        assert [ordem.id for ordem in pagina] == esperado

    def test_desempate_deterministico_por_id(self, session: Session) -> None:
        """Mesmo status e mesmo criado_em -> ordena por id ASC."""
        cliente_id, veiculo_id = _criar_cliente_com_veiculo(session)
        ids = [
            _inserir_ordem(
                session,
                cliente_id=cliente_id,
                veiculo_id=veiculo_id,
                status=StatusOrdem.RECEBIDA,
                criado_em=_BASE,
            )
            for _ in range(3)
        ]
        session.flush()

        repo = OrdemDeServicoSQLAlchemyRepository(session=session)
        resultado = [ordem.id for ordem in repo.listar(offset=0, limit=10)]

        assert resultado == sorted(ids)

    def test_contar_acompanha_o_filtro_da_listagem(
        self, session: Session, cenario: dict[str, UUID]
    ) -> None:
        """Total para paginacao reflete o mesmo universo filtrado."""
        repo = OrdemDeServicoSQLAlchemyRepository(session=session)

        assert repo.contar() == 7
        assert repo.contar(incluir_encerradas=True) == 10
