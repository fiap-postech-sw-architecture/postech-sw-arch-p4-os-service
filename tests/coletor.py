"""Apoio dos testes do coletor da API: a raspagem em dicionario e a leitura."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from tests.conftest import valor_da_metrica

if TYPE_CHECKING:
    from src.ordem_servico.infraestrutura.coletor_da_api import ColetorDaApi

# O nome que o coletor da a thread da leitura (``coletor_da_api._iniciar``).
THREAD_DE_LEITURA = "coletor-da-api"


def raspar(coletor: ColetorDaApi) -> dict[str, list[float]]:
    """Uma raspagem: o valor de cada amostra, por familia."""
    return {f.name: [a.value for a in f.samples] for f in coletor.collect()}


def falhas_do_coletor() -> float:
    """``pytstop_saga_coletor_falhas_total``; e global, entao compare antes e depois."""
    return valor_da_metrica("pytstop_saga_coletor_falhas_total")


def esperar_a_thread_de_leitura() -> None:
    """Espera a thread de leitura acabar: a vez volta no fim dela."""
    for thread in threading.enumerate():
        if thread.name == THREAD_DE_LEITURA:
            thread.join(5)
