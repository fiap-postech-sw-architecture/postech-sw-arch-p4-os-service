"""Scripts do CI (``scripts/ci/``): a conferencia que estreita a excecao do
trivy (KSV-0109) ao ``PASSWORD`` lido do ambiente, o relatorio das imagens de
terceiros dos manifestos, que nunca reprova o job, o registro da saida do
pip-audit e do trivy no summary e os nomes das variaveis do ``make
kind-deploy``."""

from __future__ import annotations

import os
import select
import subprocess
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[2]
CONFERENCIA_DE_SENHAS = RAIZ / "scripts" / "ci" / "confere-senhas-do-papeis.sh"
RELATORIO_TERCEIROS = RAIZ / "scripts" / "ci" / "relatorio-trivy-terceiros.sh"
REGISTRA_NO_SUMMARY = RAIZ / "scripts" / "ci" / "registra-no-summary.sh"


def _conferir(arquivo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(CONFERENCIA_DE_SENHAS), str(arquivo)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _script_de_papeis(tmp_path: Path, *linhas: str) -> Path:
    arquivo = tmp_path / "papeis.sql"
    arquivo.write_text(
        "\\getenv senha_app POSTGRES_APP_PASSWORD\n" + "\n".join(linhas) + "\n"
    )
    return arquivo


def test_o_papeis_sql_do_repositorio_so_le_senha_do_ambiente() -> None:
    resultado = _conferir(RAIZ / "k8s" / "base" / "papeis.sql")

    assert resultado.returncode == 0, resultado.stderr


def test_o_make_manifests_roda_a_conferencia_dos_passwords() -> None:
    # Sem esta chamada a conferencia existiria e o trivy seguiria deixando passar
    # um PASSWORD literal no ConfigMap do papeis.sql.
    receita = (RAIZ / "Makefile").read_text().split("\nmanifests:\n", 1)[1]

    assert receita.startswith("\t@scripts/ci/confere-senhas-do-papeis.sh\n")


@pytest.mark.parametrize(
    "linha",
    [
        "CREATE ROLE os_app LOGIN PASSWORD '0f1e2d3c4b5a69788796a5b4c3d2e1f0';",
        "CREATE ROLE os_app LOGIN password '0f1e';",
        "ALTER ROLE os_app ENCRYPTED PASSWORD 'abc';",
        "CREATE ROLE os_app LOGIN PASSWORD\n  'quebrada-em-duas-linhas';",
        "CREATE ROLE os_app LOGIN PASSWORD NULL;",
        "CREATE ROLE os_app LOGIN PASSWORD ':senha_app';",
        "CREATE ROLE os_app LOGIN PASSWORD :'senha_app' || 'x';",
    ],
    ids=[
        "literal",
        "minusculo",
        "encrypted",
        "literal-na-linha-seguinte",
        "null",
        "variavel-entre-aspas",
        "variavel-concatenada",
    ],
)
def test_password_que_nao_vem_do_psql_reprova(tmp_path: Path, linha: str) -> None:
    resultado = _conferir(_script_de_papeis(tmp_path, linha))

    assert resultado.returncode == 1
    assert "PASSWORD sem a variavel do psql" in resultado.stderr


def test_variavel_que_o_script_nao_le_com_getenv_reprova(tmp_path: Path) -> None:
    resultado = _conferir(
        _script_de_papeis(tmp_path, "CREATE ROLE os_app LOGIN PASSWORD :'senha_dono';")
    )

    assert resultado.returncode == 1
    assert "senha_dono nao e lida com \\getenv" in resultado.stderr


def test_password_na_variavel_lida_do_ambiente_e_no_comentario_passa(
    tmp_path: Path,
) -> None:
    arquivo = _script_de_papeis(
        tmp_path,
        "-- PASSWORD '0f1e' num comentario nao conta",
        "CREATE ROLE os_app LOGIN PASSWORD :'senha_app';  -- PASSWORD 'outra'",
        "ALTER ROLE os_app\n  PASSWORD :'senha_app';",
    )

    assert _conferir(arquivo).returncode == 0


def test_script_sem_password_reprova_porque_nao_prova_nada(tmp_path: Path) -> None:
    resultado = _conferir(_script_de_papeis(tmp_path, "SELECT 1;"))

    assert resultado.returncode == 1
    assert "nenhum PASSWORD" in resultado.stderr


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


def _registrar(
    tmp_path: Path, *comando: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    resumo = tmp_path / "resumo.md"
    resultado = subprocess.run(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(REGISTRA_NO_SUMMARY), "pip-audit", *comando],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(resumo)},
    )
    return resultado, resumo.read_text() if resumo.exists() else ""


def test_comando_que_passa_vai_para_o_log_e_para_o_summary(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path, "echo", "No known vulnerabilities found")

    assert resultado.returncode == 0
    assert resultado.stdout == "No known vulnerabilities found\n"
    assert resumo == (
        "### pip-audit: sem achado\n```text\nNo known vulnerabilities found\n```\n"
    )


