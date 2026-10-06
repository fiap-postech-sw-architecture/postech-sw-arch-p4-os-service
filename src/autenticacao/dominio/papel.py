from __future__ import annotations

from enum import StrEnum


class Papel(StrEnum):
    """Papeis dos usuarios internos (ADR-039)."""

    ADMIN = "admin"
    MECANICO = "mecanico"
    ATENDENTE = "atendente"
