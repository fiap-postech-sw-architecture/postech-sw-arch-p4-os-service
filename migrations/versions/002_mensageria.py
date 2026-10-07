"""mensageria: outbox no envelope do contrato e mensagens_processadas da RFC

Revision ID: 002
Revises: 001
Create Date: 2026-10-06

- ``outbox`` passa a guardar comandos no envelope da RFC-004 secao 5.2, com
  exchange, routing key e o contexto W3C (``traceparent``, ``tracestate``) de
  quem gravou; a ordem por OS do relay usa ``correlation_id``. Os eventos
  internos da OS que a 001 gravava (``OrdemAbertaEvent``,
  ``StatusDaOrdemAlteradoEvent``) nunca tiveram relay nem consumidor e deixam
  de ir para a outbox: o OS so publica comandos. A tabela e recriada, sem
  versao anterior implantada que dependa dela.
- ``mensagens_processadas`` fica com as colunas do ER da RFC-004 secao 7.2
  (``mensagem_id``, ``processada_em``).
- Indices das limpezas: entregues (``status``, ``entregue_em``) e processadas
  (``processada_em``).

O downgrade recria a ``outbox`` antiga vazia: os comandos ainda nao entregues
se perdem. Vale enquanto nada implantado grava comandos; da saga em diante, as
migracoes da outbox so acrescentam.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | None = None
depends_on: str | None = None

_AGORA = sa.text("now()")


def _tabela_outbox(*itens: sa.SchemaItem) -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        *itens,
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pendente"
        ),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "proxima_tentativa_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_AGORA,
        ),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_AGORA,
        ),
        sa.Column("entregue_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultimo_erro", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_claim", "outbox", ["status", "proxima_tentativa_em"])


def upgrade() -> None:
    op.drop_index("ix_outbox_agregado_ordering", table_name="outbox")
    op.drop_index("ix_outbox_claim", table_name="outbox")
    op.drop_table("outbox")
    _tabela_outbox(
        sa.Column("mensagem_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("exchange", sa.String(length=64), nullable=False),
        sa.Column("routing_key", sa.String(length=255), nullable=False),
        sa.Column("envelope", postgresql.JSONB(), nullable=False),
        sa.Column("traceparent", sa.String(length=128), nullable=True),
        sa.Column("tracestate", sa.String(length=512), nullable=True),
        sa.UniqueConstraint("mensagem_id"),
    )
    op.create_index(
        "ix_outbox_correlation_ordering", "outbox", ["correlation_id", "id", "status"]
    )
    op.create_index("ix_outbox_entregues", "outbox", ["status", "entregue_em"])

    op.alter_column("mensagens_processadas", "id", new_column_name="mensagem_id")
    op.drop_column("mensagens_processadas", "tipo")
    op.create_index(
        "ix_mensagens_processadas_processada_em",
        "mensagens_processadas",
        ["processada_em"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mensagens_processadas_processada_em", table_name="mensagens_processadas"
    )
    op.add_column(
        "mensagens_processadas",
        sa.Column("tipo", sa.String(length=100), nullable=False, server_default=""),
    )
    op.alter_column("mensagens_processadas", "tipo", server_default=None)
    op.alter_column("mensagens_processadas", "mensagem_id", new_column_name="id")

    op.drop_index("ix_outbox_entregues", table_name="outbox")
    op.drop_index("ix_outbox_correlation_ordering", table_name="outbox")
    op.drop_index("ix_outbox_claim", table_name="outbox")
    op.drop_table("outbox")
    _tabela_outbox(
        sa.Column("agregado_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_outbox_agregado_ordering", "outbox", ["agregado_id", "id", "status"]
    )
