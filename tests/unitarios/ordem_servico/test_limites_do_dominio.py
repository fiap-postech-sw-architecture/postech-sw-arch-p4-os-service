"""Limites dos agregados da OS e da saga (ai/ddd/tactical.md, "God class").

No maximo 300 linhas por modulo e 10 metodos publicos que mudam estado (os
publicos que nao sao propriedade; as propriedades de leitura nao contam). Um
mutador novo na ``Saga`` ou na ``OrdemDeServico`` que passe do teto tem de sair
em outro modulo, nao subir o limite aqui.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

MAXIMO_DE_LINHAS = 300
MAXIMO_DE_METODOS = 10
RAIZ = Path(__file__).parents[3] / "src" / "ordem_servico"
MODULOS = sorted(
    [
        *(RAIZ / "aplicacao" / "saga").glob("*.py"),
        RAIZ / "dominio" / "ordem_de_servico.py",
    ]
)


def _metodos_publicos(classe: type) -> list[str]:
    """Os metodos publicos da propria classe que nao sao propriedade."""
    return sorted(
        nome
        for nome, membro in vars(classe).items()
        if not nome.startswith("_")
        and not isinstance(membro, property)
        and inspect.isroutine(getattr(classe, nome))
    )


@pytest.mark.parametrize("modulo", MODULOS, ids=lambda m: m.name)
def test_modulo_tem_no_maximo_300_linhas(modulo: Path) -> None:
    assert len(modulo.read_text().splitlines()) <= MAXIMO_DE_LINHAS


@pytest.mark.parametrize("classe", [Saga, OrdemDeServico], ids=lambda c: c.__name__)
def test_agregado_tem_no_maximo_10_metodos_publicos_que_mudam_estado(
    classe: type,
) -> None:
    assert len(_metodos_publicos(classe)) <= MAXIMO_DE_METODOS, _metodos_publicos(
        classe
    )
