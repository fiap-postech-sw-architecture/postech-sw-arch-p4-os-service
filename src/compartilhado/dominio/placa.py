from __future__ import annotations

import re
from dataclasses import dataclass

from src.compartilhado.dominio.documento import normalizar_placa
from src.compartilhado.dominio.value_object import ValueObject

# re.ASCII: sem a flag, \d aceitaria digitos de outros alfabetos e a mesma
# placa teria duas grafias distintas sob a UNIQUE de veiculos.placa.
_PADRAO_ANTIGA = re.compile(r"[A-Z]{3}\d{4}", re.ASCII)
_PADRAO_MERCOSUL = re.compile(r"[A-Z]{3}\d[A-Z]\d{2}", re.ASCII)


@dataclass(frozen=True, slots=True)
class Placa(ValueObject):
    """Value Object de placa veicular brasileira.

    Aceita o padrao antigo `ABC1234` e o padrao Mercosul `ABC1D23`. Normaliza
    o valor para uppercase e remove hifens. Imutavel.
    """

    valor: str

    def __post_init__(self) -> None:
        valor = normalizar_placa(self.valor)
        if not (_PADRAO_ANTIGA.fullmatch(valor) or _PADRAO_MERCOSUL.fullmatch(valor)):
            # Sem ecoar o valor: placa e PII e o handler global de ValueError
            # devolve str(exc) no corpo do 422 (TD-033/p3 #126).
            msg = "Placa invalida"
            raise ValueError(msg)
        object.__setattr__(self, "valor", valor)

    def mascarado(self) -> str:
        """Placa com o meio oculto (``ABC1D23`` -> ``AB*****``)."""
        return self.valor[:2] + "*" * (len(self.valor) - 2)

    def __repr__(self) -> str:
        # Placa e PII veicular: o __repr__ default vazaria o valor cru em
        # tracebacks/logs (padrao dos VOs CPF/CNPJ/Contato).
        return f"Placa(valor='{self.mascarado()}')"
