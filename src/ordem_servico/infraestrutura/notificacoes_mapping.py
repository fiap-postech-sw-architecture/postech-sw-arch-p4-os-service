"""Tabela ``notificacoes``: o e-mail ao cliente, gravado junto da transicao.

Cada linha pede um e-mail e nao leva dado pessoal nem token: so ``{ordem_id,
modelo, status_da_ordem, motivo}`` (RFC-004 secao 7.2; ADR-036 "E-mail ao
cliente"). O destinatario, o nome e os links saem da OS e do cadastro do
cliente na hora da entrega, pelo processo ``notificador``
(``fila_de_notificacoes``). A linha entra na transacao da transicao da OS (a
API na entrega, o consumidor nos eventos da saga): se a transacao desfaz, o
e-mail nao existe. A ``outbox`` nao muda e continua so com comandos AMQP.

``pg_notify('notificacao_nova','')`` acorda o notificador; o NOTIFY e
transacional e so chega quando o COMMIT conclui.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
    func,
    text,
)

from src.compartilhado.infraestrutura.database import metadata

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.ordem_servico.aplicacao.notificacoes import Notificacao

CANAL_NOTIFICACAO: Final = "notificacao_nova"

notificacoes_table = Table(
    "notificacoes",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "ordem_id",
        Uuid,
        ForeignKey("ordens_de_servico.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("modelo", String(20), nullable=False),
    Column("status_da_ordem", String(30), nullable=False),
    # Codigo da compensacao (``MotivoCompensacao``), nunca o texto do atendente.
    Column("motivo", String(30), nullable=True),
    Column(
        "criada_em", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("situacao", String(20), nullable=False, default="pendente"),
    Column("tentativas", Integer, nullable=False, default=0),
    # Tempos pelo relogio do banco, como na outbox: o lease do claim e o atraso
    # das tentativas comparam com o ``now()`` do mesmo servidor.
    Column(
        "proxima_tentativa_em",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("entregue_em", DateTime(timezone=True), nullable=True),
    # Texto fixo (o nome da excecao): a mensagem de uma falha de SMTP pode
    # trazer o endereco do destinatario.
    Column("ultimo_erro", Text, nullable=True),
)

Index(
    "ix_notificacoes_claim",
    notificacoes_table.c.situacao,
    notificacoes_table.c.proxima_tentativa_em,
)
# Ordem por OS: `NOT EXISTS (... WHERE p.ordem_id = n.ordem_id AND p.id < n.id
# AND p.situacao = 'pendente')`.
Index(
    "ix_notificacoes_ordem",
    notificacoes_table.c.ordem_id,
    notificacoes_table.c.id,
    notificacoes_table.c.situacao,
)


class NotificacoesSQLAlchemy:
    """``PublicadorDeNotificacoes`` sobre a ``Session`` da transacao em curso.

    Nao comita: a linha vai com o commit de quem a grava (o consumidor, com
    ``mensagens_processadas``; a API, com a UoW).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def registrar(self, notificacao: Notificacao) -> None:
        self._session.execute(
            notificacoes_table.insert().values(
                ordem_id=notificacao.ordem_id,
                modelo=notificacao.modelo.value,
                status_da_ordem=notificacao.status.value,
                motivo=notificacao.motivo,
            )
        )
        if self._session.get_bind().dialect.name == "postgresql":
            self._session.execute(
                text("SELECT pg_notify(:canal, '')"), {"canal": CANAL_NOTIFICACAO}
            )
