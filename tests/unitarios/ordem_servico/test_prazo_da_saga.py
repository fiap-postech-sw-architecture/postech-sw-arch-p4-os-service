"""Prazo de resposta da saga: lido do ambiente no boot, uma vez, e usado pela API."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from src.ordem_servico.infraestrutura.prazo_da_saga import (
    intervalo_dos_prazos,
    maximo_de_reenvios_da_saga,
    prazo_de_resposta_da_saga,
)
from src.ordem_servico.interfaces import dependencies
from src.ordem_servico.interfaces.dependencies import (
    configurar_prazo_de_resposta,
    obter_cancelar_ordem,
    obter_retomar_compensacao,
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


def test_a_retomada_usa_o_prazo_guardado_no_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies, "_prazo_de_resposta", None)
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "45")
    configurar_prazo_de_resposta(prazo_de_resposta_da_saga())
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "7")

    assert obter_retomar_compensacao(MagicMock())._contexto.prazo_resposta == (
        timedelta(seconds=45)
    )


def test_maximo_de_reenvios_tem_padrao_de_5(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAGA_MAX_REENVIOS", raising=False)
    assert maximo_de_reenvios_da_saga() == 5

    monkeypatch.setenv("SAGA_MAX_REENVIOS", "0")
    assert maximo_de_reenvios_da_saga() == 0


@pytest.mark.parametrize("valor", ["-1", "cinco", "1.5", ""])
def test_maximo_de_reenvios_invalido_aborta_o_boot(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("SAGA_MAX_REENVIOS", valor)

    with pytest.raises(RuntimeError, match="SAGA_MAX_REENVIOS"):
        maximo_de_reenvios_da_saga()


def test_intervalo_dos_prazos_tem_padrao_de_30_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRAZOS_INTERVALO_SEGUNDOS", raising=False)
    assert intervalo_dos_prazos() == 30

    monkeypatch.setenv("PRAZOS_INTERVALO_SEGUNDOS", "0.2")
    assert intervalo_dos_prazos() == 0.2


@pytest.mark.parametrize("valor", ["0", "-5", "301", "nan", "meio minuto"])
def test_intervalo_dos_prazos_invalido_aborta_o_boot(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("PRAZOS_INTERVALO_SEGUNDOS", valor)

    with pytest.raises(RuntimeError, match="PRAZOS_INTERVALO_SEGUNDOS"):
        intervalo_dos_prazos()
