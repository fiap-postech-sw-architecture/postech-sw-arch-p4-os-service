from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.ordem_servico.dominio.resumos import (
    TAMANHO_MAXIMO_URL,
    ResumoOrcamento,
    ResumoPagamento,
    StatusPagamento,
)

_LINK = "https://billing.pytstop.local/publico/orcamentos/tok"


class TestResumoOrcamento:
    def test_valido_e_igualdade_estrutural(self) -> None:
        oid = uuid4()
        a = ResumoOrcamento(oid, Dinheiro(Decimal("10.00")), _LINK)
        b = ResumoOrcamento(oid, Dinheiro(Decimal("10.00")), _LINK)
        assert a == b
        assert hash(a) == hash(b)

    @pytest.mark.parametrize(
        "link", ["", "javascript:alert(1)", "//sem-esquema.com", "http:///sem-host"]
    )
    def test_link_invalido_levanta(self, link: str) -> None:
        with pytest.raises(ValueError, match="URL http"):
            ResumoOrcamento(uuid4(), Dinheiro(Decimal("1")), link)

    def test_link_longo_demais_levanta(self) -> None:
        link = "https://x.y/" + "a" * TAMANHO_MAXIMO_URL
        with pytest.raises(ValueError, match="excede"):
            ResumoOrcamento(uuid4(), Dinheiro(Decimal("1")), link)

    def test_imutavel(self) -> None:
        resumo = ResumoOrcamento(uuid4(), Dinheiro(Decimal("1")), _LINK)
        with pytest.raises(FrozenInstanceError):
            resumo.link_decisao = "https://outro"  # type: ignore[misc]


class TestResumoPagamento:
    def test_valido(self) -> None:
        resumo = ResumoPagamento(uuid4(), StatusPagamento.SOLICITADO, _LINK)
        assert resumo.status == "solicitado"

    def test_checkout_invalido_levanta(self) -> None:
        with pytest.raises(ValueError, match="checkout_url"):
            ResumoPagamento(uuid4(), StatusPagamento.SOLICITADO, "nao-e-url")
