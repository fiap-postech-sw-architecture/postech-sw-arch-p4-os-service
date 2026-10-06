from __future__ import annotations

import pytest

from src.cliente_veiculo.dominio.cnpj import CNPJ

CNPJ_VALIDO = "11222333000181"


class TestCNPJ:
    def test_criacao_com_digitos(self) -> None:
        cnpj = CNPJ(numero=CNPJ_VALIDO)
        assert cnpj.numero == CNPJ_VALIDO

    def test_criacao_com_formatacao(self) -> None:
        cnpj = CNPJ(numero="11.222.333/0001-81")
        assert cnpj.numero == CNPJ_VALIDO

    def test_cnpj_invalido_digito_verificador(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="11222333000182")

    def test_cnpj_invalido_tamanho_curto(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="123")

    def test_cnpj_invalido_vazio(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="")

    def test_cnpj_invalido_todos_iguais(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="11111111111111")

    def test_formatado(self) -> None:
        cnpj = CNPJ(numero=CNPJ_VALIDO)
        assert cnpj.formatado() == "11.222.333/0001-81"

    def test_mascarado(self) -> None:
        cnpj = CNPJ(numero=CNPJ_VALIDO)
        assert cnpj.mascarado() == "**.***.***/**01-81"

    def test_igualdade_mesmo_numero(self) -> None:
        a = CNPJ(numero=CNPJ_VALIDO)
        b = CNPJ(numero=CNPJ_VALIDO)
        assert a == b

    def test_desigualdade_numero_diferente(self) -> None:
        a = CNPJ(numero=CNPJ_VALIDO)
        b = CNPJ(numero="04252011000110")
        assert a != b
        assert len({a, b}) == 2

    def test_imutabilidade(self) -> None:
        cnpj = CNPJ(numero=CNPJ_VALIDO)
        with pytest.raises(AttributeError):
            cnpj.numero = "99999999999999"  # type: ignore[misc]

    def test_hash_consistente(self) -> None:
        a = CNPJ(numero=CNPJ_VALIDO)
        b = CNPJ(numero=CNPJ_VALIDO)
        assert hash(a) == hash(b)

    def test_usavel_em_set(self) -> None:
        a = CNPJ(numero=CNPJ_VALIDO)
        b = CNPJ(numero="11.222.333/0001-81")
        assert len({a, b}) == 1

    def test_repr_mascara_cnpj(self) -> None:
        cnpj = CNPJ(numero=CNPJ_VALIDO)
        r = repr(cnpj)
        assert "**.***.***/**01-81" in r
        assert CNPJ_VALIDO not in r


def _digitos_verificadores(base: str) -> str:
    """Oraculo independente do modulo 11 do CNPJ (pesos 2..9 ciclicos)."""
    for _ in range(2):
        pesos = [2, 3, 4, 5, 6, 7, 8, 9] * 2
        soma = sum(int(d) * p for d, p in zip(reversed(base), pesos, strict=False))
        resto = soma % 11
        base += "0" if resto < 2 else str(11 - resto)
    return base[-2:]


class TestDigitoVerificadorContraOModulo11:
    @pytest.mark.parametrize(
        "base", ["112223330001", "042520110001", "123456780001", "000000000002"]
    )
    def test_aceita_dv_correto_e_rejeita_cada_dv_errado(self, base: str) -> None:
        dv = _digitos_verificadores(base)
        assert CNPJ(numero=base + dv).numero == base + dv
        for errado in (
            base + str((int(dv[0]) + 1) % 10) + dv[1],
            base + dv[0] + str((int(dv[1]) + 1) % 10),
        ):
            with pytest.raises(ValueError, match="CNPJ invalido"):
                CNPJ(numero=errado)

    def test_oraculo_confere_o_exemplo_classico(self) -> None:
        assert _digitos_verificadores(CNPJ_VALIDO[:12]) == CNPJ_VALIDO[12:]
