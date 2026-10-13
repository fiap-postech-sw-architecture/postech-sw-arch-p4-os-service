"""``ator_de``: quem fez a operacao auditada, a partir das claims do JWT."""

from __future__ import annotations

import pytest

from src.compartilhado.dominio.exceptions import FalhaAutenticacaoException
from src.compartilhado.interfaces.auditoria import ator_de


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"sub": "u-1"}, id="sub"),
        pytest.param({"sub": "u-1", "email": "a@b.c"}, id="sub-ignora-email"),
    ],
)
def test_ator_e_o_sub(claims: dict[str, object]) -> None:
    assert ator_de(claims) == "u-1"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"sub": "", "email": "a@b.c"}, id="sub-vazio-sem-email"),
        pytest.param({"email": "a@b.c"}, id="so-email-nao-e-ator"),
        pytest.param({"sub": 123, "email": None}, id="tipos-errados"),
        pytest.param({}, id="sem-identificador"),
    ],
)
def test_sem_sub_a_credencial_e_invalida(claims: dict[str, object]) -> None:
    # Nenhuma escrita fica sem ator: o gate ja exige o sub, e sem ele e 401.
    with pytest.raises(FalhaAutenticacaoException) as exc:
        ator_de(claims)

    assert exc.value.motivo == "missing_sub"
