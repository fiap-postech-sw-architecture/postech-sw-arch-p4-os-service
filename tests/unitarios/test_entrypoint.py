"""Argumentos que o ``entrypoint.sh`` da imagem passa ao uvicorn."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def _argumentos_do_uvicorn(tmp_path: Path, ambiente: dict[str, str]) -> list[str]:
    # Um uvicorn falso no PATH grava os argumentos, separados por NUL (o vazio
    # tambem conta).
    gravados = tmp_path / "argumentos"
    falso = tmp_path / "uvicorn"
    falso.write_text(f"#!/bin/sh\nprintf '%s\\0' \"$@\" > '{gravados}'\n")
    falso.chmod(0o755)
    # Argumentos fixos (o bash e o script do repositorio).
    subprocess.run(  # noqa: S603
        ["/bin/bash", str(_ENTRYPOINT)],
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", **ambiente},
        capture_output=True,
        timeout=30,
        check=True,
    )
    return gravados.read_text().split("\0")[:-1]


@pytest.mark.parametrize(
    ("ambiente", "prefixo"),
    [
        pytest.param({"ROOT_PATH": "/os"}, "/os", id="atras-do-kong"),
        pytest.param({}, "", id="sem-prefixo"),
    ],
)
def test_entrypoint_passa_o_prefixo_da_borda_ao_uvicorn(
    tmp_path: Path, ambiente: dict[str, str], prefixo: str
) -> None:
    # ROOT_PATH vira --root-path (ADR-038: o Swagger atras do prefixo); sem
    # ele, o vazio e o padrao do uvicorn.
    assert _argumentos_do_uvicorn(tmp_path, ambiente) == [
        "src.main:app",
        "--host",
        "0.0.0.0",  # noqa: S104  # o que o entrypoint passa
        "--port",
        "8000",
        "--no-proxy-headers",
        "--no-server-header",
        "--root-path",
        prefixo,
    ]
