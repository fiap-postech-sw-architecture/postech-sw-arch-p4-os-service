"""Tabelas Core da Transactional Outbox + helpers transacionais.

``outbox`` e ``mensagens_processadas`` sao tabelas Core (sem agregado de
dominio mapeado), registradas no ``metadata`` compartilhado para entrarem no
``create_all`` dos testes e na comparacao com a migracao Alembic.

- ``outbox``: a ``UnitOfWork`` grava os ``IntegrationEvent`` na mesma
  transacao do estado; o relay para o RabbitMQ le daqui.
- ``mensagens_processadas``: idempotencia do consumidor (brief secao 4):
  chave = ``id`` da mensagem, gravada na mesma transacao do efeito.

``pg_notify_outbox`` emite ``pg_notify('outbox_novo','')`` — NOTIFY e
transacional, entao so chega ao relay quando o COMMIT concluir. No-op em
backend nao-Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from src.compartilhado.infraestrutura.database import metadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from src.compartilhado.aplicacao.outbox import OutboxRegistro

CANAL_NOTIFY = "outbox_novo"

outbox_table = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("agregado_id", Uuid, nullable=False),
    Column("tipo", String(255), nullable=False),
    # JSONB no Postgres; a variante sqlite so existe para create_all de teste.
    Column("payload", JSONB().with_variant(JSON(), "sqlite"), nullable=False),
    Column("status", String(20), nullable=False, default="pendente"),
    Column("tentativas", Integer, nullable=False, default=0),
    Column(
        "proxima_tentativa_em",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    ),
    Column(
        "criado_em",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    ),
    Column("entregue_em", DateTime(timezone=True), nullable=True),
    Column("ultimo_erro", Text, nullable=True),
)

Index(
    "ix_outbox_claim",
    outbox_table.c.status,
    outbox_table.c.proxima_tentativa_em,
)

# Suporta o claim com ordem por agregado do relay (head-of-line):
# `NOT EXISTS (... WHERE p.agregado_id = o.agregado_id AND p.id < o.id ...)`.
Index(
    "ix_outbox_agregado_ordering",
    outbox_table.c.agregado_id,
    outbox_table.c.id,
    outbox_table.c.status,
)

# Consumidor idempotente (brief secao 4): o ``id`` da mensagem e a chave; o
# INSERT acontece na mesma transacao do efeito, entao reentrega da mesma
# mensagem viola a PK e o efeito nao se repete.
mensagens_processadas_table = Table(
    "mensagens_processadas",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("tipo", String(100), nullable=False),
    Column(
        "processada_em",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    ),
)


def inserir_na_outbox(session: Session, registros: Sequence[OutboxRegistro]) -> None:
    """INSERT dos registros na ``outbox`` usando a transacao da session.

    ``payload`` (``dict`` JSON-serializavel produzido por
    ``serializar_integration_event``) e passado cru para a coluna JSONB; o
    driver psycopg2 + SQLAlchemy fazem a adaptacao para ``jsonb``. Nao
    commita — o caller (UoW) controla a fronteira transacional.
    """
    if not registros:
        return
    session.execute(
        outbox_table.insert(),
        [
            {
                "agregado_id": r.agregado_id,
                "tipo": r.tipo,
                "payload": r.payload,
            }
            for r in registros
        ],
    )


def pg_notify_outbox(session: Session) -> None:
    """Emite ``pg_notify('outbox_novo','')`` na transacao corrente (so Postgres).

    NOTIFY e transacional: a mensagem so e entregue aos ``LISTEN``ers
    quando esta transacao comita. No-op em backend nao-Postgres.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_notify(:canal, '')"), {"canal": CANAL_NOTIFY})
