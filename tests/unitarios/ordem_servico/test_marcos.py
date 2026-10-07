"""Marcos da OS que a saga le para classificar um evento."""

from __future__ import annotations

import pytest

from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.status import StatusOrdem
from tests.unitarios.ordem_servico.cenario_da_saga import SEM_CHECKOUT, ordem_no_perfil

S = StatusOrdem


@pytest.mark.parametrize(
    ("perfil", "marcos"),
    [
        pytest.param("recebida", (False, False, False), id="recebida"),
        pytest.param("em_diagnostico", (True, False, False), id="em_diagnostico"),
        pytest.param("aguardando_aprovacao", (True, False, False), id="aprovacao"),
        pytest.param(SEM_CHECKOUT, (True, False, False), id="sem-checkout"),
        pytest.param("aguardando_pagamento", (True, True, False), id="checkout"),
        pytest.param("aguardando_execucao", (True, True, False), id="execucao"),
        pytest.param("finalizada", (True, True, False), id="finalizada"),
        pytest.param("entregue", (True, True, True), id="entregue"),
        # Cancelada logo depois da abertura: nunca iniciou o diagnostico.
        pytest.param("cancelada", (False, False, True), id="cancelada"),
    ],
)
def test_marcos_pelo_historico_pelo_resumo_e_pelo_status(
    perfil: str, marcos: tuple[bool, bool, bool]
) -> None:
    lidos = MarcosDaOrdem.da_ordem(ordem_no_perfil(perfil))

    assert (lidos.diagnostico_iniciado, lidos.checkout_aberto, lidos.encerrada) == (
        marcos
    )


def test_marcos_sao_um_retrato_que_nao_acompanha_a_os() -> None:
    ordem = ordem_no_perfil("recebida")
    marcos = MarcosDaOrdem.da_ordem(ordem)

    ordem.registrar_diagnostico_iniciado(ator="consumidor")

    assert not marcos.diagnostico_iniciado
    assert MarcosDaOrdem.da_ordem(ordem).diagnostico_iniciado
