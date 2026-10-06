"""App servida por um uvicorn de verdade numa thread, em porta livre.

O ``PyJWKClient`` busca o JWKS com urllib, entao o TestClient nao serve para o
validador independente.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


@contextmanager
def servir(app: FastAPI, *, lifespan: Literal["on", "off"] = "off") -> Iterator[str]:
    """URL base da ``app`` enquanto o bloco roda; derruba o servidor no fim."""
    soquete = socket.socket()
    soquete.bind(("127.0.0.1", 0))
    # log_config=None: o uvicorn nao reconfigura o logging do processo de teste.
    servidor = uvicorn.Server(
        uvicorn.Config(app, lifespan=lifespan, log_config=None, access_log=False)
    )
    thread = threading.Thread(
        target=servidor.run, kwargs={"sockets": [soquete]}, daemon=True
    )
    thread.start()
    prazo = time.monotonic() + 10
    while not servidor.started:
        if not thread.is_alive() or time.monotonic() > prazo:
            msg = "o uvicorn de teste nao subiu"
            raise RuntimeError(msg)
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{soquete.getsockname()[1]}"
    finally:
        servidor.should_exit = True
        thread.join(timeout=10)
        soquete.close()
