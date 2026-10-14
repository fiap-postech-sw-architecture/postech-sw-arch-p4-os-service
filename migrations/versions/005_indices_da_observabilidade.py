"""observabilidade: indices da idade da outbox e da janela do historico

Revision ID: 005
Revises: 004
Create Date: 2026-10-14

So expansao (nada e removido nem reescrito):

- ``ix_outbox_status_criado_em`` (``status``, ``criado_em``): a idade da linha
  pendente mais antiga (``pytstop_outbox_mais_antiga_segundos``, lida a cada
  raspagem do coletor da API, de cada replica) passa a ser a leitura de uma linha
  so. Sem ele a consulta le todas as pendentes, e a outbox cresce justo quando o
  relay ou o broker param.
- ``ix_historico_status_ordem_ocorrido_em`` (``ocorrido_em``): a permanencia
  media por status (``GET /api/v1/ordens-de-servico/metricas`` e os gauges do
  coletor) so olha as entradas dos ultimos 30 dias; sem o indice ela varre o
  historico inteiro a cada consulta.

``CREATE INDEX`` tranca as escritas da tabela enquanto o indice nasce: com o
``lock_timeout`` de 5 s, uma escrita longa faz a migracao falhar (o Job tenta de
novo) em vez de enfileirar as escritas da app atras dela.

Rollback da imagem nao roda o ``downgrade``: o schema 005 so acrescenta e serve
ao codigo anterior, que ignora os indices.
"""

from __future__ import annotations

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index("ix_outbox_status_criado_em", "outbox", ["status", "criado_em"])
    op.create_index(
        "ix_historico_status_ordem_ocorrido_em",
        "historico_status_ordem",
        ["ocorrido_em"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_historico_status_ordem_ocorrido_em", table_name="historico_status_ordem"
    )
    op.drop_index("ix_outbox_status_criado_em", table_name="outbox")
