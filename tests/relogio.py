"""Relogio de teste: nao anda sozinho, so quando o teste manda."""

from __future__ import annotations


class RelogioFixo:
    """Relogio monotonico (segundos) que o teste controla.

    Quem recebe um relogio injetado (a validade do cache do coletor) nunca
    depende do tempo real do teste: ``avancar`` e o unico jeito de ele andar.
    """

    def __init__(self, agora: float = 1000.0) -> None:
        self.agora = agora

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos
