"""A tabela ``notificacoes`` vista pelo ``notificador``: o SQL de cada passo.

O mesmo desenho da outbox (``mensageria/outbox.py``), com o e-mail no lugar do
AMQP, e uma linha por vez:

1. Claim: a linha ``pendente`` mais antiga e vencida, em ordem por OS
   (head-of-line por ``ordem_id``; ``dead`` nao bloqueia), com ``FOR UPDATE SKIP
   LOCKED``, ganha um lease (``proxima_tentativa_em`` no futuro). O fim do lease
   e o token da replica que reivindicou.
2. O desfecho (``entregue``, nova tentativa ou ``dead``) so e gravado com o
   token ainda igual (fencing): a replica que perdeu a linha nunca sobrescreve a
   marcacao de outra.

Nenhuma transacao fica aberta durante o envio: o SMTP leva ate 5 s por operacao
do socket. Uma linha por claim cabe no lease de 60 s, sem renovacao. A ordem
vale so entre os e-mails da mesma OS: um e-mail em backoff segura os seguintes
dela, nunca um comando da outbox nem o e-mail de outra OS. Os atrasos e o limite
de tentativas sao os da outbox. Todo tempo e do relogio do banco.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import text

from src.compartilhado.infraestrutura.mensageria.outbox import (
    ATRASOS_S,
    atraso_depois_da_falha,
)
from src.compartilhado.infraestrutura.outbox_mapping import apagar_em_lotes
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Engine, TextClause

_SQL_CLAIM: Final = text(
    "SELECT n.id, n.ordem_id, n.modelo, n.status_da_ordem, n.motivo, n.tentativas "
    "FROM notificacoes n "
    "WHERE n.situacao = 'pendente' AND n.proxima_tentativa_em <= now() "
    "AND NOT EXISTS (SELECT 1 FROM notificacoes p WHERE p.ordem_id = n.ordem_id "
    "AND p.id < n.id AND p.situacao = 'pendente') "
    "ORDER BY n.id FOR UPDATE OF n SKIP LOCKED LIMIT 1"
)
_SQL_LEASE: Final = text(
    "UPDATE notificacoes SET proxima_tentativa_em = now() + :lease WHERE id = :id "
    "RETURNING proxima_tentativa_em"
)
# Fencing (o `WHERE` de cada marcacao): a linha segue `pendente` e com o lease
# que esta replica gravou.
_SQL_ENTREGUE: Final = text(
    "UPDATE notificacoes SET situacao = 'entregue', entregue_em = clock_timestamp(), "
    "ultimo_erro = NULL "
    "WHERE id = :id AND situacao = 'pendente' AND proxima_tentativa_em = :lease_ate"
)
_SQL_NOVA_TENTATIVA: Final = text(
    "UPDATE notificacoes SET tentativas = :tentativas, "
    "proxima_tentativa_em = clock_timestamp() + :atraso, ultimo_erro = :erro "
    "WHERE id = :id AND situacao = 'pendente' AND proxima_tentativa_em = :lease_ate"
)
_SQL_DEAD: Final = text(
    "UPDATE notificacoes SET situacao = 'dead', tentativas = :tentativas, "
    "ultimo_erro = :erro "
    "WHERE id = :id AND situacao = 'pendente' AND proxima_tentativa_em = :lease_ate"
)
# Retencao da outbox: entregue por 7 dias e dead por 30, esta contada do fim do
# ultimo lease (gravado a menos de um lease da morte, e coberto pelo indice).
_SQL_LIMPEZA_ENTREGUES: Final = text(
    "DELETE FROM notificacoes WHERE id IN (SELECT id FROM notificacoes "
    "WHERE situacao = 'entregue' AND entregue_em < now() - interval '7 days' "
    "LIMIT :lote)"
)
_SQL_LIMPEZA_DEAD: Final = text(
    "DELETE FROM notificacoes WHERE id IN (SELECT id FROM notificacoes "
    "WHERE situacao = 'dead' AND proxima_tentativa_em < now() - interval '30 days' "
    "LIMIT :lote)"
)


@dataclass(frozen=True, slots=True)
class LinhaDeNotificacao:
    """Linha reivindicada: o pedido, as tentativas e o token do fencing."""

    id: int
    notificacao: Notificacao
    tentativas: int
    lease_ate: datetime


class FilaDeNotificacoes:
    """Os passos do notificador sobre a tabela ``notificacoes``, um por transacao.

    As marcacoes devolvem False quando a linha deixou de ser desta replica (o
    lease venceu e outra a reivindicou, ou ja a finalizou).
    """

    def __init__(self, engine: Engine, atrasos: Sequence[float] = ATRASOS_S) -> None:
        self._engine = engine
        self._atrasos = atrasos

    def reivindicar(self, lease: timedelta) -> LinhaDeNotificacao | None:
        """A proxima linha vencida, segura pelo ``lease``; ``None`` sem nenhuma."""
        with self._engine.begin() as conexao:
            linha = conexao.execute(_SQL_CLAIM).first()
            if linha is None:
                return None
            lease_ate: datetime = conexao.execute(
                _SQL_LEASE, {"id": linha.id, "lease": lease}
            ).scalar_one()
        return LinhaDeNotificacao(
            id=linha.id,
            notificacao=Notificacao(
                ordem_id=linha.ordem_id,
                modelo=ModeloDeNotificacao(linha.modelo),
                status=StatusOrdem(linha.status_da_ordem),
                motivo=linha.motivo,
            ),
            tentativas=linha.tentativas,
            lease_ate=lease_ate,
        )

    def marcar_entregue(self, linha: LinhaDeNotificacao) -> bool:
        return self._marcar(_SQL_ENTREGUE, linha)

    def registrar_falha(
        self, linha: LinhaDeNotificacao, motivo: str
    ) -> Literal["dead", "nova_tentativa", "perdida"]:
        """Conta a tentativa: volta depois do atraso da tabela ou vira ``dead``.

        ``motivo`` vai para ``ultimo_erro`` e e texto fixo (o nome da excecao):
        a mensagem de uma falha de SMTP pode trazer o endereco do destinatario.
        ``perdida``: a linha nao e mais desta replica, e nada foi gravado.
        """
        tentativas = linha.tentativas + 1
        parametros = {"tentativas": tentativas, "erro": motivo}
        atraso = atraso_depois_da_falha(tentativas, self._atrasos)
        if atraso is None:
            return "dead" if self._marcar(_SQL_DEAD, linha, **parametros) else "perdida"
        gravada = self._marcar(
            _SQL_NOVA_TENTATIVA, linha, **parametros, atraso=timedelta(seconds=atraso)
        )
        return "nova_tentativa" if gravada else "perdida"

    def limpar(self, *, entre_lotes: Callable[[], None]) -> int:
        """Apaga, em lotes, as entregues ha mais de 7 dias e as dead ha mais de 30."""
        return sum(
            apagar_em_lotes(self._engine.begin, sql, entre_lotes=entre_lotes)
            for sql in (_SQL_LIMPEZA_ENTREGUES, _SQL_LIMPEZA_DEAD)
        )

    def _marcar(
        self, sql: TextClause, linha: LinhaDeNotificacao, **parametros: object
    ) -> bool:
        with self._engine.begin() as conexao:
            alteradas: int = conexao.execute(
                sql, {"id": linha.id, "lease_ate": linha.lease_ate, **parametros}
            ).rowcount
        return alteradas == 1
