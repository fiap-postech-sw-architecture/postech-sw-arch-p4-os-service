from __future__ import annotations

import pytest

from tests.chaves_jwt import congelar_o_relogio_do_pyjwt


@pytest.fixture
def relogio_congelado(monkeypatch: pytest.MonkeyPatch) -> None:
    """O relogio com que o PyJWT confere exp e iat fica parado (AGORA_CONGELADA)."""
    congelar_o_relogio_do_pyjwt(monkeypatch)
