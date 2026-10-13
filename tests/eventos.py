"""Eventos do contrato para os testes: o exemplo do platform com ids e OS novos."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.compartilhado.aplicacao.mensageria import MensagemRecebida
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo


def envelope_de_evento(
    tipo: str,
    *,
    mensagem_id: Any = None,
    correlation_id: Any = None,
    causation_id: Any = None,
    **dados: Any,
) -> dict[str, Any]:
    """Evento valido do catalogo: o exemplo do platform com id e OS novos.

    ``causation_id`` (o id do comando respondido, RFC-004 secao 5.2) troca o
    do exemplo; ``dados`` sobrescreve campos do exemplo.
    """
    exemplo = json.loads((CONTRATOS / "exemplos" / f"{tipo}.json").read_text())
    ordem_id = str(correlation_id or uuid4())
    exemplo["id"] = str(mensagem_id or uuid4())
    exemplo["correlation_id"] = ordem_id
    if causation_id is not None:
        exemplo["causation_id"] = str(causation_id)
    exemplo["ocorrido_em"] = datetime.now(UTC).isoformat()
    exemplo["dados"] = {**exemplo["dados"], "ordem_id": ordem_id, **dados}
    return exemplo


def evento(
    tipo: str, correlation_id: Any, *, causation_id: Any = None, **dados: Any
) -> MensagemRecebida:
    """``MensagemRecebida`` como o consumidor a entrega: validada pelo contrato.

    ``correlation_id`` e o id da OS, que tambem vai em ``dados.ordem_id``.
    """
    envelope = envelope_de_evento(
        tipo, correlation_id=correlation_id, causation_id=causation_id, **dados
    )
    catalogo().validar(envelope)
    return MensagemRecebida.do_envelope(envelope)
