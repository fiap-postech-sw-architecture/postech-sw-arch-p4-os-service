"""Eventos de dominio da ``OrdemDeServico``.

Ficam dentro do servico: o OS so publica comandos no RabbitMQ (RFC-004 secao
5.3), e quem os grava na outbox e o caso de uso, por
``PublicadorDeComandos.publicar_comando``. O agregado os registra a cada
abertura e transicao; quem os vai ler e a notificacao do cliente por e-mail
(ADR-036: a linha de e-mail entra na outbox na mesma transacao da mudanca de
status), que chega com a saga. Ate la, so os testes os coletam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.compartilhado.dominio.events import DomainEvent

if TYPE_CHECKING:
    from uuid import UUID

    from src.ordem_servico.dominio.historico import OrigemMudanca
    from src.ordem_servico.dominio.status import StatusOrdem


@dataclass(frozen=True, slots=True)
class OrdemAbertaEvent(DomainEvent):
    """OS aberta em ``RECEBIDA`` para o par cliente/veiculo.

    A ``descricao_problema`` fica fora: texto livre, pode conter PII.
    """

    cliente_id: UUID = field(kw_only=True)
    veiculo_id: UUID = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class StatusDaOrdemAlteradoEvent(DomainEvent):
    """Toda transicao de status, espelhando a linha gravada no historico.

    Um tipo so para todas as transicoes. O ``motivo`` do cancelamento fica
    fora pelo mesmo motivo da descricao (texto livre); ele esta no historico
    da OS (``GET /api/v1/ordens-de-servico/{id}/historico``).
    """

    status_anterior: StatusOrdem = field(kw_only=True)
    status_novo: StatusOrdem = field(kw_only=True)
    origem: OrigemMudanca = field(kw_only=True)
