"""``scripts/cobertura_resumo.py``: a cobertura no summary do CI e no README."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.cobertura_resumo import (
    MARCADOR_FIM,
    MARCADOR_INICIO,
    Medida,
    atualizar_readme,
    main,
    medir,
    resumir,
    secao_do_readme,
)

# Dois pacotes: ordem_servico/dominio com 3 de 4 linhas, autenticacao/aplicacao
# com 1 de 1; 2 de 4 ramos.
_COVERAGE_XML = """<?xml version="1.0" ?>
<coverage lines-valid="5" lines-covered="4" line-rate="0.8"
          branches-valid="4" branches-covered="2" branch-rate="0.5">
  <sources><source>/repo/src</source></sources>
  <packages><package name="x"><classes>
    <class filename="ordem_servico/dominio/status.py"><lines>
      <line number="1" hits="1"/><line number="2" hits="3"/>
      <line number="3" hits="1"/><line number="4" hits="0"/>
    </lines></class>
    <class filename="autenticacao/aplicacao/use_cases.py"><lines>
      <line number="1" hits="2"/>
    </lines></class>
  </classes></package></packages>
</coverage>
"""


@pytest.fixture
def coverage_xml(tmp_path: Path) -> Path:
    caminho = tmp_path / "coverage.xml"
    caminho.write_text(_COVERAGE_XML, encoding="utf-8")
    return caminho


def test_resumo_por_contexto_e_camada_com_o_numero_do_gate(
    coverage_xml: Path,
) -> None:
    texto = resumir(coverage_xml)

    # Gate: (4 linhas + 2 ramos) / (5 + 4) = 66,7%.
    assert texto.startswith("### Cobertura de testes: 66.7% no gate (linhas e ramos)")
    assert "80.0% de linhas e 50.0% de ramos." in texto
    assert "| `src/autenticacao/aplicacao` | 1 | 1 | 100.0% |" in texto
    assert "| `src/ordem_servico/dominio` | 4 | 3 | 75.0% |" in texto
    assert texto.endswith("| **total** | 5 | 4 | 80.0% |\n")


def test_main_escreve_o_resumo(
    coverage_xml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cobertura_resumo.py", str(coverage_xml)]) == 0
    assert "66.7% no gate" in capsys.readouterr().out


def test_coverage_ausente_vira_aviso_sem_falhar_o_passo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # O passo roda com `if: always()`: suite que falhou antes nao gera o xml.
    assert main(["cobertura_resumo.py", str(tmp_path / "coverage.xml")]) == 0
    assert "nao foi gerado (a suite falhou antes)" in capsys.readouterr().out


def test_sem_argumento_mostra_o_uso(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cobertura_resumo.py"]) == 2
    assert "uso:" in capsys.readouterr().err


_RUN = "https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service/actions/runs/39104220173"
_PROCESSO = """    <class filename="main.py"><lines>
      <line number="1" hits="1"/><line number="2" hits="0"/>
    </lines></class>
  </classes>"""


def test_medir_separa_contexto_camada_e_os_processos_da_raiz(tmp_path: Path) -> None:
    caminho = tmp_path / "coverage.xml"
    caminho.write_text(
        _COVERAGE_XML.replace("  </classes>", _PROCESSO), encoding="utf-8"
    )

    medida = medir(caminho)

    assert medida == Medida(
        contextos={
            "`src/ordem_servico`": (4, 3),
            "`src/autenticacao`": (1, 1),
            "`src` (processos)": (2, 1),
        },
        camadas={"`dominio`": (4, 3), "`aplicacao`": (1, 1)},
        ramos=(4, 2),
    )


def test_secao_do_readme_em_portugues_com_o_link_do_run(tmp_path: Path) -> None:
    caminho = tmp_path / "coverage.xml"
    caminho.write_text(
        _COVERAGE_XML.replace("  </classes>", _PROCESSO), encoding="utf-8"
    )

    secao = secao_do_readme(medir(caminho), _RUN)

    # Gate: (5 linhas + 2 ramos) / (7 + 4) = 63,64%; os processos primeiro e as
    # camadas de dentro para fora (dominio primeiro).
    assert secao == (
        "**63,64% no gate** (linhas e ramos somados): 7 linhas, 5 cobertas "
        "(71,4%), e 4 ramos, 2 cobertos (50,0%), medidos no "
        f"[run 39104220173 do CI]({_RUN}). Tabelas geradas por "
        "`scripts/cobertura_resumo.py` a partir do `coverage.xml` desse run.\n"
        "\n"
        "| Contexto | Linhas | Cobertas | Cobertura |\n"
        "|---|---:|---:|---:|\n"
        "| `src` (processos) | 2 | 1 | 50,0% |\n"
        "| `src/autenticacao` | 1 | 1 | 100,0% |\n"
        "| `src/ordem_servico` | 4 | 3 | 75,0% |\n"
        "| **total** | 7 | 5 | 71,4% |\n"
        "\n"
        "| Camada | Linhas | Cobertas | Cobertura |\n"
        "|---|---:|---:|---:|\n"
        "| `dominio` | 4 | 3 | 75,0% |\n"
        "| `aplicacao` | 1 | 1 | 100,0% |\n"
    )


def test_milhar_com_ponto_na_secao_do_readme() -> None:
    medida = Medida(
        contextos={"`src/ordem_servico`": (2699, 2699)},
        camadas={"`dominio`": (1250, 1249)},
        ramos=(1000, 999),
    )

    secao = secao_do_readme(medida, _RUN)

    assert "2.699 linhas, 2.699 cobertas (100,0%), e 1.000 ramos, 999" in secao
    assert "| `dominio` | 1.250 | 1.249 | 99,9% |" in secao


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/fiap-postech-sw-architecture/x/actions/runs/",
        "https://github.com/o/r/actions/runs/123/job/456",
        "http://exemplo.com/actions/runs/123",
    ],
    ids=["run-sem-numero", "job-dentro-do-run", "fora-do-github"],
)
def test_secao_do_readme_so_aceita_o_endereco_de_um_run(url: str) -> None:
    with pytest.raises(ValueError, match="endereco de um run do CI"):
        secao_do_readme(Medida(contextos={}, camadas={}, ramos=(0, 0)), url)


def _argv(xml: Path, readme: Path) -> list[str]:
    return ["cobertura_resumo.py", str(xml), "--readme", str(readme), "--run", _RUN]


def _readme(tmp_path: Path, meio: str) -> Path:
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Servico\n\n## Cobertura\n\n{meio}\nPara refazer.\n", encoding="utf-8"
    )
    return readme


def test_main_reescreve_so_o_trecho_entre_os_marcadores(
    coverage_xml: Path, tmp_path: Path
) -> None:
    readme = _readme(tmp_path, f"{MARCADOR_INICIO}\nvelha\n{MARCADOR_FIM}")

    codigo = main(_argv(coverage_xml, readme))

    assert codigo == 0
    assert readme.read_text(encoding="utf-8") == (
        "# Servico\n\n## Cobertura\n\n"
        f"{MARCADOR_INICIO}\n{secao_do_readme(medir(coverage_xml), _RUN)}\n"
        f"{MARCADOR_FIM}\nPara refazer.\n"
    )


@pytest.mark.parametrize(
    "meio",
    [
        "sem marcador",
        f"{MARCADOR_FIM}\n{MARCADOR_INICIO}",
        f"{MARCADOR_INICIO}\n{MARCADOR_FIM}\n{MARCADOR_INICIO}\n{MARCADOR_FIM}",
    ],
    ids=["ausente", "invertido", "repetido"],
)
def test_marcador_ausente_repetido_ou_invertido_reprova_sem_tocar_no_readme(
    coverage_xml: Path, tmp_path: Path, meio: str, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _readme(tmp_path, meio)
    antes = readme.read_text(encoding="utf-8")

    codigo = main(_argv(coverage_xml, readme))

    assert codigo == 1
    assert readme.read_text(encoding="utf-8") == antes
    assert "cobertura:" in capsys.readouterr().err
    with pytest.raises(ValueError, match="cobertura:"):
        atualizar_readme(antes, "x\n")


def test_coverage_ausente_reprova_a_reescrita_do_readme(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _readme(tmp_path, f"{MARCADOR_INICIO}\n{MARCADOR_FIM}")
    xml = tmp_path / "coverage.xml"

    codigo = main(_argv(xml, readme))

    assert codigo == 1
    assert "baixe o coverage.xml do run do CI" in capsys.readouterr().err
