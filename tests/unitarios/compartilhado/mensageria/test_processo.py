"""Boot, saude e configuracao comuns ao relay e ao consumidor."""

from __future__ import annotations

import signal
import threading
from typing import TYPE_CHECKING

import pytest

from src.compartilhado.infraestrutura.mensageria import processo
from src.compartilhado.infraestrutura.mensageria.processo import (
    Sinalizador,
    inteiro_do_ambiente,
    numero_do_ambiente,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_URL_DEMO = "amqp://os:pytstop-os-demo-2026@rabbitmq:5672/%2F"  # gitleaks:allow


def test_sinalizador_bate_o_heartbeat_e_marca_e_desmarca_o_pronto(
    tmp_path: Path,
) -> None:
    sinal = Sinalizador("relay", tmp_path)

    sinal.bater()
    sinal.marcar_pronto()
    assert (tmp_path / "relay-heartbeat").is_file()
    assert (tmp_path / "relay-pronto").is_file()

    sinal.marcar_nao_pronto()
    sinal.marcar_nao_pronto()  # idempotente: o arquivo pode nem existir
    assert not (tmp_path / "relay-pronto").exists()
    assert (tmp_path / "relay-heartbeat").is_file()


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


def test_sigterm_e_sigint_pedem_o_encerramento(monkeypatch: pytest.MonkeyPatch) -> None:
    instalados: dict[int, Callable[..., object]] = {}
    monkeypatch.setattr(
        signal, "signal", lambda sinal, handler: instalados.update({sinal: handler})
    )
    parar = threading.Event()

    processo.instalar_sinais(parar)
    instalados[signal.SIGTERM](signal.SIGTERM, None)

    assert parar.is_set()
    assert set(instalados) == {signal.SIGTERM, signal.SIGINT}


@pytest.fixture
def ambiente_de_boot(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(processo, "configurar_logging", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RABBITMQ_URL", _URL_DEMO)
    return monkeypatch


def test_preparar_monta_o_banco_e_os_parametros_do_broker(
    ambiente_de_boot: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine, parametros = processo.preparar("relay")

    try:
        assert engine.url.drivername == "sqlite"
        assert parametros.credentials.username == "os"
        assert parametros.host == "rabbitmq"
        assert parametros.client_properties == {
            "connection_name": "pytstop-os-service relay"
        }
        assert "pytstop-os-service relay | commit" in capsys.readouterr().out
    finally:
        engine.dispose()


def test_preparar_sem_rabbitmq_url_aborta(
    ambiente_de_boot: pytest.MonkeyPatch,
) -> None:
    ambiente_de_boot.delenv("RABBITMQ_URL")

    with pytest.raises(RuntimeError, match="RABBITMQ_URL obrigatoria"):
        processo.preparar("consumidor")


def test_preparar_recusa_a_senha_de_demonstracao_em_producao(
    ambiente_de_boot: pytest.MonkeyPatch,
) -> None:
    ambiente_de_boot.setenv("ENVIRONMENT", "production")

    with pytest.raises(RuntimeError, match="senha de demonstracao"):
        processo.preparar("consumidor")


def test_preparar_aceita_senha_real_em_producao(
    ambiente_de_boot: pytest.MonkeyPatch,
) -> None:
    ambiente_de_boot.setenv("ENVIRONMENT", "production")
    ambiente_de_boot.setenv("RABBITMQ_URL", "amqp://os:outra-senha@rabbitmq:5672/%2F")

    engine, _ = processo.preparar("consumidor")
    engine.dispose()


def test_subir_metricas_usa_a_porta_do_ambiente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portas: list[int] = []
    monkeypatch.setattr(processo, "start_http_server", portas.append)
    monkeypatch.setenv("METRICS_PORT", "9200")

    assert processo.subir_metricas() == 9200
    assert portas == [9200]
