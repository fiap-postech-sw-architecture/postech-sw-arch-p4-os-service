"""O que a acao que envia comando fora do consumidor precisa do ambiente.

O cancelamento (e, depois, a retomada e o processo de prazos) grava o comando
com o prazo tecnico a partir do relogio e abre o span da acao no trace da saga
(RFC-004 secoes 4.6 e 9; ADR-043). O relogio e o span entram de fora para os
testes controlarem o tempo e a camada de aplicacao nao importar telemetria.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import timedelta

# Abre o span ``nome`` como filho do ``traceparent`` da saga (``None``: do span
# corrente), com link para o span corrente.
type SpanDaSaga = Callable[[str, str | None], AbstractContextManager[None]]


def agora() -> datetime:
    return datetime.now(UTC)


def sem_span(_nome: str, _traceparent: str | None) -> AbstractContextManager[None]:
    """Nenhum span: o padrao de quem roda sem tracer."""
    return nullcontext()


@dataclass(frozen=True, slots=True)
class ContextoDeEnvio:
    """Prazo de resposta do comando enviado, relogio e span da acao."""

    prazo_resposta: timedelta
    relogio: Callable[[], datetime] = agora
    span: SpanDaSaga = sem_span
