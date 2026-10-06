"""schema inicial do OS Service (fase 4)

Revision ID: 001
Revises:
Create Date: 2026-10-06

Base limpa do servico (recorte do p3 @ 08dcffe, sem a cadeia 001-008 dele):

- clientes, veiculos, consentimentos (Cliente+Veiculo, LGPD);
- usuarios, tokens_revogados (autenticacao);
- ordens_de_servico sem itens, com resumo do orcamento e do pagamento,
  motivo de cancelamento e ``versao`` (lock otimista);
- historico_status_ordem: uma linha por mudanca de status;
- outbox (eventos de integracao) e mensagens_processadas (consumidor
  idempotente).

Status e origem sao VARCHAR com os valores do enum validados pela aplicacao
(sem tipo nativo nem CHECK: status novo nao exige migracao de tipo).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_AGORA = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "clientes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.String(length=255), nullable=False),
        sa.Column("documento", sa.String(length=255), nullable=False),
        sa.Column("documento_hash", sa.String(length=64), nullable=False),
        sa.Column("tipo_documento", sa.String(length=4), nullable=False),
        sa.Column("contato", sa.String(length=255), nullable=False),
        sa.Column("ativo", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("documento_hash"),
    )
    op.create_table(
        "veiculos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("placa", sa.String(length=64), nullable=False),
        sa.Column("marca", sa.String(length=100), nullable=False),
        sa.Column("modelo", sa.String(length=100), nullable=False),
        sa.Column("ano", sa.Integer(), nullable=False),
        sa.Column("cliente_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("placa"),
    )
    op.create_index("ix_veiculos_cliente_id", "veiculos", ["cliente_id"])
    op.create_table(
        "consentimentos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cliente_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=50), nullable=False),
        sa.Column("concedido_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revogado_em", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "usuarios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("senha_hash", sa.String(length=255), nullable=False),
        sa.Column("papel", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "tokens_revogados",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("jti", sa.String(length=255), nullable=False),
        sa.Column("revogado_em", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tokens_revogados_jti", "tokens_revogados", ["jti"], unique=True)
    op.create_table(
        "ordens_de_servico",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cliente_id", sa.Uuid(), nullable=False),
        sa.Column("veiculo_id", sa.Uuid(), nullable=False),
        sa.Column("descricao_problema", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("orcamento_id", sa.Uuid(), nullable=True),
        sa.Column("orcamento_total", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("orcamento_moeda", sa.String(length=3), nullable=True),
        sa.Column("orcamento_link_decisao", sa.String(length=2048), nullable=True),
        sa.Column("orcamento_valido_ate", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pagamento_id", sa.Uuid(), nullable=True),
        sa.Column("pagamento_status", sa.String(length=30), nullable=True),
        sa.Column("pagamento_valor", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("pagamento_moeda", sa.String(length=3), nullable=True),
        sa.Column("pagamento_checkout_url", sa.String(length=2048), nullable=True),
        sa.Column("pagamento_expira_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motivo_cancelamento", sa.String(length=500), nullable=True),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"]),
        sa.ForeignKeyConstraint(["veiculo_id"], ["veiculos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ordens_de_servico_cliente_status",
        "ordens_de_servico",
        ["cliente_id", "status"],
    )
    op.create_index(
        "ix_ordens_de_servico_veiculo_status",
        "ordens_de_servico",
        ["veiculo_id", "status"],
    )
    op.create_table(
        "historico_status_ordem",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ordem_id", sa.Uuid(), nullable=False),
        sa.Column("sequencia", sa.Integer(), nullable=False),
        sa.Column("de", sa.String(length=30), nullable=True),
        sa.Column("para", sa.String(length=30), nullable=False),
        sa.Column("origem", sa.String(length=30), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=True),
        sa.Column("ocorrido_em", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ordem_id"], ["ordens_de_servico.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ordem_id", "sequencia", name="uq_historico_status_ordem_sequencia"
        ),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agregado_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
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
    op.create_index(
        "ix_outbox_agregado_ordering", "outbox", ["agregado_id", "id", "status"]
    )
    op.create_table(
        "mensagens_processadas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=100), nullable=False),
        sa.Column(
            "processada_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_AGORA,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("mensagens_processadas")
    op.drop_index("ix_outbox_agregado_ordering", table_name="outbox")
    op.drop_index("ix_outbox_claim", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("historico_status_ordem")
    op.drop_index("ix_ordens_de_servico_veiculo_status", table_name="ordens_de_servico")
    op.drop_index("ix_ordens_de_servico_cliente_status", table_name="ordens_de_servico")
    op.drop_table("ordens_de_servico")
    op.drop_index("ix_tokens_revogados_jti", table_name="tokens_revogados")
    op.drop_table("tokens_revogados")
    op.drop_table("usuarios")
    op.drop_table("consentimentos")
    op.drop_index("ix_veiculos_cliente_id", table_name="veiculos")
    op.drop_table("veiculos")
    op.drop_table("clientes")
