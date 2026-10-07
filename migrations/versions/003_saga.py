"""saga: instancia da saga de atendimento e o ator do historico da OS

Revision ID: 003
Revises: 002
Create Date: 2026-10-07

So expansao (nada e removido nem reescrito):

- ``historico_status_ordem.ator``: quem provocou a mudanca, o ``sub`` do JWT
  ou o processo (RFC-004 secao 7.2); nulo nas linhas anteriores.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "historico_status_ordem",
        sa.Column("ator", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("historico_status_ordem", "ator")
