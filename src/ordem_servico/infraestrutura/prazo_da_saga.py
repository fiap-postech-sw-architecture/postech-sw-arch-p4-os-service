"""Prazo de resposta dos comandos da saga, lido do ambiente.

``SAGA_PRAZO_RESPOSTA_SEGUNDOS`` e o mesmo valor na API (a primeira compensacao
do cancelamento), no consumidor (os comandos seguintes) e no processo de prazos:
quem o mudar num lugar so grava prazos diferentes para a mesma saga.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from src.compartilhado.infraestrutura.ambiente import inteiro_do_ambiente

# Espera por resposta a comando com prazo tecnico, gravada pela saga no envio
# (RFC-004 secoes 4.6 e 10.3).
_PRAZO_RESPOSTA_PADRAO_S: Final = 120


def prazo_de_resposta_da_saga() -> timedelta:
    """``SAGA_PRAZO_RESPOSTA_SEGUNDOS`` (1 ou mais, padrao 120).

    O prazo que a saga grava ao enviar um comando com resposta automatica.
    Valor invalido aborta o boot com a variavel e o valor na mensagem.
    """
    return timedelta(
        seconds=inteiro_do_ambiente(
            "SAGA_PRAZO_RESPOSTA_SEGUNDOS", _PRAZO_RESPOSTA_PADRAO_S, minimo=1
        )
    )
