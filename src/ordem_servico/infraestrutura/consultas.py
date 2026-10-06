"""Query services de leitura do contexto OS (projecoes, sem hidratar o agregado)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from src.cliente_veiculo.infraestrutura.mapping import clientes_table, veiculos_table
from src.compartilhado.infraestrutura.encryption import EncryptionService
from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO
from src.ordem_servico.infraestrutura.mapping import ordens_de_servico_table

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.compartilhado.dominio.documento import Documento
    from src.compartilhado.dominio.placa import Placa

_t = ordens_de_servico_table


class ConsultaAcompanhamentoSQLAlchemy:
    """``ConsultaAcompanhamento`` sobre a ``Session`` da requisicao."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def mais_recente(
        self, placa: Placa, documento: Documento
    ) -> AcompanhamentoDTO | None:
        """Projeta so ``(status, criado_em, atualizado_em)`` da OS mais recente.

        O documento e comparado pelo hash deterministico (nunca em claro), o
        mesmo do cadastro; a escolha da mais recente e do banco
        (``ORDER BY ... LIMIT 1``).
        """
        doc_hash = EncryptionService.instance().hash_deterministic(documento.numero)
        stmt = (
            select(_t.c.status, _t.c.criado_em, _t.c.atualizado_em)
            .join(clientes_table, _t.c.cliente_id == clientes_table.c.id)
            .join(veiculos_table, _t.c.veiculo_id == veiculos_table.c.id)
            .where(veiculos_table.c.placa == placa.valor)
            .where(clientes_table.c.documento_hash == doc_hash)
            .order_by(_t.c.criado_em.desc(), _t.c.id.desc())
            .limit(1)
        )
        linha = self._session.execute(stmt).first()
        if linha is None:
            return None
        return AcompanhamentoDTO(
            status=linha.status.value,
            criado_em=linha.criado_em,
            atualizado_em=linha.atualizado_em,
        )
