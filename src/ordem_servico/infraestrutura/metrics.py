"""Metrica de negocio da OS via listener ``before_flush`` (herdado do p3, ADR-043).

Toda abertura de ``OrdemDeServico`` passa por um flush da UnitOfWork, entao um
unico listener na ``Session`` mede tudo sem instrumentar caso de uso por caso de
uso; dominio e aplicacao ficam livres de observabilidade.

- ``pytstop_os_criadas_total``: uma por ``OrdemDeServico`` em ``session.new``.

O contador so conta depois do commit da transacao raiz, a regra das metricas da
saga (``metricas_apos_commit.registrar``): rollback, conflito de versao ou commit
que falha descartam a abertura, e o painel "Volume diario de OS" nao conta OS que
nao foi gravada. As transicoes de status nao sao medidas aqui: a permanencia em
cada status vem do historico, no coletor da API (``coletor_da_api.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from prometheus_client import Counter
from sqlalchemy import event
from sqlalchemy.orm import Session

from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.infraestrutura.metricas_apos_commit import registrar

if TYPE_CHECKING:
    from sqlalchemy.orm import UOWTransaction

OS_CRIADAS: Final = Counter("pytstop_os_criadas_total", "Ordens de servico criadas.")

_instrumentado = False


def instrumentar_metricas_de_ordens() -> None:
    """Registra o listener (idempotente)."""
    global _instrumentado  # noqa: PLW0603  # init-once flag
    if _instrumentado:
        return
    _instrumentado = True
    event.listen(Session, "before_flush", _observar_flush)


def _observar_flush(
    session: Session, _flush_context: UOWTransaction, _instances: object
) -> None:
    for novo in session.new:
        if isinstance(novo, OrdemDeServico):
            registrar(session, [OS_CRIADAS.inc])
