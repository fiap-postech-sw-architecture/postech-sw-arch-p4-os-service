from __future__ import annotations

import pytest

from src.cliente_veiculo.dominio.cpf import CPF

CPF_VALIDO = "21249722519"


class TestCPF:
    def test_criacao_com_digitos(self) -> None:
        cpf = CPF(numero=CPF_VALIDO)
        assert cpf.numero == CPF_VALIDO

    def test_criacao_com_formatacao(self) -> None:
        cpf = CPF(numero="212.497.225-19")
        assert cpf.numero == CPF_VALIDO

    def test_cpf_invalido_digito_verificador(self) -> None:
        with pytest.raises(ValueError, match="CPF invalido"):
            CPF(numero="21249722510")

    def test_cpf_invalido_todos_iguais(self) -> None:
        with pytest.raises(ValueError, match="CPF invalido"):
            CPF(numero="11111111111")

    def test_cpf_invalido_tamanho_curto(self) -> None:
        with pytest.raises(ValueError, match="CPF invalido"):
            CPF(numero="123")

    def test_cpf_invalido_vazio(self) -> None:
        with pytest.raises(ValueError, match="CPF invalido"):
            CPF(numero="")

    def test_formatado(self) -> None:
        cpf = CPF(numero=CPF_VALIDO)
        assert cpf.formatado() == "212.497.225-19"

    def test_mascarado(self) -> None:
        cpf = CPF(numero=CPF_VALIDO)
        assert cpf.mascarado() == "***.***.***-19"

    def test_igualdade_mesmo_numero(self) -> None:
        a = CPF(numero=CPF_VALIDO)
        b = CPF(numero=CPF_VALIDO)
        assert a == b

    def test_desigualdade_numero_diferente(self) -> None:
        a = CPF(numero=CPF_VALIDO)
        b = CPF(numero="16755769983")
        assert a != b

    def test_imutabilidade(self) -> None:
        cpf = CPF(numero=CPF_VALIDO)
        with pytest.raises(AttributeError):
            cpf.numero = "99999999999"  # type: ignore[misc]

    def test_hash_consistente(self) -> None:
        a = CPF(numero=CPF_VALIDO)
        b = CPF(numero=CPF_VALIDO)
        assert hash(a) == hash(b)

    def test_usavel_em_set(self) -> None:
        a = CPF(numero=CPF_VALIDO)
        b = CPF(numero="212.497.225-19")
        assert len({a, b}) == 1

    def test_repr_mascara_cpf(self) -> None:
        cpf = CPF(numero=CPF_VALIDO)
        r = repr(cpf)
        assert "***.***.***-19" in r
        assert CPF_VALIDO not in r


class TestMensagemSemPii:
    """Guard TD-033/p3 #126: mensagem de CPF invalido nao ecoa o numero."""

    def test_mensagem_de_cpf_invalido_nao_contem_o_numero(self) -> None:
        with pytest.raises(ValueError, match="CPF invalido") as exc_info:
            CPF(numero="111.444.777-04")
        assert "111" not in str(exc_info.value)
        assert "777" not in str(exc_info.value)
        assert str(exc_info.value) == "CPF invalido"


def _digitos_verificadores(base: str, pesos_iniciais: tuple[int, ...]) -> str:
    """Oraculo independente do modulo 11 da Receita (CPF e CNPJ)."""
    for pesos in pesos_iniciais:
        soma = sum(int(d) * p for d, p in zip(base, range(pesos, 1, -1), strict=False))
        resto = soma % 11
        base += "0" if resto < 2 else str(11 - resto)
    return base[-2:]


class TestDigitoVerificadorContraOModulo11:
    """A banca da fase 3 descontou a falta disso na Lambda: o VO tem de bater
    com o algoritmo da Receita, nao so aceitar 11 digitos."""

    @pytest.mark.parametrize(
        "base",
        ["123456789", "987654321", "529982247", "212497225", "000000019", "390533447"],
    )
    def test_aceita_dv_correto_e_rejeita_cada_dv_errado(self, base: str) -> None:
        dv = _digitos_verificadores(base, (10, 11))
        assert CPF(numero=base + dv).numero == base + dv
        for errado in (
            base + str((int(dv[0]) + 1) % 10) + dv[1],
            base + dv[0] + str((int(dv[1]) + 1) % 10),
        ):
            with pytest.raises(ValueError, match="CPF invalido"):
                CPF(numero=errado)

    @pytest.mark.parametrize("posicao", range(9))
    def test_qualquer_digito_da_base_alterado_invalida(self, posicao: int) -> None:
        digitos = list(CPF_VALIDO)
        digitos[posicao] = str((int(digitos[posicao]) + 1) % 10)
        with pytest.raises(ValueError, match="CPF invalido"):
            CPF(numero="".join(digitos))
