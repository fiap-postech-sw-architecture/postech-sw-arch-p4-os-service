from __future__ import annotations

from enum import StrEnum


class Papel(StrEnum):
    """Papeis dos usuarios internos (brief secao 7)."""

    ADMIN = "admin"
    MECANICO = "mecanico"
    ATENDENTE = "atendente"
