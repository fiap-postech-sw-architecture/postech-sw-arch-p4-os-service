"""Resultado do tratamento de um evento e os erros que o interrompem.

O ``OrquestradorDaSaga`` devolve o ``Tratamento`` (ou levanta um dos dois
erros); o consumidor leva a etapa antes e depois e o desfecho ao span e ao log
(RFC-004 secao 4.5; ADR-043).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.aplicacao.mensageria import (
    Desfecho,
    FalhaPermanenteError,
    FalhaTransitoriaError,
)

if TYPE_CHECKING:
    from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga


@dataclass(frozen=True, slots=True)
class Tratamento:
    """Desfecho do evento e a etapa antes e depois dele (span e log).

    ``falha`` e a da compensacao quando o evento levou a saga a
    ``falha_na_compensacao`` (``estorno_recusado``): o span e o log dizem a causa.
    """

    desfecho: Desfecho
    etapa: EtapaSaga
    etapa_nova: EtapaSaga
    falha: str | None = None


class EventoAdiantadoError(FalhaTransitoriaError):
    """Evento de etapa a frente da atual: volta pela fila de retry (RFC-004 secao 4.5).

    Esgotadas as tentativas, vai para a DLQ com alerta.
    """

    def __init__(self, etapa: EtapaSaga) -> None:
        super().__init__(f"evento adiantado na etapa {etapa.value}")
        self.etapa = etapa


class EventoRecusadoError(FalhaPermanenteError):
    """Evento que nenhuma tentativa resolve: DLQ com o ``motivo`` em codigo.

    ``etapa`` e a da saga quando ela existe (vai para o span do consumo).
    """

    def __init__(self, motivo: str, etapa: EtapaSaga | None = None) -> None:
        super().__init__(motivo)
        self.etapa = etapa