def test_comando_que_reprova_registra_a_saida_e_mantem_o_codigo(
    tmp_path: Path,
) -> None:
    resultado, resumo = _registrar(
        tmp_path, "/bin/sh", "-c", "echo achado-no-stdout; echo erro >&2; exit 3"
    )

    assert resultado.returncode == 3
    assert "achado-no-stdout" in resultado.stdout
    assert resumo.startswith("### pip-audit: reprovou (codigo 3)\n```text\n")
    assert "achado-no-stdout" in resumo
    assert "erro" in resumo


def test_a_saida_aparece_no_log_enquanto_o_comando_roda(tmp_path: Path) -> None:
    """Com o comando ainda de pe (ele espera um arquivo que so o teste cria), a
    primeira linha ja tem de estar no log: trivy baixando o banco ou um
    pip-audit pendurado precisam mostrar o que fazem ao vivo."""
    fim = tmp_path / "fim"
    espera = f"echo comecou; until [ -e {fim} ]; do sleep 0.1; done; echo terminou"
    processo = subprocess.Popen(  # noqa: S603 - o script do proprio repositorio
        ["/bin/bash", str(REGISTRA_NO_SUMMARY), "ao vivo", "/bin/sh", "-c", espera],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(tmp_path / "resumo.md")},
    )
    assert processo.stdout is not None
    try:
        pronto, _, _ = select.select([processo.stdout], [], [], 10)
        primeira = processo.stdout.readline() if pronto else ""
        fim.touch()
        resto, _ = processo.communicate(timeout=10)
    finally:
        processo.kill()

    assert primeira == "comecou\n"
    assert resto == "terminou\n"
    assert processo.returncode == 0


def test_comando_inexistente_reprova_e_aparece_no_summary(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path, "comando-que-nao-existe-aqui")

    assert resultado.returncode == 127
    assert "reprovou (codigo 127)" in resumo


def test_sem_comando_o_script_recusa(tmp_path: Path) -> None:
    resultado, resumo = _registrar(tmp_path)

    assert resultado.returncode == 64
    assert "uso:" in resultado.stderr
    assert resumo == ""


@pytest.mark.parametrize(
    ("job", "ferramenta", "titulo"),
    [
        ("pip-audit", "uvx pip-audit", "dependencias de runtime"),
        ("trivy", "trivy image", "HIGH e CRITICAL com correcao"),
    ],
)
def test_o_security_yml_registra_o_pip_audit_e_o_trivy_no_summary(
    job: str, ferramenta: str, titulo: str
) -> None:
    """O titulo do trivy diz o filtro do passo: sem ele, "sem achado" leria como
    "sem CVE nenhuma"."""
    fluxo = yaml.safe_load(
        (RAIZ / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    )
    comandos = [
        " ".join(passo["run"].split())
        for passo in fluxo["jobs"][job]["steps"]
        if ferramenta in passo.get("run", "")
    ]

    assert len(comandos) == 1
    assert comandos[0].startswith("scripts/ci/registra-no-summary.sh ")
    assert titulo in comandos[0].split(ferramenta)[0]


PADRAO = "../postech-sw-arch-p4-platform"


@pytest.mark.parametrize(
    ("variaveis", "platform", "overlay"),
    [
        ([], PADRAO, "kind"),
        (["KIND_OVERLAY=kind-ci"], PADRAO, "kind-ci"),
        (["PLATFORM_DIR=/clone/platform"], "/clone/platform", "kind"),
        (["PLATFORM=/clone/antigo"], "/clone/antigo", "kind"),
        (["PLATFORM=/clone/antigo", "PLATFORM_DIR=/clone/novo"], "/clone/novo", "kind"),
    ],
    ids=[
        "padroes",
        "kind-overlay",
        "platform-dir",
        "nome-antigo",
        "novo-vence-o-antigo",
    ],
)
def test_kind_deploy_aceita_platform_dir_e_kind_overlay(
    variaveis: list[str], platform: str, overlay: str
) -> None:
    """``PLATFORM_DIR`` e ``KIND_OVERLAY`` sao os nomes dos tres servicos;
    ``PLATFORM``, o nome antigo do clone, segue valendo quando ``PLATFORM_DIR``
    nao e dado."""
    # Sem as variaveis do desenvolvedor (nem as que o make pai passa em
    # MAKEFLAGS, como em `make check PLATFORM=...`), e como make filho
    # (MAKELEVEL), que e como o CI roda: o make 4 avisa o diretorio.
    ambiente = {
        chave: valor
        for chave, valor in os.environ.items()
        if chave not in {"PLATFORM", "PLATFORM_DIR", "KIND_OVERLAY", "MAKEFLAGS"}
    }
    resultado = subprocess.run(  # noqa: S603 - o Makefile do proprio repositorio
        ["make", "-n", "--no-print-directory", "kind-deploy", *variaveis],  # noqa: S607
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={**ambiente, "MAKELEVEL": "1"},
    )

    assert resultado.returncode == 0, resultado.stderr
    comando = " ".join(resultado.stdout.replace("\\\n", " ").split())
    assert comando.startswith(f"{platform}/scripts/ci/implantar-servicos.sh")
    assert f" --overlay {overlay} os-service=" in comando
