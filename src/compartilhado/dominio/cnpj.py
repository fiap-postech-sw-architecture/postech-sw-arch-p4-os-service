from __future__ import annotations

from dataclasses import dataclass

from brutils.cnpj import is_valid

from src.compartilhado.dominio.documento import normalizar_cnpj, normalizar_e_validar
from src.compartilhado.dominio.value_object import ValueObject


@dataclass(frozen=True, slots=True)
class CNPJ(ValueObject):
    """Value Object de CNPJ brasileiro.

    Valida via `brutils.cnpj.is_valid`, inclusive o CNPJ alfanumerico (letras
    nas 12 primeiras posicoes), normaliza sem mascara e em maiusculas, e expoe
    formatacao `XX.XXX.XXX/XXXX-XX` e mascaramento (LGPD). Imutavel.
    """

    numero: str

    def __post_init__(self) -> None:
        numero = normalizar_e_validar(self.numero, "CNPJ", normalizar_cnpj, is_valid)
        object.__setattr__(self, "numero", numero)

    def formatado(self) -> str:
        """Retorna o CNPJ no padrao `XX.XXX.XXX/XXXX-XX`."""
        n = self.numero
        return f"{n[:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:]}"

    def mascarado(self) -> str:
        """Retorna o CNPJ com os digitos iniciais ocultos (seguro para logs)."""
        n = self.numero
        return f"**.***.***/**{n[10:12]}-{n[12:]}"

    def __repr__(self) -> str:
        return f"CNPJ(numero='{self.mascarado()}')"
