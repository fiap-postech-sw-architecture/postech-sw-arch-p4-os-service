"""Composition root do contexto OS: liga casos de uso a infraestrutura.

Unico modulo de ``interfaces`` que importa ``infraestrutura``. Cada factory
recebe a ``Session`` da requisicao; repositorio, UoW e adapters compartilham
essa mesma session (mesma transacao).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.aplicacao.use_cases import (
    AbrirOrdem,
    CancelarOrdem,
    ConsultarAcompanhamento,
    ListarOrdens,
    ObterOrdem,
    RegistrarEntrega,
)
from src.ordem_servico.infraestrutura.adapters import ClienteSQLAlchemyAdapter
from src.ordem_servico.infraestrutura.consultas import ConsultaAcompanhamentoSQLAlchemy
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository


def _repo(session: Session) -> OrdemDeServicoRepository:
    return OrdemDeServicoSQLAlchemyRepository(session=session)


def _uow(session: Session) -> UnitOfWork:
    return SQLAlchemyUnitOfWork(session_factory=lambda: session)


def obter_abrir_ordem(session: Session) -> AbrirOrdem:
    return AbrirOrdem(
        repo=_repo(session),
        uow=_uow(session),
        cliente_port=ClienteSQLAlchemyAdapter(session=session),
    )


def obter_listar_ordens(session: Session) -> ListarOrdens:
    return ListarOrdens(repo=_repo(session))


def obter_obter_ordem(session: Session) -> ObterOrdem:
    return ObterOrdem(repo=_repo(session))


def obter_cancelar_ordem(session: Session) -> CancelarOrdem:
    return CancelarOrdem(repo=_repo(session), uow=_uow(session))


def obter_registrar_entrega(session: Session) -> RegistrarEntrega:
    return RegistrarEntrega(repo=_repo(session), uow=_uow(session))


def obter_consultar_acompanhamento(session: Session) -> ConsultarAcompanhamento:
    return ConsultarAcompanhamento(
        consulta=ConsultaAcompanhamentoSQLAlchemy(session=session)
    )
