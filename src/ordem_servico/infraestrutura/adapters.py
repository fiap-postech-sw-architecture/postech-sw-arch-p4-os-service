"""Adapter SQLAlchemy da ``ClientePort`` (ACL para o contexto Cliente+Veiculo).

Le as tabelas do contexto vizinho no mesmo banco do servico, sem importar o
agregado ``Cliente`` nem hidrata-lo (sem decrypt de documento so para testar
existencia).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import exists, select

from src.cliente_veiculo.infraestrutura.mapping import clientes_table, veiculos_table

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session


class ClienteSQLAlchemyAdapter:
    """Implementa ``ClientePort`` com consultas ``EXISTS``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def cliente_existe(self, cliente_id: UUID) -> bool:
        """Cliente existe E esta ativo: desativado/anonimizado nao abre OS.

        ``FOR SHARE`` ate o commit da abertura: conflita com o ``FOR UPDATE``
        de desativacao e erasure (``ClienteRepository.bloquear_cliente``), que
        entao esperam e enxergam a OS nova; se eles chegarem antes, esta
        leitura espera e reavalia ``ativo`` na linha ja atualizada.
        """
        stmt = (
            select(clientes_table.c.id)
            .where(
                clientes_table.c.id == cliente_id,
                clientes_table.c.ativo.is_(True),
            )
            .with_for_update(read=True)
        )
        return self._session.execute(stmt).first() is not None

    def veiculo_pertence_ao_cliente(self, cliente_id: UUID, veiculo_id: UUID) -> bool:
        stmt = select(
            exists().where(
                veiculos_table.c.id == veiculo_id,
                veiculos_table.c.cliente_id == cliente_id,
            )
        )
        return bool(self._session.scalar(stmt))
