"""Parametros lidos do ambiente, com a validacao do boot.

Modulo sem efeito colateral no import: a API, o relay, o consumidor, o prazos e o
notificador o leem sem puxar o cliente AMQP nem as metricas dos processos (a API
nunca as importa, ou o ``/metrics`` dela mostraria series do relay zeradas).
"""

from __future__ import annotations

import math
import os


def inteiro_do_ambiente(nome: str, padrao: int, *, minimo: int) -> int:
    """Le um inteiro do ambiente; valor invalido aborta o boot com mensagem clara."""
    bruto = os.environ.get(nome, str(padrao))
    try:
        valor = int(bruto)
    except ValueError:
        valor = minimo - 1
    if valor < minimo:
        msg = f"config invalida no boot: {nome}={bruto!r} (inteiro >= {minimo})"
        raise RuntimeError(msg)
    return valor


def numero_do_ambiente(
    nome: str, padrao: float, *, minimo: float, maximo: float = math.inf
) -> float:
    """Le um numero do ambiente; valor invalido aborta o boot com mensagem clara."""
    bruto = os.environ.get(nome, str(padrao))
    try:
        valor = float(bruto)
    except ValueError:
        valor = minimo - 1
    if math.isnan(valor) or valor < minimo or valor > maximo:
        msg = (
            f"config invalida no boot: {nome}={bruto!r} (numero de {minimo} a {maximo})"
        )
        raise RuntimeError(msg)
    return valor
