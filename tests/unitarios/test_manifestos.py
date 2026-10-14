"""O que os manifestos de ``k8s/base`` combinam com o codigo, sem cluster."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src.compartilhado.infraestrutura.mensageria.processo import (
    IDADE_MAXIMA_DO_HEARTBEAT_DOS_PRAZOS_S,
    IDADE_MAXIMA_DO_HEARTBEAT_S,
    Sinalizador,
)
from tests.manifestos import RAIZ, container, objeto

if TYPE_CHECKING:
    from collections.abc import Callable

# O temporario do container: TMPDIR nao e definido na imagem.
_TMP_DO_CONTAINER = Path("/tmp")  # noqa: S108  # o /tmp do pod, nao deste processo
# A idade maxima do heartbeat que a liveness de cada processo aceita.
IDADE_MAXIMA = {
    "relay": IDADE_MAXIMA_DO_HEARTBEAT_S,
    "consumidor": IDADE_MAXIMA_DO_HEARTBEAT_S,
    "prazos": IDADE_MAXIMA_DO_HEARTBEAT_DOS_PRAZOS_S,
}


def test_aguarda_migracao_e_o_mesmo_nos_deployments_do_servico() -> None:
    especificacoes = [
        container(objeto("Deployment", f"os-service-{processo}"), "aguarda-migracao")
        for processo in ("api", "relay", "consumidor", "prazos")
    ]

    assert especificacoes[1:] == [especificacoes[0]] * 3
    assert especificacoes[0]["command"] == ["python", "-m", "src.banco", "aguardar"]


@pytest.mark.parametrize("processo", ["relay", "consumidor", "prazos"])
def test_sondas_do_processo_leem_os_arquivos_que_ele_toca(processo: str) -> None:
    sinal = Sinalizador(processo, _TMP_DO_CONTAINER)
    principal = container(objeto("Deployment", f"os-service-{processo}"), processo)

    for sonda in ("startupProbe", "livenessProbe"):
        python, opcao, codigo = principal[sonda]["exec"]["command"]
        assert (python, opcao) == ("python", "-c")
        assert f"'{sinal.heartbeat}'" in codigo
        assert f">{IDADE_MAXIMA[processo]})" in codigo
    assert principal["readinessProbe"]["exec"]["command"] == [
        "test",
        "-f",
        str(sinal.pronto),
    ]


@pytest.mark.parametrize("processo", ["relay", "consumidor", "prazos"])
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
@pytest.mark.parametrize("processo", ["relay", "consumidor", "prazos"])
@pytest.mark.parametrize(
    ("idade_do", "viva"),
    [
        pytest.param(lambda limite: 0, True, id="heartbeat-recente"),
        pytest.param(lambda limite: limite - 5, True, id="abaixo-do-limite"),
        pytest.param(lambda limite: limite + 5, False, id="acima-do-limite"),
        pytest.param(lambda limite: None, False, id="sem-heartbeat"),
    ],
)
def test_sonda_tolera_a_idade_maxima_do_heartbeat(
    tmp_path: Path,
    processo: str,
    sonda: str,
    idade_do: Callable[[int], int | None],
    viva: bool,
) -> None:
    # A idade e em relacao ao limite do processo (90 s; 120 s no prazos).
    idade_s = idade_do(IDADE_MAXIMA[processo])
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


def test_sem_kubectl_no_path_o_render_encerra_a_sessao_uma_vez_com_o_motivo(
    tmp_path: Path,
) -> None:
    # PATH sem o kubectl (um diretorio vazio): sem a fixture, cada teste do render
    # cairia com um FileNotFoundError cru.
    resultado = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unitarios/test_contrato_dos_manifestos.py",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
        ],
        cwd=RAIZ,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    saida = resultado.stdout + resultado.stderr

    assert resultado.returncode == 1
    assert saida.count("kubectl nao esta no PATH") == 1
    assert "FileNotFoundError" not in saida
