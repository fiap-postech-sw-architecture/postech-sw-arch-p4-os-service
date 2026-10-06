from __future__ import annotations

from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS, StatusOrdem


class TestStatusOrdem:
    def test_estados_da_fase_4_na_ordem_do_fluxo(self) -> None:
        assert [s.value for s in StatusOrdem] == [
            "recebida",
            "em_diagnostico",
            "aguardando_aprovacao",
            "aguardando_pagamento",
            "aguardando_execucao",
            "em_execucao",
            "finalizada",
            "entregue",
            "cancelada",
        ]

    def test_complementar_do_p3_saiu(self) -> None:
        assert "aguardando_aprovacao_complementar" not in {s.value for s in StatusOrdem}

    def test_comparavel_com_string(self) -> None:
        # StrEnum: o membro e igual ao valor (colunas e payloads guardam a str).
        assert StatusOrdem.RECEBIDA == "recebida"
        assert StatusOrdem("recebida") is StatusOrdem.RECEBIDA

    def test_terminais(self) -> None:
        assert frozenset({StatusOrdem.ENTREGUE, StatusOrdem.CANCELADA}) == (
            ESTADOS_TERMINAIS
        )
