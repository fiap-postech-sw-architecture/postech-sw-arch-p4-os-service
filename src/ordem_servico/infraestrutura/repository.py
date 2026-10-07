"""Implementacao SQLAlchemy do repositorio de OrdemDeServico."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import case, func, select
from sqlalchemy.orm.exc import StaleDataError

from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
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


class SagaSQLAlchemyRepository:
    """``SagaRepository`` sobre a ``Session`` da transacao (API ou mensagem)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def obter(self, ordem_id: UUID) -> Saga | None:
        return self._session.get(Saga, ordem_id)

    def salvar(self, saga: Saga) -> None:
        """Grava o contexto de trace corrente e faz flush com lock otimista.

        O ``traceparent`` do span em curso (a requisicao da abertura, o
        consumo do evento) vira o da saga (ADR-043); fora de um span, fica o
        anterior. Versao divergente vira ``ConflitoDeConcorrenciaException``.
        """
        ordem_id = saga.ordem_id
        saga.traceparent = cabecalhos_do_contexto_atual().get(
            "traceparent", saga.traceparent
        )
        self._session.add(saga)
        try:
            self._session.flush()
        except StaleDataError:
            raise ConflitoDeConcorrenciaException(
                mensagem=f"Saga {ordem_id} alterada por outra operacao; releia"
            ) from None
