"""Tabelas Core da mensageria e as escritas transacionais delas (ADR-036).

``outbox`` e ``mensagens_processadas`` sao tabelas Core (sem agregado de
dominio mapeado), registradas no ``metadata`` compartilhado para entrarem na
comparacao com a migracao Alembic.

- ``outbox``: cada linha e um comando no envelope do contrato (RFC-004 secao
  5.2), com exchange, routing key e o contexto W3C (``traceparent``,
  ``tracestate``) de quem a gravou, na mesma transacao do efeito. O relay a
  publica no RabbitMQ.
- ``mensagens_processadas``: idempotencia do consumidor (RFC-004 secao 5.4):
  chave = ``id`` da mensagem, gravada na mesma transacao do efeito.

``pg_notify('outbox_novo','')`` acorda o relay; o NOTIFY e transacional e so
chega quando o COMMIT conclui.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from src.compartilhado.infraestrutura.database import metadata
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from sqlalchemy import Connection, CursorResult, TextClause
    from sqlalchemy.orm import Session

CANAL_NOTIFY = "outbox_novo"
# Linhas por DELETE das limpezas, com um commit cada: sem lock longo nem uma
# transacao enorme na primeira limpeza depois de uma parada.
LOTE_DE_LIMPEZA: Final = 1000
# Tamanho que a W3C recomenda suportar no tracestate; maior que isso, o
# contexto segue so pelo traceparent (descartar o tracestate e permitido).
_TRACESTATE_MAXIMO = 512


def _agora() -> datetime:
    return datetime.now(UTC)


outbox_table = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("mensagem_id", Uuid, nullable=False, unique=True),
    Column("correlation_id", Uuid, nullable=False),
    Column("exchange", String(64), nullable=False),
    Column("routing_key", String(255), nullable=False),
    # JSONB no Postgres; a variante sqlite so existe para create_all de teste.
    Column("envelope", JSONB().with_variant(JSON(), "sqlite"), nullable=False),
    Column("traceparent", String(128), nullable=True),
    Column("tracestate", String(_TRACESTATE_MAXIMO), nullable=True),
    Column("status", String(20), nullable=False, default="pendente"),
    Column("tentativas", Integer, nullable=False, default=0),
    # Tempos pelo relogio do banco, o mesmo com que o relay compara: o relogio
    # do processo que grava (adiantado ou atrasado) nao adia a linha.
    Column(
        "proxima_tentativa_em",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "criado_em", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("entregue_em", DateTime(timezone=True), nullable=True),
    Column("ultimo_erro", Text, nullable=True),
)

Index("ix_outbox_claim", outbox_table.c.status, outbox_table.c.proxima_tentativa_em)
# Limpeza das entregues ha mais de 7 dias.
Index("ix_outbox_entregues", outbox_table.c.status, outbox_table.c.entregue_em)

# Claim com ordem por OS (head-of-line): `NOT EXISTS (... WHERE
# p.correlation_id = o.correlation_id AND p.id < o.id AND p.status = 'pendente')`.
Index(
    "ix_outbox_correlation_ordering",
    outbox_table.c.correlation_id,
    outbox_table.c.id,
    outbox_table.c.status,
)

# Consumidor idempotente (ADR-036): o INSERT acontece na mesma transacao do
# efeito, entao a reentrega da mesma mensagem nao repete o efeito.
mensagens_processadas_table = Table(
    "mensagens_processadas",
    metadata,
    Column("mensagem_id", Uuid, primary_key=True),
    Column(
        "processada_em",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
# Limpeza das processadas ha mais de 30 dias.
Index(
    "ix_mensagens_processadas_processada_em",
    mensagens_processadas_table.c.processada_em,
)


def apagar_em_lotes(
    transacao: Callable[[], AbstractContextManager[Connection | Session]],
    sql: TextClause,
    *,
    entre_lotes: Callable[[], None],
) -> int:
    """Roda o DELETE ``sql`` (com ``LIMIT :lote``) ate ele apagar menos que um lote.

    Cada lote e uma transacao de ``transacao()`` (``engine.begin`` ou
    ``sessionmaker.begin``), e ``entre_lotes`` roda entre um e outro: o processo
    atende o heartbeat do broker no meio de uma limpeza longa.

    Returns:
        Quantas linhas foram apagadas.
    """
    total = 0
    while True:
        with transacao() as conexao:
            resultado = cast(
                "CursorResult[Any]", conexao.execute(sql, {"lote": LOTE_DE_LIMPEZA})
            )
        total += resultado.rowcount
        if resultado.rowcount < LOTE_DE_LIMPEZA:
            return total
        entre_lotes()


def gravar_comando(
    session: Session,
    tipo: str,
    dados: Mapping[str, Any],
    *,
    correlation_id: UUID,
    causation_id: UUID | None,
) -> UUID:
    """INSERT do comando na outbox, na transacao da ``session`` (sem commit).

    Monta e valida o envelope pelo contrato e captura o contexto W3C corrente,
    para o relay publicar como filho dele (ADR-043).

    Raises:
        ContratoInvalidoError: comando que o OS nao publica ou ``dados`` fora do
            schema (bug de quem chama).
    """
    contratos = catalogo()
    envelope = contratos.montar_envelope(
        tipo,
        dados,
        correlation_id=correlation_id,
        causation_id=causation_id,
        ocorrido_em=_agora(),
    )
    destino = contratos.destino(tipo)
    contexto = cabecalhos_do_contexto_atual()
    tracestate = contexto.get("tracestate")
    if tracestate is not None and len(tracestate) > _TRACESTATE_MAXIMO:
        tracestate = None
    session.execute(
        outbox_table.insert().values(
            mensagem_id=envelope["id"],
            correlation_id=correlation_id,
            exchange=destino.exchange,
            routing_key=destino.routing_key,
            envelope=envelope,
            traceparent=contexto.get("traceparent"),
            tracestate=tracestate,
        )
    )
    pg_notify_outbox(session)
    return UUID(envelope["id"])


def registrar_processada(session: Session, mensagem_id: UUID) -> bool:
    """Grava a mensagem como processada; False se ela ja estava (reentrega).

    ``ON CONFLICT DO NOTHING``: duas entregas concorrentes da mesma mensagem
    nao estouram a PK; a segunda espera a primeira e devolve False.
    """
    inserida = session.execute(
        insert(mensagens_processadas_table)
        .values(mensagem_id=mensagem_id)
        .on_conflict_do_nothing(index_elements=["mensagem_id"])
        .returning(mensagens_processadas_table.c.mensagem_id)
    ).first()
    return inserida is not None


def pg_notify_outbox(session: Session) -> None:
    """Emite ``pg_notify('outbox_novo','')`` na transacao corrente (so Postgres)."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_notify(:canal, '')"), {"canal": CANAL_NOTIFY})
