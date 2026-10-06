from __future__ import annotations

import pytest

from src.compartilhado.dominio.cnpj import CNPJ

CNPJ_VALIDO = "11222333000181"


class TestCNPJ:
    def test_criacao_com_digitos(self) -> None:
        cnpj = CNPJ(numero=CNPJ_VALIDO)
        assert cnpj.numero == CNPJ_VALIDO

    def test_criacao_com_formatacao(self) -> None:
        cnpj = CNPJ(numero="11.222.333/0001-81")
        assert cnpj.numero == CNPJ_VALIDO

    @pytest.mark.parametrize(
        "numero",
        [
            pytest.param(" 11.222.333/0001-81 ", id="espacos-nas-pontas"),
            pytest.param("11.222.333/0001-81\n", id="quebra-de-linha-no-fim"),
            pytest.param("\t11222333000181\r\n", id="tab-e-crlf"),
            pytest.param("11 222 333 0001 81", id="espacos-no-meio"),
            pytest.param("\u00a011222333000181\u00a0", id="espaco-inquebravel"),
        ],
    )
    def test_espaco_e_quebra_de_linha_sao_ignorados(self, numero: str) -> None:
        assert CNPJ(numero=numero).numero == CNPJ_VALIDO

    def test_cnpj_invalido_digito_verificador(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="11222333000182")

    def test_cnpj_invalido_tamanho_curto(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="123")

    def test_cnpj_invalido_vazio(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="")

    def test_cnpj_com_digitos_de_outro_alfabeto_e_rejeitado(self) -> None:
        arabe_indico = "".join(chr(0x0660 + int(d)) for d in CNPJ_VALIDO)
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero=arabe_indico)

    def test_dv_em_outro_alfabeto_e_rejeitado_mesmo_com_o_brutils_aceitando(
        self,
    ) -> None:
        """O brutils le o DV com ``int()``, que aceita digito arabe-indico."""
        dv_arabe_indico = "".join(chr(0x0660 + int(d)) for d in CNPJ_VALIDO[12:])
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero=CNPJ_VALIDO[:12] + dv_arabe_indico)

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


CNPJ_ALFANUMERICO = "12ABC34501DE35"


class TestCNPJAlfanumerico:
    """CNPJ com letras (IN RFB 2.229/2024), emitido desde julho de 2026."""

    def test_aceita_com_mascara_e_normaliza(self) -> None:
        assert CNPJ(numero="12.ABC.345/01DE-35").numero == CNPJ_ALFANUMERICO

    def test_minusculas_viram_maiusculas(self) -> None:
        assert CNPJ(numero="12.abc.345/01de-35").numero == CNPJ_ALFANUMERICO

    def test_espaco_e_quebra_de_linha_sao_ignorados(self) -> None:
        assert CNPJ(numero=" 12.ABC.345/01DE-35\n").numero == CNPJ_ALFANUMERICO

    def test_dv_errado_e_rejeitado(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="12ABC34501DE36")

    def test_letra_no_dv_e_rejeitada(self) -> None:
        with pytest.raises(ValueError, match="CNPJ invalido"):
            CNPJ(numero="12ABC34501DE3A")

    def test_formatado_e_mascarado(self) -> None:
        cnpj = CNPJ(numero=CNPJ_ALFANUMERICO)
        assert cnpj.formatado() == "12.ABC.345/01DE-35"
        assert cnpj.mascarado() == "**.***.***/**DE-35"
        assert CNPJ_ALFANUMERICO not in repr(cnpj)


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
