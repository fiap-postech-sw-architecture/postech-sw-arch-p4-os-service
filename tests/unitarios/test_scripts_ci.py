"""Scripts do CI (``scripts/ci/``): o relatorio das imagens de terceiros dos
manifestos, que nunca reprova o job."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
RELATORIO_TERCEIROS = RAIZ / "scripts" / "ci" / "relatorio-trivy-terceiros.sh"

# O trivy de mentira repete o do CI: o relatorio sai no stdout e o codigo e 1
# quando ha achado, salvo com --exit-code 0; a imagem de FALHA_EM nao chega a
# ser varrida (pull recusado), e o comando sai com 1 mesmo com --exit-code 0.
TRIVY_FALSO = """#!/usr/bin/env bash
echo "trivy $*" >> "$TRIVY_LOG"
imagem="${*: -1}"
if [ -n "$TRIVY_FALHA_EM" ] && [[ "$imagem" == *"$TRIVY_FALHA_EM"* ]]; then
  echo "FATAL pull recusado: $imagem" >&2
  exit 1
fi
echo "RELATORIO $imagem: Total: 3 (HIGH: 3)"
case " $* " in *" --exit-code 0 "*) exit 0 ;; esac
exit 1
"""


def _relatar(
    tmp_path: Path,
    imagens: list[str],
    falha_em: str = "",
    manifestos: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Roda o relatorio sobre ``manifestos`` (por padrao, um diretorio com
    ``imagens``) e o trivy de mentira; devolve o processo, o summary e o log de
    chamadas."""
    if manifestos is None:
        manifestos = tmp_path / "manifestos"
        manifestos.mkdir()
        linhas = "".join(
            f"        - name: c{i}\n          image: {imagem}\n"
            for i, imagem in enumerate(imagens)
        )
        (manifestos / "deployment.yaml").write_text(f"containers:\n{linhas}")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "trivy").write_text(TRIVY_FALSO)
    (bin_ / "trivy").chmod(0o755)
    resumo, log = tmp_path / "resumo.md", tmp_path / "trivy.log"
    resultado = subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(RELATORIO_TERCEIROS), str(manifestos)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(resumo),
            "TRIVY_LOG": str(log),
            "TRIVY_FALHA_EM": falha_em,
        },
    )
    return (
        resultado,
        resumo.read_text() if resumo.exists() else "",
        log.read_text() if log.exists() else "",
    )


def test_achado_do_trivy_nao_reprova_e_entra_no_relatorio(tmp_path: Path) -> None:
    resultado, resumo, chamadas = _relatar(
        tmp_path, ["pytstop-os-service", "terceiro/a:1", "terceiro/b:2"]
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "RELATORIO terceiro/a:1" in resumo
    assert "RELATORIO terceiro/b:2" in resumo
    assert "falhou" not in resumo
    assert "pytstop-os-service" not in chamadas


def test_falha_da_varredura_vira_aviso_e_nao_reprova(tmp_path: Path) -> None:
    resultado, resumo, _ = _relatar(
        tmp_path, ["terceiro/a:1", "terceiro/b:2"], falha_em="terceiro/a"
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "terceiro/a:1: varredura falhou (codigo 1)" in resumo
    assert "::warning::" in resultado.stdout
    # A falha de uma imagem nao impede o relatorio da seguinte.
    assert "RELATORIO terceiro/b:2" in resumo


def test_todas_as_varreduras_falhando_tambem_nao_reprova(tmp_path: Path) -> None:
    resultado, resumo, _ = _relatar(
        tmp_path, ["terceiro/a:1", "terceiro/b:2"], falha_em="terceiro"
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resumo.count("varredura falhou") == 2


def test_sem_imagem_de_terceiro_reprova_porque_o_relatorio_nao_prova_nada(
    tmp_path: Path,
) -> None:
    resultado, resumo, chamadas = _relatar(tmp_path, ["pytstop-os-service"])

    assert resultado.returncode == 1
    assert "nenhuma imagem de terceiro" in resultado.stderr
    assert resumo == chamadas == ""


def test_os_manifestos_do_repositorio_trazem_o_banco_e_o_exporter(
    tmp_path: Path,
) -> None:
    # As duas imagens de terceiros que o servico poe no cluster: o PostgreSQL
    # (no StatefulSet e no Job) e o exporter; a do proprio servico fica de fora.
    resultado, _, chamadas = _relatar(tmp_path, [], manifestos=RAIZ / "k8s" / "base")

    assert resultado.returncode == 0, resultado.stderr
    varridas = [linha.split()[-1] for linha in chamadas.splitlines()]
    assert varridas == [
        "postgres:16.15",
        "prometheuscommunity/postgres-exporter:v0.20.1",
    ]
