"""Prazo de resposta da saga: lido do ambiente no boot, uma vez, e usado pela API."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from src.ordem_servico.infraestrutura.prazo_da_saga import prazo_de_resposta_da_saga
from src.ordem_servico.interfaces import dependencies
from src.ordem_servico.interfaces.dependencies import (
    configurar_prazo_de_resposta,
    obter_cancelar_ordem,
)


def test_prazo_de_resposta_da_saga_tem_padrao_de_120_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", raising=False)
    assert prazo_de_resposta_da_saga() == timedelta(seconds=120)

    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "1")
    assert prazo_de_resposta_da_saga() == timedelta(seconds=1)


@pytest.mark.parametrize("valor", ["0", "-1", "2m"])
def test_prazo_de_resposta_invalido_aborta_o_boot(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", valor)

    with pytest.raises(RuntimeError, match="SAGA_PRAZO_RESPOSTA_SEGUNDOS"):
        prazo_de_resposta_da_saga()


def _prazo_do_cancelamento() -> timedelta:
    caso_de_uso = obter_cancelar_ordem(MagicMock())
    return caso_de_uso._contexto.prazo_resposta


def test_o_cancelamento_usa_o_prazo_guardado_no_startup_sem_reler_o_ambiente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies, "_prazo_de_resposta", None)
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "45")
    configurar_prazo_de_resposta(prazo_de_resposta_da_saga())
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "7")

    assert _prazo_do_cancelamento() == timedelta(seconds=45)


def test_sem_startup_o_cancelamento_usa_o_prazo_do_ambiente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies, "_prazo_de_resposta", None)
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "7")

    assert _prazo_do_cancelamento() == timedelta(seconds=7)
