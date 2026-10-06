from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from src.compartilhado.dominio.dinheiro import Dinheiro


class TestDinheiro:
    def test_criacao_basica(self) -> None:
        d = Dinheiro(valor=Decimal("10.00"))
        assert d.valor == Decimal("10.00")
        assert d.moeda == "BRL"

    def test_criacao_com_moeda_diferente(self) -> None:
        d = Dinheiro(valor=Decimal("5.00"), moeda="USD")
        assert d.moeda == "USD"

    def test_arredondamento_para_duas_casas(self) -> None:
        d = Dinheiro(valor=Decimal("10.005"))
        assert d.valor == Decimal("10.01")

    def test_arredondamento_tres_casas(self) -> None:
        d = Dinheiro(valor=Decimal("10.004"))
        assert d.valor == Decimal("10.00")

    def test_valor_zero_valido(self) -> None:
        d = Dinheiro(valor=Decimal("0"))
        assert d.valor == Decimal("0.00")

    def test_valor_negativo_invalido(self) -> None:
        with pytest.raises(ValueError, match="negativo"):
            Dinheiro(valor=Decimal("-1"))

    def test_moeda_minuscula_invalida(self) -> None:
        with pytest.raises(ValueError, match="maiusculas"):
            Dinheiro(valor=Decimal("10"), moeda="brl")

    def test_moeda_duas_letras_invalida(self) -> None:
        with pytest.raises(ValueError, match="maiusculas"):
            Dinheiro(valor=Decimal("10"), moeda="BR")

    def test_moeda_com_numeros_invalida(self) -> None:
        with pytest.raises(ValueError, match="maiusculas"):
            Dinheiro(valor=Decimal("10"), moeda="BR1")

    def test_igualdade_estrutural(self) -> None:
        a = Dinheiro(valor=Decimal("10.00"))
        b = Dinheiro(valor=Decimal("10.00"))
        assert a == b

    def test_desigualdade_por_valor(self) -> None:
        a = Dinheiro(valor=Decimal("10.00"))
        b = Dinheiro(valor=Decimal("20.00"))
        assert a != b

    def test_desigualdade_por_moeda(self) -> None:
        a = Dinheiro(valor=Decimal("10.00"), moeda="BRL")
        b = Dinheiro(valor=Decimal("10.00"), moeda="USD")
        assert a != b

    def test_imutabilidade(self) -> None:
        d = Dinheiro(valor=Decimal("10.00"))
        with pytest.raises(FrozenInstanceError):
            d.valor = Decimal("20.00")  # type: ignore[misc]

    def test_conversao_automatica_de_int_para_decimal(self) -> None:
        d = Dinheiro(valor=Decimal("10"))
        assert d.valor == Decimal("10.00")

    def test_hash_consistente(self) -> None:
        a = Dinheiro(valor=Decimal("10.00"))
        b = Dinheiro(valor=Decimal("10.00"))
        assert hash(a) == hash(b)

    def test_usavel_em_set(self) -> None:
        a = Dinheiro(valor=Decimal("10.00"))
        b = Dinheiro(valor=Decimal("10.00"))
        assert len({a, b}) == 1

    def test_valor_infinito_invalido(self) -> None:
        with pytest.raises(ValueError, match="finito"):
            Dinheiro(valor=Decimal("Infinity"))

    def test_valor_nan_invalido(self) -> None:
        with pytest.raises(ValueError, match="finito"):
            Dinheiro(valor=Decimal("NaN"))

    def test_coercao_de_int_para_decimal(self) -> None:
        d = Dinheiro(valor=10)  # type: ignore[arg-type]
        assert isinstance(d.valor, Decimal)
        assert d.valor == Decimal("10.00")

    def test_coercao_de_float_para_decimal(self) -> None:
        d = Dinheiro(valor=10.5)  # type: ignore[arg-type]
        assert isinstance(d.valor, Decimal)
        assert d.valor == Decimal("10.50")

    def test_moeda_nao_ascii_invalida(self) -> None:
        # isalpha/isupper aceitam letras acentuadas; ISO 4217 exige A-Z.
        with pytest.raises(ValueError, match="maiusculas"):
            Dinheiro(valor=Decimal("10"), moeda="RÉA")

    def test_valor_string_invalida_vira_value_error(self) -> None:
        # Decimal("abc") levantaria decimal.InvalidOperation (nao ValueError):
        # o VO converte para o contrato de invariante do dominio.
        with pytest.raises(ValueError, match="valor monetario invalido"):
            Dinheiro(valor="abc")  # type: ignore[arg-type]

    def test_zero_negativo_normalizado(self) -> None:
        d = Dinheiro(valor=Decimal("-0.001"))
        assert d.valor == Decimal("0.00")
        assert str(d.valor) == "0.00"  # sem sinal: -0.00 e normalizado
