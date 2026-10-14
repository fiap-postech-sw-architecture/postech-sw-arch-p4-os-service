"""Adapter SQLAlchemy da ``ClientePort`` (ACL para o contexto Cliente+Veiculo).

Le as tabelas do contexto vizinho no mesmo banco do servico, sem importar o
agregado ``Cliente`` nem hidrata-lo (sem decrypt de documento so para testar
existencia).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from src.cliente_veiculo.dominio.contato import CONTATO_ANONIMIZADO
from src.cliente_veiculo.infraestrutura.mapping import clientes_table, veiculos_table
from src.ordem_servico.aplicacao.dtos import ContatoDoCliente, RetratoDoVeiculo

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

    def retrato_do_veiculo(
        self, cliente_id: UUID, veiculo_id: UUID
    ) -> RetratoDoVeiculo | None:
        """Retrato do veiculo para o ``SolicitarDiagnostico``, se for do cliente."""
        v = veiculos_table
        linha = self._session.execute(
            select(v.c.placa, v.c.marca, v.c.modelo, v.c.ano).where(
                v.c.id == veiculo_id, v.c.cliente_id == cliente_id
            )
        ).first()
        if linha is None:
            return None
        return RetratoDoVeiculo(
            placa=linha.placa, marca=linha.marca, modelo=linha.modelo, ano=linha.ano
        )

    def obter_contato(self, cliente_id: UUID) -> ContatoDoCliente | None:
        """Nome e contato em claro (``nome`` e ``contato``), sem o agregado.

        So duas colunas: o ``documento`` (o unico cifrado) nao e lido, e quem
        chama nao precisa da ``ENCRYPTION_KEY``. Cliente anonimizado (contato
        no marcador do apagamento LGPD) e como cliente sem contato.
        """
        c = clientes_table
        linha = self._session.execute(
            select(c.c.nome, c.c.contato).where(c.c.id == cliente_id)
        ).first()
        if linha is None or linha.contato == CONTATO_ANONIMIZADO:
            return None
        return ContatoDoCliente(nome=linha.nome, contato=linha.contato)
