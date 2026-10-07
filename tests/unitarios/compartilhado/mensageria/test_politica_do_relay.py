"""Tabela de atrasos do relay: um atraso por falha, e a quinta falha mata a linha."""

from __future__ import annotations

import pytest

from src.compartilhado.infraestrutura.mensageria.outbox import (
    ATRASOS_S,
    LinhaDaOutbox,
    atraso_depois_da_falha,
)


def test_atrasos_do_relay_sao_os_do_p3_e_a_quinta_falha_e_dead() -> None:
    assert [atraso_depois_da_falha(t) for t in range(1, 7)] == [
        1,
        4,
        16,
        64,
        None,
        None,
    ]
    assert ATRASOS_S == (1, 4, 16, 64)


@pytest.mark.parametrize(
    ("atrasos", "esperado"),
    [
        pytest.param((0.5,), [0.5, None], id="um-atraso"),
        pytest.param((2, 3, 5), [2, 3, 5, None], id="tres-atrasos"),
    ],
)
def test_limite_de_tentativas_sai_do_tamanho_da_tabela(
    atrasos: tuple[float, ...], esperado: list[float | None]
) -> None:
    assert [atraso_depois_da_falha(t, atrasos) for t in range(1, len(atrasos) + 2)] == (
        esperado
    )


def test_envelope_fica_fora_do_repr_da_linha() -> None:
    from uuid import uuid4

    linha = LinhaDaOutbox(
        id=1,
        mensagem_id=uuid4(),
        correlation_id=uuid4(),
        exchange="pytstop.comandos",
        routing_key="comando.execucao.solicitar_diagnostico",
        envelope={"dados": {"veiculo": {"placa": "BRA2E19"}}},
        traceparent=None,
        tracestate=None,
        tentativas=0,
    )

    assert "BRA2E19" not in repr(linha)
