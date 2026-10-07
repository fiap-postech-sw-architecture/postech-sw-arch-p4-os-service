"""saga: instancia da saga de atendimento e o ator do historico da OS

Revision ID: 003
Revises: 002
Create Date: 2026-10-07

So expansao (nada e removido nem reescrito):

- ``sagas``: uma instancia por OS (``ordem_id``, PK e FK), com etapa, passos,
  passos concluidos, comando em voo, plano de compensacao, itens do
  diagnostico, prazo tecnico, ``traceparent`` e ``versao`` do lock otimista
  (RFC-004 secao 7.2). So codigos: texto livre fica na OS.
- ``ix_sagas_prazo`` (parcial, so com prazo) acha as candidatas a prazo
  vencido; ``ix_sagas_ativas`` (parcial, etapas nao finais) serve os gauges.
- ``historico_status_ordem.ator``: quem provocou a mudanca, o ``sub`` do JWT
  ou o processo; nulo nas linhas anteriores.

O ``ALTER TABLE`` e a chave estrangeira pedem lock forte em tabelas que a API
le: com ``lock_timeout`` de 5 s, uma leitura longa faz a migracao falhar (o Job
tenta de novo) em vez de enfileirar as leituras da app atras dela.

Rollback da imagem nao roda o ``downgrade``: o schema 003 so acrescenta e serve
ao codigo anterior. O ``downgrade`` apaga a tabela ``sagas`` (o estado de todas
as sagas) e a coluna ``ator`` (quem mudou cada status): so para desfazer a
migracao num banco descartavel.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "historico_status_ordem",
        sa.Column("ator", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "sagas",
        sa.Column("ordem_id", sa.Uuid(), nullable=False),
        sa.Column("etapa", sa.String(length=30), nullable=False),
        sa.Column("motivo", sa.String(length=30), nullable=True),
        sa.Column("falha", sa.String(length=30), nullable=True),
        sa.Column("passos", postgresql.JSONB(), nullable=False),
        sa.Column("passos_concluidos", postgresql.JSONB(), nullable=False),
        sa.Column("comando_em_voo", postgresql.JSONB(), nullable=True),
        sa.Column("plano_compensacao", postgresql.JSONB(), nullable=False),
        sa.Column("itens", postgresql.JSONB(), nullable=False),
        sa.Column("reenvios", sa.Integer(), nullable=False),
        sa.Column("prazo_resposta_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("traceparent", sa.String(length=55), nullable=True),
        sa.Column("iniciada_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("etapa_desde", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizada_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ordem_id"], ["ordens_de_servico.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("ordem_id"),
    )
    op.create_index(
        "ix_sagas_prazo",
        "sagas",
        ["prazo_resposta_em"],
        postgresql_where=sa.text("prazo_resposta_em IS NOT NULL"),
    )
    op.create_index(
        "ix_sagas_ativas",
        "sagas",
        ["etapa", "etapa_desde"],
        postgresql_where=sa.text("etapa NOT IN ('concluida', 'compensada')"),
    )


def downgrade() -> None:
    op.drop_index("ix_sagas_ativas", table_name="sagas")
    op.drop_index("ix_sagas_prazo", table_name="sagas")
    op.drop_table("sagas")
    op.drop_column("historico_status_ordem", "ator")
