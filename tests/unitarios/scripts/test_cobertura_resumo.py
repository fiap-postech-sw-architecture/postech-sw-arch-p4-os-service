"""``scripts/cobertura_resumo.py``: a evidencia de cobertura do summary do CI."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.cobertura_resumo import main, resumir  # noqa: E402

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
    caminho.write_text(_COVERAGE_XML)
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
