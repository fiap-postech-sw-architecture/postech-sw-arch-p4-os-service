"""Implementacao SQLAlchemy do repositorio de OrdemDeServico."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from sqlalchemy import case, func, select
from sqlalchemy.orm.exc import StaleDataError

from src.cliente_veiculo.infraestrutura.mapping import clientes_table, veiculos_table
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.encryption import EncryptionService
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.mapping import ordens_de_servico_table

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

_t = ordens_de_servico_table

# Encerradas ficam fora da listagem padrao: o proprio status e o marcador
# (nenhum delete fisico, nenhuma coluna de soft-delete).
_ESTADOS_ENCERRADOS: Final = frozenset(
    {StatusOrdem.FINALIZADA, StatusOrdem.ENTREGUE, StatusOrdem.CANCELADA}
)
# Prioridade da fila: quanto mais perto da conclusao, antes aparece (mesma
# regra do p3, estendida aos estados novos da fase 4). Encerradas caem no
# else_, ao final, quando a visao completa e pedida.
_PRIORIDADE_STATUS: Final = {
    StatusOrdem.EM_EXECUCAO: 0,
    StatusOrdem.AGUARDANDO_EXECUCAO: 1,
    StatusOrdem.AGUARDANDO_PAGAMENTO: 2,
    StatusOrdem.AGUARDANDO_APROVACAO: 3,
    StatusOrdem.EM_DIAGNOSTICO: 4,
    StatusOrdem.RECEBIDA: 5,
}
_PRIORIDADE_ENCERRADAS: Final = 9
# Mesma normalizacao do contexto Cliente+Veiculo: documento so com digitos e
# placa em maiusculas sem hifen, senao a entrada mascarada nao casa.
_NAO_DIGITO: Final = re.compile(r"\D", re.ASCII)


class OrdemDeServicoSQLAlchemyRepository:
    """``OrdemDeServicoRepository`` sobre a ``Session`` da requisicao."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def obter_por_id(self, ordem_id: UUID) -> OrdemDeServico | None:
        return self._session.get(OrdemDeServico, ordem_id)

    def salvar(self, ordem: OrdemDeServico) -> None:
        """Adiciona e faz flush; versao divergente vira ``ConflitoDeConcorrencia``.

        O flush aqui (e nao so no commit) faz o UPDATE condicional na versao
        acontecer dentro do caso de uso, que entao aborta antes do commit.
        """
        # Lido antes do flush: a falha expira a instancia e qualquer leitura
        # de atributo depois dela exigiria um rollback da session.
        ordem_id = ordem.id
        self._session.add(ordem)
        try:
            self._session.flush()
        except StaleDataError:
            raise ConflitoDeConcorrenciaException(
                mensagem=f"Ordem {ordem_id} alterada por outra operacao; releia"
            ) from None

    def listar(
        self, offset: int = 0, limit: int = 20, *, incluir_encerradas: bool = False
    ) -> list[OrdemDeServico]:
        """Prioridade de status, depois ``criado_em`` e ``id`` (paginacao estavel)."""
        prioridade = case(
            _PRIORIDADE_STATUS, value=_t.c.status, else_=_PRIORIDADE_ENCERRADAS
        )
        stmt = select(OrdemDeServico)
        if not incluir_encerradas:
            stmt = stmt.where(_t.c.status.notin_(_ESTADOS_ENCERRADOS))
        stmt = (
            stmt.order_by(prioridade, _t.c.criado_em.asc(), _t.c.id)
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def contar(self, *, incluir_encerradas: bool = False) -> int:
        stmt = select(func.count()).select_from(_t)
        if not incluir_encerradas:
            stmt = stmt.where(_t.c.status.notin_(_ESTADOS_ENCERRADOS))
        return self._session.scalar(stmt) or 0

    def obter_mais_recente_por_placa_e_documento(
        self, placa: str, documento: str
    ) -> OrdemDeServico | None:
        """Ordem mais recente do par placa + documento (CPF/CNPJ), ou ``None``.

        O documento e comparado pelo hash deterministico (nunca em claro); a
        escolha da mais recente e do banco (``ORDER BY ... LIMIT 1``).
        """
        doc_hash = EncryptionService.instance().hash_deterministic(
            _NAO_DIGITO.sub("", documento)
        )
        stmt = (
            select(OrdemDeServico)
            .join(clientes_table, _t.c.cliente_id == clientes_table.c.id)
            .join(veiculos_table, _t.c.veiculo_id == veiculos_table.c.id)
            .where(veiculos_table.c.placa == placa.upper().replace("-", ""))
            .where(clientes_table.c.documento_hash == doc_hash)
            .order_by(_t.c.criado_em.desc(), _t.c.id.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()
