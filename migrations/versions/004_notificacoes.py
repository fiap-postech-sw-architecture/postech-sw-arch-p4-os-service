"""notificacoes: o pedido de e-mail ao cliente, gravado junto da transicao da OS

Revision ID: 004
Revises: 003
Create Date: 2026-10-13

So expansao (nada e removido nem reescrito):

- ``notificacoes``: uma linha por e-mail a enviar (``ordem_id``, ``modelo``,
  ``status_da_ordem`` e o ``motivo`` com o codigo da compensacao), sem dado
  pessoal nem token (RFC-004 secao 7.2). O destinatario, o nome e os links saem
  da OS e do cadastro do cliente na entrega, pelo processo ``notificador``. A
  ``outbox`` nao muda: continua so com comandos AMQP, e um relay da versao
  anterior (rolling update, ``rollout undo``) nunca ve linha de e-mail.
- ``ix_notificacoes_claim`` e ``ix_notificacoes_ordem``: o claim com lease das
  linhas pendentes e a ordem por OS, como os da outbox.

A chave estrangeira pede lock forte em ``ordens_de_servico``, que a API escreve:
com ``lock_timeout`` de 5 s, uma escrita longa faz a migracao falhar (o Job
tenta de novo) em vez de enfileirar as escritas da app atras dela.

Rollback da imagem nao roda o ``downgrade``: o schema 004 so acrescenta e serve
ao codigo anterior, que ignora a tabela. O ``downgrade`` apaga os e-mails ainda
nao enviados: so para desfazer a migracao num banco descartavel.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | None = None
depends_on: str | None = None

_AGORA = sa.text("now()")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "notificacoes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ordem_id", sa.Uuid(), nullable=False),
        sa.Column("modelo", sa.String(length=20), nullable=False),
        sa.Column("status_da_ordem", sa.String(length=30), nullable=False),
        sa.Column("motivo", sa.String(length=30), nullable=True),
        sa.Column(
            "criada_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_AGORA,
        ),
        sa.Column(
            "situacao", sa.String(length=20), nullable=False, server_default="pendente"
        ),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "proxima_tentativa_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_AGORA,
        ),
        sa.Column("entregue_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultimo_erro", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["ordem_id"], ["ordens_de_servico.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notificacoes_claim", "notificacoes", ["situacao", "proxima_tentativa_em"]
    )
    op.create_index(
        "ix_notificacoes_ordem", "notificacoes", ["ordem_id", "id", "situacao"]
    )


def downgrade() -> None:
    op.drop_index("ix_notificacoes_ordem", table_name="notificacoes")
    op.drop_index("ix_notificacoes_claim", table_name="notificacoes")
    op.drop_table("notificacoes")
