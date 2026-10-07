"""Marcos da OS que a saga le para classificar um evento (RFC-004 secao 4.5).

A saga decide pela propria etapa; so dentro da mesma etapa, e com a OS
encerrada, ela precisa saber o que a OS ja viveu. Os marcos sao fatos da OS,
lidos dela e congelados num VO antes de qualquer mudanca do evento.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.dominio.value_object import ValueObject
from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS, StatusOrdem

if TYPE_CHECKING:
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


@dataclass(frozen=True, slots=True)
class MarcosDaOrdem(ValueObject):
    """O que a OS ja viveu, sem expor o status a saga.

    ``diagnostico_iniciado``: a OS passou por EM_DIAGNOSTICO. ``checkout_aberto``:
    o pagamento foi solicitado (a OS tem o resumo dele). ``encerrada``: a OS esta
    cancelada ou entregue.
    """

    diagnostico_iniciado: bool
    checkout_aberto: bool
    encerrada: bool

    @classmethod
    def da_ordem(cls, ordem: OrdemDeServico) -> MarcosDaOrdem:
        """Os marcos da ``ordem`` agora, pelo historico, pelo resumo e pelo status."""
        return cls(
            diagnostico_iniciado=any(
                m.para is StatusOrdem.EM_DIAGNOSTICO for m in ordem.historico
            ),
            checkout_aberto=ordem.resumo_pagamento is not None,
            encerrada=ordem.status in ESTADOS_TERMINAIS,
        )
