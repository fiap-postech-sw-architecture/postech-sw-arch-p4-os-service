"""Eventos de integracao da ``OrdemDeServico`` (vao para a outbox no commit).

A ``UnitOfWork`` grava todo ``IntegrationEvent`` na tabela ``outbox`` na
mesma transacao do estado; o relay le a tabela e publica no RabbitMQ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.compartilhado.dominio.integration_event import IntegrationEvent

if TYPE_CHECKING:
    from uuid import UUID

    from src.ordem_servico.dominio.historico import OrigemMudanca
    from src.ordem_servico.dominio.status import StatusOrdem


@dataclass(frozen=True, slots=True)
class OrdemAbertaEvent(IntegrationEvent):
    """OS aberta em ``RECEBIDA`` para o par cliente/veiculo.

    A ``descricao_problema`` fica fora: texto livre, pode conter PII.
    """

    cliente_id: UUID = field(kw_only=True)
    veiculo_id: UUID = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class StatusDaOrdemAlteradoEvent(IntegrationEvent):
    """Toda transicao de status, espelhando a linha gravada no historico.

    Um tipo so para todas as transicoes: o consumidor filtra por
    ``status_novo``. O ``motivo`` do cancelamento fica fora do payload pelo
    mesmo motivo da descricao (texto livre); ele esta no historico da OS
    (``GET /api/v1/ordens-de-servico/{id}/historico``).
    """

    status_anterior: StatusOrdem = field(kw_only=True)
    status_novo: StatusOrdem = field(kw_only=True)
    origem: OrigemMudanca = field(kw_only=True)
