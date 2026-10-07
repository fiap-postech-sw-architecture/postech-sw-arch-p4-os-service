"""``servir``: o servidor de teste nao deixa soquete nem thread para tras."""

from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI

from tests import servidor_http
from tests.servidor_http import servir

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _app_que_nao_sobe() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        msg = "falha no startup"
        raise RuntimeError(msg)
        yield  # pragma: no cover

    return FastAPI(lifespan=lifespan)


# Com o startup quebrado o uvicorn termina a thread com sys.exit(3), por desenho.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_servidor_que_nao_sobe_levanta_e_fecha_o_soquete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soquete = servidor_http._soquete_em_porta_livre()
    monkeypatch.setattr(servidor_http, "_soquete_em_porta_livre", lambda: soquete)

    with (
        pytest.raises(RuntimeError, match="o uvicorn de teste nao subiu"),
        servir(_app_que_nao_sobe(), lifespan="on"),
    ):
        pytest.fail("o bloco nao devia rodar")

    assert soquete.fileno() == -1  # fechado


def test_servidor_no_ar_responde_e_e_derrubado_no_fim() -> None:
    app = FastAPI()

    @app.get("/ping")
    def _ping() -> dict[str, str]:
        return {"ok": "sim"}

    with servir(app) as url:
        assert httpx.get(f"{url}/ping", timeout=5).json() == {"ok": "sim"}
        porta = int(url.rsplit(":", 1)[1])

    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", porta), 1):  # noqa: PT011
        pass
