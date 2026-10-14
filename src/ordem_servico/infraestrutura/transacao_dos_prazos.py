"""A transacao do processo ``prazos`` sobre o PostgreSQL (RFC-004 secao 4.6).

Uma ``UnitOfWork`` com os repositorios da OS e da saga montados na sessao dela:
a saga reivindicada (``FOR UPDATE SKIP LOCKED``), o fato gravado e o comando na
outbox entram no mesmo commit, ou nenhum entra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
    SagaSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from src.ordem_servico.aplicacao.ports import SagaRepository
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository


class TransacaoDosPrazosSQLAlchemy(SQLAlchemyUnitOfWork):
    """``TransacaoDosPrazos``: ``with`` abre a sessao e o fim sem commit a desfaz."""

    @property
    def ordens(self) -> OrdemDeServicoRepository:
        return OrdemDeServicoSQLAlchemyRepository(self.session)

    @property
    def sagas(self) -> SagaRepository:
        return SagaSQLAlchemyRepository(self.session)
