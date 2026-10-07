"""``scripts/validar_token.py``: o validador independente, rodado como o smoke o roda.

O script e a prova de que Billing e Execucao validam o token sem codigo do OS:
so o PyJWT e a biblioteca padrao, e o JWKS buscado por HTTP.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from src.main import criar_app
from tests.chaves_jwt import CHAVE_PEM, OUTRA_CHAVE, assinar, jwt_service
from tests.servidor_http import servir

if TYPE_CHECKING:
    from collections.abc import Iterator

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "validar_token.py"
# Dependencias que o script pode importar: nada de `src` nem de `tests`.
_IMPORTS_PERMITIDOS = {"__future__", "sys", "jwt"}


@pytest.fixture(autouse=True)
def _chave_no_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_PRIVATE_KEY", CHAVE_PEM)
    monkeypatch.delenv("JWT_PREVIOUS_PUBLIC_KEY", raising=False)


@pytest.fixture(scope="module")
def url_base() -> Iterator[str]:
    with servir(criar_app()) as url:
        yield url


def _rodar(url: str, token: str) -> subprocess.CompletedProcess[str]:
    # Argumentos fixos (o interpretador e o script do repositorio); o token vai
    # pela entrada padrao, como no `make smoke`.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_SCRIPT), url],
        input=token,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _modulos_importados() -> set[str]:
    modulos: set[str] = set()
    for no in ast.walk(ast.parse(_SCRIPT.read_text())):
        if isinstance(no, ast.Import):
            modulos |= {alias.name.split(".")[0] for alias in no.names}
        elif isinstance(no, ast.ImportFrom) and no.module:
            modulos.add(no.module.split(".")[0])
    return modulos


def test_importa_so_a_biblioteca_padrao_e_o_pyjwt() -> None:
    assert _modulos_importados() <= _IMPORTS_PERMITIDOS


def test_cli_aceita_o_access_token_e_diz_o_papel(url_base: str) -> None:
    token = jwt_service().gerar_access_token(uuid4(), "mecanico")

    resultado = _rodar(url_base, f"{token}\n")

    assert resultado.returncode == 0
    assert resultado.stdout == "access token valido pelo JWKS: papel mecanico\n"
    assert token not in resultado.stdout + resultado.stderr


def test_cli_recusa_o_refresh_sem_imprimir_o_token(url_base: str) -> None:
    token = jwt_service().gerar_refresh_token(uuid4())

    resultado = _rodar(url_base, token)

    assert resultado.returncode == 1
    assert "o token nao e um access token" in resultado.stderr
    assert token not in resultado.stdout + resultado.stderr


def test_cli_recusa_token_de_outra_chave(url_base: str) -> None:
    resultado = _rodar(url_base, assinar(chave=OUTRA_CHAVE))

    assert resultado.returncode == 1


def test_cli_com_o_jwks_fora_do_ar_falha() -> None:
    token = jwt_service().gerar_access_token(uuid4(), "admin")

    resultado = _rodar("http://127.0.0.1:1", token)

    assert resultado.returncode == 1
