from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from src.compartilhado.dominio.value_object import ValueObject

_DUAS_CASAS = Decimal("0.01")
_TAMANHO_CODIGO_MOEDA = 3
# Teto das colunas Numeric(12, 2): acima disso o valor so estouraria no flush.
VALOR_MAXIMO = Decimal("9999999999.99")


@dataclass(frozen=True, slots=True)
class Dinheiro(ValueObject):
    """Value Object monetario com moeda e precisao de 2 casas decimais.

    Usa Decimal (nunca float). Impoe valores finitos entre 0 e ``VALOR_MAXIMO``
    (o teto das colunas ``Numeric(12, 2)``) e codigo de moeda ISO 4217 com 3
    letras maiusculas. Qualquer violacao levanta ``ValueError``.

    Sem aritmetica: a OS so guarda o total que o Billing calcula. As operacoes
    do p3 (soma, subtracao e multiplicacao) voltam se a OS precisar delas.
    """

    valor: Decimal
    moeda: str = "BRL"

    def __post_init__(self) -> None:
        if not isinstance(self.valor, Decimal):
            try:
                object.__setattr__(self, "valor", Decimal(str(self.valor)))
            except InvalidOperation as exc:
                msg = "valor monetario invalido"
                raise ValueError(msg) from exc

        if not self.valor.is_finite():
            msg = "Valor monetario deve ser finito"
            raise ValueError(msg)

        try:
            quantizado = self.valor.quantize(_DUAS_CASAS, rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            # Acima da precisao do contexto (28 digitos) o quantize nao cabe.
            msg = "valor monetario invalido"
            raise ValueError(msg) from exc
        # Normaliza zero negativo (-0.00 -> 0.00) antes das validacoes.
        quantizado += Decimal(0)
        object.__setattr__(self, "valor", quantizado)

        if self.valor < 0:
            msg = f"Valor nao pode ser negativo: {self.valor}"
            raise ValueError(msg)
        if self.valor > VALOR_MAXIMO:
            msg = f"Valor excede o maximo de {VALOR_MAXIMO}"
            raise ValueError(msg)

        moeda_valida = (
            len(self.moeda) == _TAMANHO_CODIGO_MOEDA
            # isascii: isalpha/isupper aceitam letras acentuadas; ISO 4217 e A-Z.
            and self.moeda.isascii()
            and self.moeda.isalpha()
            and self.moeda.isupper()
        )
        if not moeda_valida:
            msg = f"Moeda deve ter 3 letras maiusculas: {self.moeda}"
            raise ValueError(msg)
