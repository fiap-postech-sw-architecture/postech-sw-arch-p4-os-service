"""A transacao do processo ``prazos`` sobre o PostgreSQL (RFC-004 secao 4.6).

Uma ``UnitOfWork`` com o repositorio da saga montado na sessao dela: a saga
reivindicada (``FOR UPDATE SKIP LOCKED``), o fato gravado e o comando na outbox
entram no mesmo commit, ou nenhum entra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.infraestrutura.repository import SagaSQLAlchemyRepository

if TYPE_CHECKING:
    from datetime import datetime

    from src.ordem_servico.aplicacao.ports import SagaRepository


class TransacaoDosPrazosSQLAlchemy(SQLAlchemyUnitOfWork):
    """``TransacaoDosPrazos``: ``with`` abre a sessao e o fim sem commit a desfaz."""

    @property
    def sagas(self) -> SagaRepository:
        return SagaSQLAlchemyRepository(self.session)

    def agora_do_banco(self) -> datetime:
        """O ``clock_timestamp()`` do banco, o mesmo que grava ``entregue_em``."""
        banco: datetime = self.session.execute(
            text("SELECT clock_timestamp()")
        ).scalar_one()
        return banco
