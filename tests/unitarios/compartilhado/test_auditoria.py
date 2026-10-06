"""``ator_de``: quem fez a operacao auditada, a partir das claims do JWT."""

from __future__ import annotations

import pytest

from src.compartilhado.interfaces.auditoria import ator_de


@pytest.mark.parametrize(
    ("claims", "ator"),
    [
        pytest.param({"sub": "u-1", "email": "a@b.c"}, "u-1", id="sub-primeiro"),
        pytest.param({"sub": "", "email": "a@b.c"}, "a@b.c", id="sub-vazio-usa-email"),
        pytest.param({"email": "a@b.c"}, "a@b.c", id="sem-sub-usa-email"),
        pytest.param({"sub": 123, "email": None}, None, id="tipos-errados"),
        pytest.param({}, None, id="sem-identificador"),
    ],
)
def test_ator_de(claims: dict[str, object], ator: str | None) -> None:
    assert ator_de(claims) == ator
