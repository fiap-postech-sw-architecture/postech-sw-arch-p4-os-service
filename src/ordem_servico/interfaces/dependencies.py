"""Composition root do contexto OS: liga casos de uso a infraestrutura.

Unico modulo de ``interfaces`` que importa ``infraestrutura``. Cada factory
recebe a ``Session`` da requisicao; repositorio, UoW e adapters compartilham
essa mesma session (mesma transacao).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.observability import tracer_da_api
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.use_cases import (
    AbrirOrdem,
    CancelarOrdem,
    ConsultarAcompanhamento,
    ListarOrdens,
    ObterOrdem,
    ObterSaga,
    RegistrarEntrega,
    RetomarCompensacao,
)
from src.ordem_servico.infraestrutura.adapters import ClienteSQLAlchemyAdapter
from src.ordem_servico.infraestrutura.consultas import (
    ConsultaAcompanhamentoSQLAlchemy,
    ConsultaDaOrdemSQLAlchemy,
)
from src.ordem_servico.infraestrutura.prazo_da_saga import prazo_de_resposta_da_saga
from src.ordem_servico.infraestrutura.rastreamento_da_saga import span_da_saga
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
    SagaSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from datetime import timedelta

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.ordem_servico.aplicacao.ports import SagaRepository
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository


_prazo_de_resposta: timedelta | None = None


def configurar_prazo_de_resposta(prazo: timedelta) -> None:
    """Registra o prazo de resposta da saga lido no startup, uma vez so.

    Sem o startup (os testes montam o app sem o lifespan), vale o do ambiente.
    """
    global _prazo_de_resposta  # noqa: PLW0603  # DI singleton configurado no startup
    _prazo_de_resposta = prazo


def _repo(session: Session) -> OrdemDeServicoRepository:
    return OrdemDeServicoSQLAlchemyRepository(session=session)


def _sagas(session: Session) -> SagaRepository:
    return SagaSQLAlchemyRepository(session=session)


def _uow(session: Session) -> UnitOfWork:
    return SQLAlchemyUnitOfWork(session_factory=lambda: session)


def obter_abrir_ordem(session: Session) -> AbrirOrdem:
    return AbrirOrdem(
        repo=_repo(session),
        uow=_uow(session),
        cliente_port=ClienteSQLAlchemyAdapter(session=session),
        sagas=_sagas(session),
    )


def obter_listar_ordens(session: Session) -> ListarOrdens:
    return ListarOrdens(repo=_repo(session))


def obter_obter_ordem(session: Session) -> ObterOrdem:
    return ObterOrdem(consulta=ConsultaDaOrdemSQLAlchemy(session=session))


def obter_obter_saga(session: Session) -> ObterSaga:
    return ObterSaga(sagas=_sagas(session))


def obter_cancelar_ordem(session: Session) -> CancelarOrdem:
    return CancelarOrdem(
        repo=_repo(session),
        uow=_uow(session),
        sagas=_sagas(session),
        contexto=ContextoDeEnvio(
            _prazo_de_resposta or prazo_de_resposta_da_saga(),
            span=span_da_saga(tracer_da_api()),
        ),
    )


def obter_retomar_compensacao(session: Session) -> RetomarCompensacao:
    return RetomarCompensacao(
        uow=_uow(session),
        sagas=_sagas(session),
        contexto=ContextoDeEnvio(
            _prazo_de_resposta or prazo_de_resposta_da_saga(),
            span=span_da_saga(tracer_da_api()),
        ),
    )


def obter_registrar_entrega(session: Session) -> RegistrarEntrega:
    return RegistrarEntrega(
        repo=_repo(session), uow=_uow(session), sagas=_sagas(session)
    )


def obter_consultar_acompanhamento(session: Session) -> ConsultarAcompanhamento:
    return ConsultarAcompanhamento(
        consulta=ConsultaAcompanhamentoSQLAlchemy(session=session)
    )
