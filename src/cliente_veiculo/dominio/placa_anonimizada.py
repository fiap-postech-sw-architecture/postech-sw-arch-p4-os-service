from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.value_object import ValueObject

if TYPE_CHECKING:
    from uuid import UUID

# O prefixo do tombstone gravado na coluna ``placa`` do veiculo anonimizado. Um
# lugar so: quem grava o tombstone (o erasure e o mapeamento do ORM) e quem
# decide o que ainda guarda a placa original (a consulta dos veiculos a avisar a
# Execucao) tem de concordar nele.
PREFIXO_DO_TOMBSTONE: Final = "ANONIMIZADO:"


def tombstone_da_placa(veiculo_id: UUID) -> str:
    """O texto da coluna ``placa`` do veiculo anonimizado: unico por veiculo."""
    return f"{PREFIXO_DO_TOMBSTONE}{veiculo_id}"


@dataclass(frozen=True, slots=True)
class PlacaAnonimizada(ValueObject):
    """Value Object que marca a placa de um Veiculo anonimizado via LGPD (p3 #72).

    A placa e PII. Quando ``ClienteRepository.anonimizar_dados`` apaga o titular,
    os veiculos do cliente sao anonimizados na mesma transacao (raw UPDATE,
    bypass dos listeners): a coluna ``placa`` recebe o tombstone unico
    ``"ANONIMIZADO:{veiculo_id}"`` (preserva a constraint UNIQUE quando varios
    veiculos sao anonimizados) e o ``Veiculo`` e reidratado com este VO em vez de
    uma ``Placa`` — assim ``isinstance(placa, Placa)`` e naturalmente False e nao
    precisamos burlar a validacao de formato da placa.

    ``valor`` devolve ``"ANONIMIZADO"`` (sem PII) para respostas/export; o
    tombstone unico vive so na coluna, reconstruido a partir do ``veiculo_id``.
    """

    veiculo_id: UUID

    @property
    def valor(self) -> str:
        """Sentinela ``"ANONIMIZADO"`` — sem PII para vazar em respostas/logs."""
        return "ANONIMIZADO"

    def mascarado(self) -> str:
        """Sentinela ``"ANONIMIZADO"`` — paridade com ``Placa.mascarado()``.

        Nao ha PII para ocultar; expor o sentinela mantem o contrato usado
        por eventos/logs que mascaram a placa incondicionalmente.
        """
        return self.valor

    def __repr__(self) -> str:
        return f"PlacaAnonimizada(veiculo_id={self.veiculo_id})"
