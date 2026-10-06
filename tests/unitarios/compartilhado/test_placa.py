from __future__ import annotations

import pytest

from src.compartilhado.dominio.placa import Placa


class TestPlaca:
    def test_formato_antigo_valido(self) -> None:
        placa = Placa(valor="ABC1234")
        assert placa.valor == "ABC1234"

    def test_formato_mercosul_valido(self) -> None:
        placa = Placa(valor="ABC1D23")
        assert placa.valor == "ABC1D23"

    def test_hifen_removido(self) -> None:
        placa = Placa(valor="ABC-1234")
        assert placa.valor == "ABC1234"

    def test_minuscula_normalizada(self) -> None:
        placa = Placa(valor="abc1234")
        assert placa.valor == "ABC1234"

    def test_hifen_e_minuscula(self) -> None:
        placa = Placa(valor="abc-1d23")
        assert placa.valor == "ABC1D23"

    def test_invalida_muito_curta(self) -> None:
        with pytest.raises(ValueError, match="Placa invalida"):
            Placa(valor="ABC")

    def test_invalida_muito_longa(self) -> None:
        with pytest.raises(ValueError, match="Placa invalida"):
            Placa(valor="ABC12345")

    def test_invalida_padrao_errado(self) -> None:
        with pytest.raises(ValueError, match="Placa invalida"):
            Placa(valor="1234ABC")

    def test_invalida_vazia(self) -> None:
        with pytest.raises(ValueError, match="Placa invalida"):
            Placa(valor="")

    def test_digitos_de_outro_alfabeto_sao_rejeitados(self) -> None:
        arabe_indico = "".join(chr(0x0660 + int(d)) for d in "1234")
        with pytest.raises(ValueError, match="Placa invalida"):
            Placa(valor="ABC" + arabe_indico)

    def test_quebra_de_linha_final_e_rejeitada(self) -> None:
        """``$`` casa antes do ``\\n`` final; a validacao usa ``fullmatch``."""
        with pytest.raises(ValueError, match="Placa invalida"):
            Placa(valor="ABC1234\n")

    def test_igualdade_mesmo_valor(self) -> None:
        a = Placa(valor="ABC1234")
        b = Placa(valor="ABC1234")
        assert a == b

    def test_igualdade_formatos_diferentes(self) -> None:
        a = Placa(valor="ABC-1234")
        b = Placa(valor="abc1234")
        assert a == b

    def test_imutabilidade(self) -> None:
        placa = Placa(valor="ABC1234")
        with pytest.raises(AttributeError):
            placa.valor = "DEF5678"  # type: ignore[misc]

    def test_hash_consistente(self) -> None:
        a = Placa(valor="ABC1234")
        b = Placa(valor="ABC-1234")
        assert hash(a) == hash(b)


class TestMensagemSemPii:
    """Guard TD-033/p3 #126: o handler global ecoa str(exc) no corpo do 422,
    entao mensagens de invariante de VO com PII NAO podem interpolar o valor."""

    def test_mensagem_de_placa_invalida_nao_contem_o_valor(self) -> None:
        with pytest.raises(ValueError, match="Placa invalida") as exc_info:
            Placa(valor="XYZ-99999")
        assert "XYZ" not in str(exc_info.value)
        assert "99999" not in str(exc_info.value)
        assert str(exc_info.value) == "Placa invalida"


class TestReprSemPii:
    """Finding da revisao de entrega: repr default expunha a placa crua."""

    def test_repr_mascara_a_placa(self) -> None:
        placa = Placa(valor="ABC1D23")
        assert repr(placa) == "Placa(valor='AB*****')"
        assert "ABC1D23" not in repr(placa)

    def test_mascarado_preserva_tamanho(self) -> None:
        assert Placa(valor="ABC1234").mascarado() == "AB*****"
