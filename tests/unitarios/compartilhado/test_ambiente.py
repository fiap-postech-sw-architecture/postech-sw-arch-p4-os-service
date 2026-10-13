"""Parametros do ambiente lidos no boot (API, relay e consumidor)."""

from __future__ import annotations

import pytest

from src.compartilhado.infraestrutura.ambiente import (
    inteiro_do_ambiente,
    numero_do_ambiente,
)


def test_numeros_do_ambiente_usam_o_padrao_e_aceitam_valor_valido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OUTBOX_LOTE", raising=False)
    monkeypatch.setenv("OUTBOX_POLL_SEGUNDOS", "0.5")

    assert inteiro_do_ambiente("OUTBOX_LOTE", 10, minimo=1) == 10
    assert numero_do_ambiente("OUTBOX_POLL_SEGUNDOS", 5.0, minimo=0.1) == 0.5


@pytest.mark.parametrize(
    "valor",
    [
        pytest.param("abc", id="texto"),
        pytest.param("0", id="zero"),
        pytest.param("-3", id="negativo"),
        pytest.param("1.5", id="decimal"),
    ],
)
def test_inteiro_invalido_aborta_o_boot(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("OUTBOX_LOTE", valor)

    with pytest.raises(RuntimeError, match="OUTBOX_LOTE"):
        inteiro_do_ambiente("OUTBOX_LOTE", 10, minimo=1)


@pytest.mark.parametrize(
    "valor",
    [
        pytest.param("abc", id="texto"),
        pytest.param("0.05", id="abaixo-do-minimo"),
        pytest.param("nan", id="nan"),
    ],
)
def test_numero_invalido_aborta_o_boot(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("OUTBOX_POLL_SEGUNDOS", valor)

    with pytest.raises(RuntimeError, match="OUTBOX_POLL_SEGUNDOS"):
        numero_do_ambiente("OUTBOX_POLL_SEGUNDOS", 5.0, minimo=0.1)
