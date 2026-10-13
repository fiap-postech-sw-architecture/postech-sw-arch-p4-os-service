"""O README: os comandos dos blocos ``bash`` rodam colados no terminal."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"
_BLOCO = re.compile(r"^```(?:bash|sh)\n(.*?)^```", re.DOTALL | re.MULTILINE)
_COMENTARIO = re.compile(r"(^|\s)#(\s|$)")


def test_blocos_de_comando_nao_levam_comentario() -> None:
    # No zsh interativo (o padrao do macOS) o # nao e comentario: o texto depois
    # dele vira argumento do comando, e no make vira alvo. O que o comando faz
    # fica na prosa acima do bloco.
    blocos = _BLOCO.findall(README.read_text())
    com_comentario = [
        linha
        for bloco in blocos
        for linha in bloco.splitlines()
        if _COMENTARIO.search(linha)
    ]

    assert len(blocos) > 5
    assert com_comentario == []
