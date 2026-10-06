from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
)
from src.compartilhado.interfaces import dependencies
from src.compartilhado.interfaces.router_publico import router
from src.main import criar_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy.orm import Session

_ROUTER = "src.compartilhado.interfaces.router_publico"


def test_saude_retorna_ok() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.get("/api/v1/saude")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_saude_atraves_do_app_completo() -> None:
    # Smoke movido de tests/e2e/ (diretorio removido): o teste acima monta so
    # o router; este atravessa o `criar_app()` REAL (middlewares, error
    # handler, wiring) — um middleware quebrado passaria no de cima e falharia
    # aqui. Sem lifespan/TestClient-context: /saude nao toca banco.
    client = TestClient(criar_app())
    resp = client.get("/api/v1/saude")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


class TestReadiness:
    """``/api/v1/saude/pronto``: 200 so com o banco respondendo em ate 2 s."""

    @staticmethod
    def _client(
        monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Session] | None
    ) -> TestClient:
        monkeypatch.setattr(dependencies, "_session_factory", factory)
        # Logger sem cache: um configurar_logging() anterior escaparia do
        # capture_logs (MEMORY, gotcha do structlog).
        monkeypatch.setattr(f"{_ROUTER}._log", structlog.get_logger())
        return TestClient(criar_app())

    def test_pronto_200_com_o_banco_respondendo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = criar_engine("sqlite:///:memory:")
        try:
            client = self._client(monkeypatch, criar_session_factory(engine))
            resp = client.get("/api/v1/saude/pronto")
        finally:
            engine.dispose()
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_pronto_503_com_o_banco_inacessivel(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Diretorio inexistente: o driver falha ao conectar (OperationalError).
        engine = criar_engine(f"sqlite:///{tmp_path}/nao/existe/banco.db")
        try:
            client = self._client(monkeypatch, criar_session_factory(engine))
            with structlog.testing.capture_logs() as logs:
                resp = client.get("/api/v1/saude/pronto")
        finally:
            engine.dispose()
        assert resp.status_code == 503
        assert resp.json() == {"status": "indisponivel"}
        assert {"event": "readiness_failed", "error": "OperationalError"} in [
            {"event": log["event"], "error": log.get("error")} for log in logs
        ]

    def test_pronto_503_sem_session_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp = self._client(monkeypatch, None).get("/api/v1/saude/pronto")
        assert resp.status_code == 503

    def test_pronto_503_quando_o_banco_passa_do_tempo_limite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(f"{_ROUTER}._TIMEOUT_PRONTO_S", 0.05)
        monkeypatch.setattr(f"{_ROUTER}._consultar_banco", lambda: time.sleep(0.5))
        client = self._client(monkeypatch, None)
        inicio = time.monotonic()
        resp = client.get("/api/v1/saude/pronto")
        assert resp.status_code == 503
        # Responde no tempo limite, sem esperar a consulta presa terminar.
        assert time.monotonic() - inicio < 0.4
