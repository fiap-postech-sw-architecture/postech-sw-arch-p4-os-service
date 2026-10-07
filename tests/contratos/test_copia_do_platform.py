"""A copia de ``contratos/`` e a do platform no SHA de ``contratos/ORIGEM``.

AsyncAPI, schemas, exemplos e a topologia do RabbitMQ (definitions, permissoes,
init de usuarios e configuracao) sao comparados byte a byte com o tarball do
platform nesse SHA (repositorio publico), baixado uma vez, com novas
tentativas. Copia editada aqui, ou ``ORIGEM`` apontando para um SHA sem esses
arquivos, reprova o CI; a copia atrasada em relacao a ``main`` do platform se
resolve atualizando ``ORIGEM`` e copiando de novo.

Sem rede o teste falha uma vez, com o motivo. Para rodar a suite offline de
proposito: ``uv run pytest -m "not rede"``.
"""

from __future__ import annotations

import io
import tarfile
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS

if TYPE_CHECKING:
    from pathlib import Path

_TARBALL = (
    "https://codeload.github.com/fiap-postech-sw-architecture/"
    "postech-sw-arch-p4-platform/tar.gz/{sha}"
)
_TENTATIVAS = 3
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


def _platform(sha: str) -> dict[str, bytes]:
    """Arquivos do platform no ``sha``, pelo caminho a partir da raiz do repositorio."""
    falha: httpx.HTTPError | None = None
    for tentativa in range(_TENTATIVAS):
        if tentativa:
            time.sleep(2**tentativa)
        try:
            resposta = httpx.get(_TARBALL.format(sha=sha), timeout=30)
            resposta.raise_for_status()
        except httpx.HTTPError as exc:
            falha = exc
            continue
        with tarfile.open(fileobj=io.BytesIO(resposta.content), mode="r:gz") as tar:
            return {
                membro.name.split("/", 1)[1]: arquivo.read()
                for membro in tar.getmembers()
                if (arquivo := tar.extractfile(membro)) is not None
            }
    pytest.fail(
        f"sem acesso ao platform no SHA {sha} depois de {_TENTATIVAS} tentativas "
        f"({type(falha).__name__}); offline, rode com -m 'not rede'",
        pytrace=False,
    )


def test_origem_e_um_sha_completo() -> None:
    sha = _ORIGEM.read_text().strip()
    assert len(sha) == 40
    int(sha, 16)


@pytest.mark.rede
def test_copia_e_identica_a_do_platform_no_sha_de_origem() -> None:
    platform = _platform(_ORIGEM.read_text().strip())

    divergentes = [
        _relativo(copia)
        for copia in _COPIAS
        if platform.get(_no_platform(copia)) != copia.read_bytes()
    ]

    assert len(_COPIAS) > 70
    assert divergentes == []
