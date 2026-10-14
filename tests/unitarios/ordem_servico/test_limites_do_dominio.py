"""Limites dos agregados da OS e da saga (ai/ddd/tactical.md, "God class").

No maximo 300 linhas por modulo e 10 metodos publicos que mudam estado (os
publicos que nao sao propriedade, da classe e dos mixins de leitura do OS; as
propriedades de leitura nao contam). Um mutador novo na ``Saga`` ou na
``OrdemDeServico``, ou num mixin, que passe do teto tem de sair em outro modulo,
nao subir o limite aqui.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Final

import pytest

from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

MAXIMO_DE_LINHAS = 300
MAXIMO_DE_METODOS = 10
# As consultas dos mixins de leitura (nao mudam estado): ficam fora da conta. Um
# metodo novo num mixin entra na conta, a menos que se liste aqui por ser consulta.
CONSULTAS: Final = frozenset({"classificar", "primeira_compensacao_do_cancelamento"})
RAIZ = Path(__file__).parents[3] / "src" / "ordem_servico"
MODULOS = sorted(
    [
        *(RAIZ / "aplicacao" / "saga").glob("*.py"),
        RAIZ / "dominio" / "ordem_de_servico.py",
    ]
)


def _publicos(classe: type) -> set[str]:
    """Os metodos publicos da classe e dos mixins do OS, sem a base compartilhada."""
    return {
        nome
        for base in classe.__mro__
        if base.__module__.startswith("src.ordem_servico")
        for nome, membro in vars(base).items()
        if not nome.startswith("_")
        and not isinstance(membro, property)
        and inspect.isroutine(getattr(classe, nome))
    }


def _metodos_publicos(classe: type) -> list[str]:
    """Os publicos que mudam estado: os da classe e dos mixins, sem as consultas."""
    return sorted(_publicos(classe) - CONSULTAS)


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


def test_a_lista_de_consultas_so_tem_metodos_que_existem() -> None:
    """Um nome velho na lista deixaria um mutador futuro de mesmo nome sem conta."""
    assert _publicos(Saga) | _publicos(OrdemDeServico) >= CONSULTAS
