"""Diagnostico de excecao para o log, sem a mensagem dela."""

from __future__ import annotations

import traceback


def onde(exc: BaseException) -> str:
    """Arquivo e linha em que a excecao nasceu, para o log (sem a mensagem dela).

    A mensagem de uma excecao pode trazer dado da mensagem (placa, texto livre):
    os logs de falha levam so o tipo e este ``arquivo:linha``.
    """
    quadros = traceback.extract_tb(exc.__traceback__)
    if not quadros:
        return "desconhecido"
    return f"{quadros[-1].filename.rsplit('/', 1)[-1]}:{quadros[-1].lineno}"
