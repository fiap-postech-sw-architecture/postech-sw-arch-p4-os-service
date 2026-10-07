"""O que os manifestos de ``k8s/base`` combinam com o codigo, sem cluster."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador
from tests.manifestos import container, objeto

# O temporario do container: TMPDIR nao e definido na imagem.
_TMP_DO_CONTAINER = Path("/tmp")  # noqa: S108  # o /tmp do pod, nao deste processo


def test_aguarda_migracao_e_o_mesmo_nos_tres_deployments() -> None:
    especificacoes = [
        container(objeto("Deployment", f"os-service-{processo}"), "aguarda-migracao")
        for processo in ("api", "relay", "consumidor")
    ]

    assert especificacoes[1:] == [especificacoes[0]] * 2


@pytest.mark.parametrize("processo", ["relay", "consumidor"])
def test_sondas_do_processo_leem_os_arquivos_que_ele_toca(processo: str) -> None:
    sinal = Sinalizador(processo, _TMP_DO_CONTAINER)
    principal = container(objeto("Deployment", f"os-service-{processo}"), processo)

    for sonda in ("startupProbe", "livenessProbe"):
        python, opcao, codigo = principal[sonda]["exec"]["command"]
        assert (python, opcao) == ("python", "-c")
        assert f"'{sinal.heartbeat}'" in codigo
    assert principal["readinessProbe"]["exec"]["command"] == [
        "test",
        "-f",
        str(sinal.pronto),
    ]


def _vida(codigo: str) -> int:
    # Argumentos fixos: o interpretador deste venv e o codigo da sonda.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", codigo], timeout=30, check=False
    ).returncode


@pytest.mark.parametrize(
    ("idade_s", "viva"),
    [
        pytest.param(0, True, id="heartbeat-recente"),
        pytest.param(85, True, id="abaixo-de-90-s"),
        pytest.param(95, False, id="acima-de-90-s"),
        pytest.param(None, False, id="sem-heartbeat"),
    ],
)
def test_sonda_de_vida_tolera_90_s_sem_heartbeat(
    tmp_path: Path, idade_s: int | None, viva: bool
) -> None:
    padrao = container(objeto("Deployment", "os-service-relay"), "relay")
    codigo = padrao["livenessProbe"]["exec"]["command"][2]
    heartbeat = tmp_path / "relay-heartbeat"
    if idade_s is not None:
        heartbeat.touch()
        momento = time.time() - idade_s
        os.utime(heartbeat, (momento, momento))
    codigo_local = codigo.replace(
        str(Sinalizador("relay", _TMP_DO_CONTAINER).heartbeat), str(heartbeat)
    )

    assert (_vida(codigo_local) == 0) is viva
