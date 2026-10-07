"""Regras do consumidor que nao dependem de broker nem de banco."""

from __future__ import annotations

from src.compartilhado.infraestrutura.mensageria.consumidor import (
    fila_de_retry_da_copia,
)


def test_cada_tentativa_tem_a_sua_fila_de_retry_e_depois_da_quinta_e_dlq() -> None:
    assert [fila_de_retry_da_copia(t) for t in range(7)] == [
        "os.eventos.retry.1s",
        "os.eventos.retry.5s",
        "os.eventos.retry.15s",
        "os.eventos.retry.60s",
        "os.eventos.retry.300s",
        None,
        None,
    ]
