"""O que os manifestos de ``k8s/base`` combinam com o codigo, sem cluster."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.compartilhado.infraestrutura.mensageria.processo import (
    IDADE_MAXIMA_DO_HEARTBEAT_S,
    Sinalizador,
)
from tests.manifestos import container, objeto

# O temporario do container: TMPDIR nao e definido na imagem.
_TMP_DO_CONTAINER = Path("/tmp")  # noqa: S108  # o /tmp do pod, nao deste processo


def test_aguarda_migracao_e_o_mesmo_nos_tres_deployments() -> None:
    especificacoes = [
        container(objeto("Deployment", f"os-service-{processo}"), "aguarda-migracao")
        for processo in ("api", "relay", "consumidor")
    ]

    assert especificacoes[1:] == [especificacoes[0]] * 2
    assert especificacoes[0]["command"] == ["python", "-m", "src.banco", "aguardar"]


@pytest.mark.parametrize("processo", ["relay", "consumidor"])
def test_sondas_do_processo_leem_os_arquivos_que_ele_toca(processo: str) -> None:
    sinal = Sinalizador(processo, _TMP_DO_CONTAINER)
    principal = container(objeto("Deployment", f"os-service-{processo}"), processo)

    for sonda in ("startupProbe", "livenessProbe"):
        python, opcao, codigo = principal[sonda]["exec"]["command"]
        assert (python, opcao) == ("python", "-c")
        assert f"'{sinal.heartbeat}'" in codigo
        assert f">{IDADE_MAXIMA_DO_HEARTBEAT_S})" in codigo
    assert principal["readinessProbe"]["exec"]["command"] == [
        "test",
        "-f",
        str(sinal.pronto),
    ]


@pytest.mark.parametrize("processo", ["relay", "consumidor"])
def test_startup_do_processo_e_a_liveness(processo: str) -> None:
    # Broker fora no boot nao reinicia o pod (o pronto so existe com a conexao):
    # a startup olha a idade do heartbeat, como a liveness, e nao o pronto.
    principal = container(objeto("Deployment", f"os-service-{processo}"), processo)

    assert principal["startupProbe"]["exec"] == principal["livenessProbe"]["exec"]


def _vida(codigo: str) -> int:
    # Argumentos fixos: o interpretador deste venv e o codigo da sonda.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", codigo], timeout=30, check=False
    ).returncode


@pytest.mark.parametrize("sonda", ["startupProbe", "livenessProbe"])
@pytest.mark.parametrize("processo", ["relay", "consumidor"])
@pytest.mark.parametrize(
    ("idade_s", "viva"),
    [
        pytest.param(0, True, id="heartbeat-recente"),
        pytest.param(IDADE_MAXIMA_DO_HEARTBEAT_S - 5, True, id="abaixo-do-limite"),
        pytest.param(IDADE_MAXIMA_DO_HEARTBEAT_S + 5, False, id="acima-do-limite"),
        pytest.param(None, False, id="sem-heartbeat"),
    ],
)
def test_sonda_tolera_a_idade_maxima_do_heartbeat(
    tmp_path: Path, processo: str, sonda: str, idade_s: int | None, viva: bool
) -> None:
    padrao = container(objeto("Deployment", f"os-service-{processo}"), processo)
    codigo = padrao[sonda]["exec"]["command"][2]
    heartbeat = tmp_path / f"{processo}-heartbeat"
    if idade_s is not None:
        heartbeat.touch()
        momento = time.time() - idade_s
        os.utime(heartbeat, (momento, momento))
    codigo_local = codigo.replace(
        str(Sinalizador(processo, _TMP_DO_CONTAINER).heartbeat), str(heartbeat)
    )

    assert (_vida(codigo_local) == 0) is viva
