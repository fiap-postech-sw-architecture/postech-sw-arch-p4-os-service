"""A tabela ``notificacoes`` vista pelo ``notificador``: o SQL de cada passo.

O mesmo desenho da outbox (``mensageria/outbox.py``), com o e-mail no lugar do
AMQP, e uma linha por vez:

1. Claim: a linha ``pendente`` mais antiga e vencida, em ordem por OS
   (head-of-line por ``ordem_id``; ``dead`` nao bloqueia), com ``FOR UPDATE SKIP
   LOCKED``, ganha um lease (``proxima_tentativa_em`` no futuro). O fim do lease
   e o token da replica que reivindicou.
2. O desfecho (``entregue``, nova tentativa ou ``dead``) so e gravado com o
   token ainda igual (fencing): a replica que perdeu a linha nunca sobrescreve a
   marcacao de outra. E o ``MarcacaoComFencing`` da outbox, o mesmo codigo: so o
   claim e a linha reivindicada sao desta fila.

Nenhuma transacao fica aberta durante o envio: o SMTP leva ate 5 s por operacao
do socket. Uma linha por claim cabe no lease de 60 s, sem renovacao. A ordem
vale so entre os e-mails da mesma OS: um e-mail em backoff segura os seguintes
dela, nunca um comando da outbox nem o e-mail de outra OS. Os atrasos e o limite
de tentativas sao os da outbox. Todo tempo e do relogio do banco.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import text

from src.compartilhado.infraestrutura.mensageria.outbox import (
    ATRASOS_S,
    MarcacaoComFencing,
)
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime, timedelta

    from sqlalchemy import Engine

_SQL_CLAIM: Final = text(
    "SELECT n.id, n.ordem_id, n.modelo, n.status_da_ordem, n.motivo, n.tentativas "
    "FROM notificacoes n "
    "WHERE n.situacao = 'pendente' AND n.proxima_tentativa_em <= now() "
    "AND n.modelo = ANY(:modelos) AND n.status_da_ordem = ANY(:status) "
    "AND NOT EXISTS (SELECT 1 FROM notificacoes p WHERE p.ordem_id = n.ordem_id "
    "AND p.id < n.id AND p.situacao = 'pendente') "
    "ORDER BY n.id FOR UPDATE OF n SKIP LOCKED LIMIT 1"
)
# So reivindica o que esta versao sabe escrever: o pedido de um modelo ou de um
# status novo (a API ja na versao seguinte, o notificador ainda na anterior) fica
# pendente para o notificador novo, em vez de derrubar este no laco do claim. A
# ordem por OS segue valendo: o pedido que fica segura os seguintes da mesma OS.
_CONHECIDOS: Final = {
    "modelos": [modelo.value for modelo in ModeloDeNotificacao],
    "status": [status.value for status in StatusOrdem],
}
_SQL_LEASE: Final = text(
    "UPDATE notificacoes SET proxima_tentativa_em = now() + :lease WHERE id = :id "
    "RETURNING proxima_tentativa_em"
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
        self._marcacao = MarcacaoComFencing(
            engine, tabela="notificacoes", situacao="situacao", atrasos=atrasos
        )

    def reivindicar(self, lease: timedelta) -> LinhaDeNotificacao | None:
        """A proxima linha vencida, segura pelo ``lease``; ``None`` sem nenhuma."""
        with self._engine.begin() as conexao:
            linha = conexao.execute(_SQL_CLAIM, _CONHECIDOS).first()
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
        """Marca a linha ``entregue``; False se ela nao e mais desta replica."""
        return self._marcacao.entregue(linha)

    def registrar_falha(
        self, linha: LinhaDeNotificacao, motivo: str
    ) -> Literal["dead", "nova_tentativa", "perdida"]:
        """Conta a tentativa: volta depois do atraso da tabela ou vira ``dead``.

        ``motivo`` vai para ``ultimo_erro`` e e texto fixo (o nome da excecao):
        a mensagem de uma falha de SMTP pode trazer o endereco do destinatario.
        ``perdida``: a linha nao e mais desta replica, e nada foi gravado.
        """
        return self._marcacao.falha(linha, motivo)

    def limpar(self, *, entre_lotes: Callable[[], None]) -> int:
        """Apaga, em lotes, as entregues ha mais de 7 dias e as dead ha mais de 30."""
        return self._marcacao.limpar(entre_lotes=entre_lotes)
