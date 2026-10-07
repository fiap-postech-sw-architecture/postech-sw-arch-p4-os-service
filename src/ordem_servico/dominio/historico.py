"""Historico de status da OS: cada transicao vira uma ``MudancaDeStatus``."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from src.compartilhado.dominio.entity import Entity

if TYPE_CHECKING:
    from datetime import datetime

    from src.ordem_servico.dominio.status import StatusOrdem


class OrigemMudanca(StrEnum):
    """Quem provocou a mudanca de status.

    ``ATENDIMENTO``: usuario interno pela API (abertura, entrega, cancelamento).
    ``EXECUCAO`` / ``BILLING``: fato reportado pelo servico dono do dado.
    ``SAGA``: decisao do orquestrador (compensacao, prazo esgotado).
    """

    ATENDIMENTO = "atendimento"
    EXECUCAO = "execucao"
    BILLING = "billing"
    SAGA = "saga"


@dataclass(eq=False)
class MudancaDeStatus(Entity):
    """Linha do historico (entidade interna do agregado, append-only).

    ``de`` e ``None`` so na abertura. ``sequencia`` (1, 2, ...) ordena a linha
    do tempo sem depender da resolucao do relogio. ``ator`` e quem provocou a
    mudanca: o ``sub`` do JWT do usuario ou o processo (``consumidor``,
    ``prazos``), RFC-004 secao 7.2; ``None`` so nas linhas anteriores a ele.
    Sem mutadores: o agregado cria e nunca altera.
    """

    _sequencia: int = field(kw_only=True)
    _de: StatusOrdem | None = field(kw_only=True)
    _para: StatusOrdem = field(kw_only=True)
    _origem: OrigemMudanca = field(kw_only=True)
    # Texto livre (motivo de cancelamento): fora do repr, pode conter PII.
    _motivo: str | None = field(kw_only=True, repr=False)
    _ator: str | None = field(kw_only=True)
    _ocorrido_em: datetime = field(kw_only=True)

    @property
    def sequencia(self) -> int:
        return self._sequencia

    @property
    def de(self) -> StatusOrdem | None:
        return self._de

    @property
    def para(self) -> StatusOrdem:
        return self._para

    @property
    def origem(self) -> OrigemMudanca:
        return self._origem

    @property
    def motivo(self) -> str | None:
        return self._motivo

    @property
    def ator(self) -> str | None:
        return self._ator

    @property
    def ocorrido_em(self) -> datetime:
        return self._ocorrido_em
