"""O README: os comandos dos blocos ``bash`` rodam colados no terminal."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"
# A cerca pode vir indentada: os passos numerados do README levam o bloco recuado.
_BLOCO = re.compile(
    r"^[ \t]*```(?:bash|sh)\n(.*?)^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE
)
_COMENTARIO = re.compile(r"(^|\s)#(\s|$)")


def _linhas_com_comentario(texto: str) -> list[str]:
    # No zsh interativo (o padrao do macOS) o # nao e comentario: o texto depois
    # dele vira argumento do comando, e no make vira alvo. O que o comando faz
    # fica na prosa acima do bloco.
    return [
        linha
        for bloco in _BLOCO.findall(texto)
        for linha in bloco.splitlines()
        if _COMENTARIO.search(linha)
    ]


def test_blocos_de_comando_nao_levam_comentario() -> None:
    texto = README.read_text()

    assert len(_BLOCO.findall(texto)) > 10
    assert _linhas_com_comentario(texto) == []


def test_bloco_indentado_de_passo_numerado_tambem_e_conferido() -> None:
    texto = (
        "1. Passo:\n\n"
        "   ```bash\n"
        "   kubectl get pods  # espera\n"
        "   ```\n\n"
        "```sh\n"
        "make test\n"
        "```\n"
    )

    assert _linhas_com_comentario(texto) == ["   kubectl get pods  # espera"]
