"""Metricas da sessao que so contam depois do commit da transacao raiz.

Uma metrica de negocio (a saga iniciada, a etapa alterada, a OS criada) nao pode
contar o que a transacao desfez: ``registrar`` guarda a observacao na sessao, e ela
so roda no ``after_commit`` da transacao raiz. Rollback (o da raiz e o de um
SAVEPOINT, que descarta o que foi anotado dentro dele), conflito de versao ou
commit que falha a descartam: a mensagem que volta pela retry nao conta duas vezes.

Quem usa: os fatos da saga (``metricas_da_saga.anotar``) e a OS criada
(``metrics``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import event
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from sqlalchemy.orm import SessionTransaction

# Observacoes da transacao em curso, na sessao: so valem depois do commit. Cada
# uma leva o SAVEPOINT em que foi anotada (``None`` fora de qualquer um), para o
# rollback do SAVEPOINT a descartar.
_PENDENTES: Final = "pytstop_metricas_apos_commit"
_sessoes_instrumentadas = False


def registrar(sessao: Session, observacoes: Iterable[Callable[[], None]]) -> None:
    """Guarda cada observacao ate o commit da transacao da ``sessao``.

    Rodam no commit da transacao raiz; o rollback da raiz ou do SAVEPOINT em que
    foram guardadas as descarta.
    """
    _instrumentar_sessoes()
    savepoint = sessao.get_nested_transaction()
    sessao.info.setdefault(_PENDENTES, []).extend((savepoint, o) for o in observacoes)


def _instrumentar_sessoes() -> None:
    global _sessoes_instrumentadas  # noqa: PLW0603  # init-once flag
    if _sessoes_instrumentadas:
        return
    _sessoes_instrumentadas = True
    event.listen(Session, "after_commit", _ao_comitar)
    event.listen(Session, "after_soft_rollback", _ao_desfazer)
    event.listen(Session, "after_transaction_end", _ao_encerrar)


def _ao_comitar(sessao: Session) -> None:
    """Commit da transacao raiz: as observacoes guardadas valem.

    Liberar um SAVEPOINT tambem dispara o ``after_commit``; a transacao raiz
    ainda pode ser desfeita, entao ali nada conta.
    """
    if sessao.in_nested_transaction():
        return
    for _, observar in sessao.info.pop(_PENDENTES, ()):
        observar()


def _ao_desfazer(sessao: Session, desfeita: SessionTransaction) -> None:
    """Rollback de um SAVEPOINT: descarta o que foi anotado dentro dele.

    Vale tambem para o SAVEPOINT interno que um SAVEPOINT desfeito ja tinha
    liberado. O que foi anotado fora dele segue para o commit da raiz.
    """
    pendentes = sessao.info.get(_PENDENTES)
    if pendentes:
        sessao.info[_PENDENTES] = [
            (savepoint, observar)
            for savepoint, observar in pendentes
            if not _dentro_de(savepoint, desfeita)
        ]


def _dentro_de(
    savepoint: SessionTransaction | None, desfeita: SessionTransaction
) -> bool:
    while savepoint is not None:
        if savepoint is desfeita:
            return True
        savepoint = savepoint.parent
    return False


def _ao_encerrar(sessao: Session, transacao: SessionTransaction) -> None:
    """Transacao raiz encerrada sem commit (rollback ou close): descarta tudo."""
    if transacao.parent is None:
        sessao.info.pop(_PENDENTES, None)
