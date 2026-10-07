"""A copia de ``contratos/`` e a do platform no SHA de ``contratos/ORIGEM``.

AsyncAPI, schemas, exemplos e a topologia do RabbitMQ (definitions, permissoes,
init de usuarios e configuracao) sao baixados pelo raw do GitHub (repositorio
publico) e comparados byte a byte. Copia editada aqui, ou ``ORIGEM`` apontando
para um SHA sem esses arquivos, reprova o CI; a copia atrasada em relacao a
``main`` do platform se resolve atualizando ``ORIGEM`` e copiando de novo.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import httpx
import pytest

from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_RAW = (
    "https://raw.githubusercontent.com/fiap-postech-sw-architecture/"
    "postech-sw-arch-p4-platform"
)
_ORIGEM = CONTRATOS / "ORIGEM"
_COPIAS = sorted(p for p in CONTRATOS.rglob("*") if p.is_file() and p != _ORIGEM)


def _relativo(copia: Path) -> str:
    return copia.relative_to(CONTRATOS).as_posix()


def _no_platform(copia: Path) -> str:
    """Caminho do arquivo no platform: a topologia vem de k8s/ e do compose."""
    relativo = _relativo(copia)
    if not relativo.startswith("rabbitmq/"):
        return f"contratos/{relativo}"
    if copia.name == "rabbitmq-admin.json":
        return "compose/rabbitmq-admin.json"
    return f"k8s/base/rabbitmq/{copia.name}"


@pytest.fixture(scope="module")
def platform() -> Iterator[httpx.Client]:
    sha = _ORIGEM.read_text().strip()
    with httpx.Client(base_url=f"{_RAW}/{sha}", timeout=15) as cliente:
        yield cliente


def test_origem_e_um_sha_completo() -> None:
    sha = _ORIGEM.read_text().strip()
    assert len(sha) == 40
    int(sha, 16)


@pytest.mark.parametrize("copia", _COPIAS, ids=_relativo)
def test_copia_e_identica_a_do_platform_no_sha_de_origem(
    platform: httpx.Client, copia: Path
) -> None:
    resposta = platform.get(f"/{_no_platform(copia)}")
    resposta.raise_for_status()
    assert hashlib.sha256(resposta.content).hexdigest() == (
        hashlib.sha256(copia.read_bytes()).hexdigest()
    )
