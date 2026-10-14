"""Prazos da saga lidos do ambiente (RFC-004 secoes 4.6 e 10.3).

``SAGA_PRAZO_RESPOSTA_SEGUNDOS`` e o mesmo valor na API (a primeira compensacao
do cancelamento e a retomada), no consumidor (os comandos seguintes) e no
processo ``prazos``: quem o mudar num lugar so grava prazos diferentes para a
mesma saga. ``SAGA_MAX_REENVIOS`` e ``PRAZOS_INTERVALO_SEGUNDOS`` so o
``prazos`` le.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from src.compartilhado.infraestrutura.ambiente import (
    inteiro_do_ambiente,
    numero_do_ambiente,
)

# Espera por resposta a comando com prazo tecnico, gravada pela saga no envio
# (RFC-004 secoes 4.6 e 10.3).
PRAZO_RESPOSTA_PADRAO_S: Final = 120
# Reenvios do mesmo comando antes de compensar (ou de parar a compensacao): o
# original e os cinco reenvios cobrem 12 minutos com o prazo padrao, mais que os
# 381 s do retry do consumidor (um teste confere a relacao).
MAX_REENVIOS_PADRAO: Final = 5
# Intervalo entre os ciclos do processo ``prazos``.
INTERVALO_DOS_PRAZOS_PADRAO_S: Final = 30.0


def prazo_de_resposta_da_saga() -> timedelta:
    """``SAGA_PRAZO_RESPOSTA_SEGUNDOS`` (1 ou mais, padrao 120).

    O prazo que a saga grava ao enviar um comando com resposta automatica.
    Valor invalido aborta o boot com a variavel e o valor na mensagem.
    """
    return timedelta(
        seconds=inteiro_do_ambiente(
            "SAGA_PRAZO_RESPOSTA_SEGUNDOS", PRAZO_RESPOSTA_PADRAO_S, minimo=1
        )
    )


def maximo_de_reenvios_da_saga() -> int:
    """``SAGA_MAX_REENVIOS`` (0 ou mais, padrao 5).

    Valor invalido aborta o boot com a variavel e o valor na mensagem.
    """
    return inteiro_do_ambiente("SAGA_MAX_REENVIOS", MAX_REENVIOS_PADRAO, minimo=0)


def intervalo_dos_prazos() -> float:
    """``PRAZOS_INTERVALO_SEGUNDOS`` (de 0,1 a 300, padrao 30).

    O teto de 5 minutos mantem o ciclo pequeno perto do prazo de resposta.
    Valor invalido aborta o boot com a variavel e o valor na mensagem.
    """
    return numero_do_ambiente(
        "PRAZOS_INTERVALO_SEGUNDOS",
        INTERVALO_DOS_PRAZOS_PADRAO_S,
        minimo=0.1,
        maximo=300.0,
    )
