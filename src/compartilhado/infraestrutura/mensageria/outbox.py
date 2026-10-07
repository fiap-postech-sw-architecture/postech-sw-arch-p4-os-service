"""A outbox vista pelo relay: o SQL de cada passo e a politica de tentativas.

O relay reivindica linhas (claim com ``FOR UPDATE SKIP LOCKED``, em ordem por
OS, e um lease), entrega cada uma na transacao do fencing (o relock ``SKIP
LOCKED`` com status ``pendente``) e marca o desfecho nela: ``entregue``, nova
tentativa com o atraso da tabela ou ``dead``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from uuid import UUID

    from sqlalchemy import Connection, Engine

# Politica do relay do p3: um atraso depois de cada falha da propria mensagem;
# a falha seguinte a ultima da tabela (a quinta) leva a linha a `dead`.
ATRASOS_S: Final[tuple[float, ...]] = (1, 4, 16, 64)
_RETENCAO: Final = timedelta(days=7)

_SQL_CLAIM: Final = text(
    "SELECT o.id, o.mensagem_id, o.correlation_id, o.exchange, o.routing_key, "
    "o.envelope, o.traceparent, o.tracestate, o.tentativas "
    "FROM outbox o "
    "WHERE o.status = 'pendente' AND o.proxima_tentativa_em <= :agora "
    "AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.correlation_id = o.correlation_id "
    "AND p.id < o.id AND p.status = 'pendente') "
    "ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT :limite"
)
_SQL_LEASE: Final = text(
    "UPDATE outbox SET proxima_tentativa_em = :ate WHERE id = ANY(:ids)"
)
_SQL_FENCING: Final = text(
    "SELECT 1 FROM outbox WHERE id = :id AND status = 'pendente' FOR UPDATE SKIP LOCKED"
)
_SQL_ENTREGUE: Final = text(
    "UPDATE outbox SET status = 'entregue', entregue_em = :agora, "
    "ultimo_erro = NULL WHERE id = :id"
)
_SQL_NOVA_TENTATIVA: Final = text(
    "UPDATE outbox SET tentativas = :tentativas, proxima_tentativa_em = :proxima, "
    "ultimo_erro = :erro WHERE id = :id"
)
_SQL_DEAD: Final = text(
    "UPDATE outbox SET status = 'dead', tentativas = :tentativas, "
    "ultimo_erro = :erro WHERE id = :id"
)
_SQL_LIBERAR: Final = text(
    "UPDATE outbox SET proxima_tentativa_em = :agora "
    "WHERE id = ANY(:ids) AND status = 'pendente'"
)
_SQL_LIMPEZA: Final = text(
    "DELETE FROM outbox WHERE status = 'entregue' AND entregue_em < :limite"
)
_SQL_CONTAGEM: Final = text("SELECT count(*) FROM outbox WHERE status = :status")


def atraso_depois_da_falha(
    tentativas: int, atrasos: Sequence[float] = ATRASOS_S
) -> float | None:
    """Segundos ate a proxima tentativa depois da falha numero ``tentativas``.

    ``None`` quando a falha passa da tabela: com quatro atrasos, a quinta falha
    leva a linha a ``dead``.
    """
    if tentativas > len(atrasos):
        return None
    return atrasos[tentativas - 1]


@dataclass(frozen=True, slots=True)
class LinhaDaOutbox:
    """Linha reivindicada; o envelope fica fora do ``repr`` (placa, texto livre)."""

    id: int
    mensagem_id: UUID
    correlation_id: UUID
    exchange: str
    routing_key: str
    envelope: dict[str, Any] = field(repr=False)
    traceparent: str | None
    tracestate: str | None
    tentativas: int


class Outbox:
    """Os passos do relay sobre a tabela ``outbox``."""

    def __init__(self, engine: Engine, atrasos: Sequence[float] = ATRASOS_S) -> None:
        self._engine = engine
        self._atrasos = atrasos

    def reivindicar(self, lote: int, lease: timedelta) -> list[LinhaDaOutbox]:
        """Ate ``lote`` linhas vencidas, em ordem por OS, seguras pelo ``lease``."""
        agora = _agora()
        with self._engine.begin() as conexao:
            linhas = [
                LinhaDaOutbox(**row._mapping)
                for row in conexao.execute(_SQL_CLAIM, {"agora": agora, "limite": lote})
            ]
            if linhas:
                conexao.execute(
                    _SQL_LEASE,
                    {"ate": agora + lease, "ids": [linha.id for linha in linhas]},
                )
        return linhas

    @contextmanager
    def travar(self, linha: LinhaDaOutbox) -> Iterator[Connection | None]:
        """Transacao da entrega, com a linha travada (fencing).

        Devolve ``None`` quando outra replica ja finalizou a linha ou a tem
        travada: esta nao publica.
        """
        with self._engine.begin() as conexao:
            travada = conexao.execute(_SQL_FENCING, {"id": linha.id}).first()
            yield conexao if travada is not None else None

    def marcar_entregue(self, conexao: Connection, linha: LinhaDaOutbox) -> None:
        conexao.execute(_SQL_ENTREGUE, {"id": linha.id, "agora": _agora()})

    def registrar_falha(
        self, conexao: Connection, linha: LinhaDaOutbox, motivo: str
    ) -> bool:
        """Conta a tentativa com o atraso da tabela; True se a linha virou ``dead``.

        ``motivo`` vai para ``ultimo_erro`` e e texto fixo: a excecao do pika
        carrega o corpo da mensagem (placa, texto livre).
        """
        tentativas = linha.tentativas + 1
        parametros = {"id": linha.id, "tentativas": tentativas, "erro": motivo}
        atraso = atraso_depois_da_falha(tentativas, self._atrasos)
        if atraso is None:
            conexao.execute(_SQL_DEAD, parametros)
            return True
        conexao.execute(
            _SQL_NOVA_TENTATIVA,
            {**parametros, "proxima": _agora() + timedelta(seconds=atraso)},
        )
        return False

    def liberar(self, ids: Sequence[int]) -> None:
        """Devolve linhas reivindicadas ja, sem esperar o lease (o broker caiu)."""
        with self._engine.begin() as conexao:
            conexao.execute(_SQL_LIBERAR, {"agora": _agora(), "ids": list(ids)})

    def limpar(self) -> int:
        """Apaga as linhas entregues ha mais de 7 dias; devolve quantas."""
        with self._engine.begin() as conexao:
            apagadas: int = conexao.execute(
                _SQL_LIMPEZA, {"limite": _agora() - _RETENCAO}
            ).rowcount
        return apagadas

    def contar(self, status: str) -> float:
        """Linhas com o ``status`` (gauge do /metrics); NaN com o banco fora."""
        try:
            with self._engine.connect() as conexao:
                total = conexao.execute(_SQL_CONTAGEM, {"status": status}).scalar_one()
        except SQLAlchemyError:
            return float("nan")
        return float(total)


def _agora() -> datetime:
    return datetime.now(UTC)
