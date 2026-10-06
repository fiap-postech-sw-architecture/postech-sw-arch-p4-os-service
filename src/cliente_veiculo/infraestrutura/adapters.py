from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, exists, select, update

from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS
from src.ordem_servico.infraestrutura.mapping import (
    historico_status_ordem_table,
    ordens_de_servico_table,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

# Mesmo sentinela do erasure de clientes e veiculos (repository.py).
_ANONIMIZADO = "ANONIMIZADO"


class OrdemDeServicoSQLAlchemyAdapter:
    """Adapta consultas SQLAlchemy para o contrato `OrdemDeServicoPort`.

    Verifica OS ativas para clientes e qualquer OS vinculada a veiculos,
    evitando desativacoes/remocoes que quebrariam regras de negocio ou FKs.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def existe_os_ativa_para_cliente(self, cliente_id: UUID) -> bool:
        stmt = select(
            exists().where(
                ordens_de_servico_table.c.cliente_id == cliente_id,
                ordens_de_servico_table.c.status.notin_(ESTADOS_TERMINAIS),
            )
        )
        return bool(self._session.scalar(stmt))

    def existe_os_para_veiculo(self, veiculo_id: UUID) -> bool:
        """Verifica se existe QUALQUER OS para o veiculo (ativa ou encerrada).

        Usado antes de remover o veiculo do banco para evitar IntegrityError:
        a tabela ordens_de_servico tem FK para veiculos.id sem ON DELETE CASCADE.
        """
        stmt = select(
            exists().where(ordens_de_servico_table.c.veiculo_id == veiculo_id)
        )
        return bool(self._session.scalar(stmt))

    def anonimizar_textos_livres_do_cliente(self, cliente_id: UUID) -> None:
        """UPDATE direto da descricao e dos motivos das OS do cliente (LGPD).

        So roda sem OS ativa (guard do erasure), entao nenhuma transicao
        concorre; a ``versao`` sobe mesmo assim para o lock otimista enxergar
        a escrita. Motivo nulo continua nulo (so o texto existente sai).
        """
        ordens = ordens_de_servico_table
        self._session.execute(
            update(ordens)
            .where(ordens.c.cliente_id == cliente_id)
            .values(
                descricao_problema=_ANONIMIZADO,
                motivo_cancelamento=case(
                    (ordens.c.motivo_cancelamento.is_(None), None),
                    else_=_ANONIMIZADO,
                ),
                versao=ordens.c.versao + 1,
            )
        )
        historico = historico_status_ordem_table
        self._session.execute(
            update(historico)
            .where(
                historico.c.motivo.is_not(None),
                historico.c.ordem_id.in_(
                    select(ordens.c.id).where(ordens.c.cliente_id == cliente_id)
                ),
            )
            .values(motivo=_ANONIMIZADO)
        )
