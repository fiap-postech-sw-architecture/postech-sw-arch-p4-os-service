"""Metricas de negocio da OS via listener ``before_flush`` (herdado do p3).

Toda abertura e transicao de ``OrdemDeServico`` passa por um flush da
UnitOfWork, entao um unico listener na ``Session`` mede tudo sem instrumentar
caso de uso por caso de uso; dominio e aplicacao ficam livres de
observabilidade.

- abertura: ``OrdemDeServico`` em ``session.new`` -> ``pytstop_os_criadas_total``.
- transicao: a history de ``_status`` mostra o status antigo no flush que
  grava a troca -> ``pytstop_os_duracao_status_segundos{status=<antigo>}``.
  A permanencia vem das duas ultimas linhas do historico (entrada no status
  antigo e no novo), exata.

ponytail: duas transicoes no mesmo flush medem so a ultima; os casos de uso
fazem uma por transacao. Flush seguido de rollback ainda conta (raro).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import get_history

from src.compartilhado.infraestrutura.metrics import metricas_api
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

if TYPE_CHECKING:
    from sqlalchemy.orm import UOWTransaction

_instrumentado = False


def instrumentar_metricas_de_ordens() -> None:
    """Registra o listener (idempotente). So roda com as metricas ligadas."""
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
            metricas_api.os_criada()
    for sujo in session.dirty:
        if isinstance(sujo, OrdemDeServico):
            _observar_transicao(sujo)


def _observar_transicao(ordem: OrdemDeServico) -> None:
    """Observa a permanencia no status anterior quando este flush o troca."""
    anteriores = get_history(ordem, "_status").deleted
    historico = ordem.historico
    if not anteriores or len(historico) < 2:  # noqa: PLR2004  # par entrada/saida
        return
    duracao_s = (historico[-1].ocorrido_em - historico[-2].ocorrido_em).total_seconds()
    metricas_api.os_duracao_status(anteriores[0].value, max(duracao_s, 0.0))
