"""Texto livre do atendimento: o que a descricao e o motivo aceitam e recusam."""

from __future__ import annotations

import pytest

from src.compartilhado.dominio.exceptions import ValorInvalidoException
from src.ordem_servico.dominio.texto_livre import texto_obrigatorio

_LIMITE = 20


def _validar(valor: str) -> str:
    return texto_obrigatorio(valor, "texto", _LIMITE)


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        pytest.param("  freio rangendo  ", "freio rangendo", id="aparado"),
        pytest.param("freio\r\nrangendo", "freio\nrangendo", id="crlf-vira-lf"),
        pytest.param("freio\n\tdireito", "freio\n\tdireito", id="quebra-e-tab"),
        pytest.param("ação rápida ç", "ação rápida ç", id="acentos"),
        pytest.param("x" * _LIMITE, "x" * _LIMITE, id="no-limite"),
        pytest.param("freio 🔧", "freio 🔧", id="emoji-simples"),
    ],
)
def test_texto_valido_passa_aparado(valor: str, esperado: str) -> None:
    assert _validar(valor) == esperado


@pytest.mark.parametrize(
    ("valor", "erro"),
    [
        pytest.param("", "obrigatorio", id="vazio"),
        pytest.param("   \n ", "obrigatorio", id="so-espacos"),
        pytest.param("x" * (_LIMITE + 1), "excede", id="acima-do-limite"),
        pytest.param("a\x00b", "controle", id="nul"),
        pytest.param("a\x1bb", "controle", id="escape"),
        pytest.param("a\rb", "controle", id="cr-solto"),
        pytest.param("a\x85b", "controle", id="c1"),
        pytest.param("a\u202eb", "controle", id="override-bidirecional"),
        pytest.param("a\u2066b", "controle", id="isolamento-bidirecional"),
        pytest.param("a\u200bb", "controle", id="espaco-de-largura-zero"),
        pytest.param("a\u200db", "controle", id="juncao-de-largura-zero"),
        pytest.param("a\ufeffb", "controle", id="marca-de-ordem-de-bytes"),
        pytest.param("a\u00adb", "controle", id="hifen-condicional"),
        pytest.param("a\u2028b", "controle", id="separador-de-linha"),
        pytest.param("a\u2029b", "controle", id="separador-de-paragrafo"),
    ],
)
def test_texto_invalido_levanta(valor: str, erro: str) -> None:
    with pytest.raises(ValorInvalidoException, match=erro):
        _validar(valor)


def test_none_e_tratado_como_vazio() -> None:
    with pytest.raises(ValorInvalidoException, match="obrigatorio"):
        # None de proposito: a guarda de runtime e o que esta sob teste.
        texto_obrigatorio(None, "texto", _LIMITE)  # type: ignore[arg-type]
