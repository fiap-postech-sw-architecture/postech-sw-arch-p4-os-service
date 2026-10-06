"""``ator_de``: quem fez a operacao auditada, a partir das claims do JWT."""

from __future__ import annotations

import pytest

from src.compartilhado.interfaces.auditoria import ator_de


@pytest.mark.parametrize(
    ("claims", "ator"),
    [
        pytest.param({"sub": "u-1"}, "u-1", id="sub"),
        pytest.param({"sub": "u-1", "email": "a@b.c"}, "u-1", id="sub-ignora-email"),
        pytest.param({"sub": "", "email": "a@b.c"}, None, id="sub-vazio-sem-email"),
        pytest.param({"email": "a@b.c"}, None, id="so-email-nao-e-ator"),
        pytest.param({"sub": 123, "email": None}, None, id="tipos-errados"),
        pytest.param({}, None, id="sem-identificador"),
    ],
)
def test_ator_de(claims: dict[str, object], ator: str | None) -> None:
    assert ator_de(claims) == ator
