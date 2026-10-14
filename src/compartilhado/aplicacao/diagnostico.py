"""Diagnostico de excecao para o log, sem a mensagem dela."""

from __future__ import annotations

import traceback
from pathlib import Path

# O codigo do servico (``src/``): o quadro daqui diz onde o servico errou, e o das
# bibliotecas (enum, sqlalchemy, pika) so onde a biblioteca reclamou.
_SRC = str(Path(__file__).resolve().parents[2]) + "/"


def onde(exc: BaseException) -> str:
    """Arquivo e linha em que a excecao passou pelo servico, para o log.

    E o ultimo quadro dentro de ``src/`` (o mais interno que e do servico), ou o
    mais interno de todos quando a excecao nao passou por ele. Sem a mensagem:
    ela pode trazer dado da mensagem (placa, texto livre), e os logs de falha
    levam so o tipo e este ``arquivo:linha``.
    """
    quadros = traceback.extract_tb(exc.__traceback__)
    if not quadros:
        return "desconhecido"
    do_servico = [q for q in quadros if q.filename.startswith(_SRC)]
    quadro = (do_servico or quadros)[-1]
    return f"{quadro.filename.rsplit('/', 1)[-1]}:{quadro.lineno}"
