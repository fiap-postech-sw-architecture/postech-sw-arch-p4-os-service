"""Query services de leitura do contexto OS.

O acompanhamento projeta so as colunas que expoe; a consulta da OS hidrata a OS
e a saga juntas, num SELECT, para o status e a etapa sairem do mesmo instante; as
metricas das OS contam por status e medem a permanencia em cada um, e sao a
mesma leitura da rota ``/metricas`` e dos gauges do coletor da API.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import String, func, select, type_coerce

from src.cliente_veiculo.infraestrutura.mapping import clientes_table, veiculos_table
from src.compartilhado.infraestrutura.encryption import EncryptionService
from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO, MetricasDasOrdensDTO
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.mapping import (
    historico_status_ordem_table,
    ordens_de_servico_table,
    sagas_table,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import Connection
    from sqlalchemy.orm import Session

    from src.compartilhado.dominio.documento import Documento
    from src.compartilhado.dominio.placa import Placa

_t = ordens_de_servico_table
_h = historico_status_ordem_table

# A permanencia que entra na media: a que comecou nesta janela (a linha do
# historico em que a OS entrou no status).
JANELA_DAS_PERMANENCIAS: Final = timedelta(days=30)


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


class ConsultaDaOrdemSQLAlchemy:
    """``ConsultaDaOrdem``: a OS e a saga dela num SELECT so (LEFT JOIN)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def com_saga(self, ordem_id: UUID) -> tuple[OrdemDeServico, Saga | None] | None:
        linha = self._session.execute(
            select(OrdemDeServico, Saga)
            .outerjoin(Saga, sagas_table.c.ordem_id == _t.c.id)
            .where(_t.c.id == ordem_id)
        ).first()
        if linha is None:
            return None
        ordem, saga = linha
        return ordem, saga


def ler_metricas_das_ordens(
    origem: Session | Connection, agora: datetime
) -> MetricasDasOrdensDTO:
    """Total, OS por status e permanencia media por status (RF-027 da fase 3).

    A permanencia no status e o tempo da linha do historico em que a OS entrou
    nele ate a linha seguinte da mesma OS (``lead`` por ``sequencia``); a OS que
    ainda esta no status, e os status finais, que nao tem linha seguinte, ficam
    fora. Entram as entradas dos ``JANELA_DAS_PERMANENCIAS`` ate ``agora``, pelo
    indice de ``ocorrido_em``, e o relogio vem de quem chama. Os relogios de
    replicas diferentes gravam o ``ocorrido_em``, e uma permanencia negativa por
    desvio de relogio conta como zero. O status vem como texto: o de outra
    versao (rollout com versoes misturadas) fica fora, e os nove status do
    ``StatusOrdem`` saem sempre em ``por_status``, com zero onde nao ha OS.
    """
    contagem = dict(
        origem.execute(
            select(type_coerce(_t.c.status, String), func.count()).group_by(_t.c.status)
        )
        .tuples()
        .all()
    )
    entradas = (
        select(
            type_coerce(_h.c.para, String).label("status"),
            _h.c.ocorrido_em,
            func.lead(_h.c.ocorrido_em)
            .over(partition_by=_h.c.ordem_id, order_by=_h.c.sequencia)
            .label("saiu_em"),
        )
        .where(_h.c.ocorrido_em >= agora - JANELA_DAS_PERMANENCIAS)
        .subquery("entradas")
    )
    permanencia = func.greatest(
        func.extract("epoch", entradas.c.saiu_em - entradas.c.ocorrido_em), 0
    )
    medias = dict(
        origem.execute(
            select(entradas.c.status, func.avg(permanencia))
            .where(entradas.c.saiu_em.is_not(None))
            .group_by(entradas.c.status)
        )
        .tuples()
        .all()
    )
    por_status = {s.value: int(contagem.get(s.value, 0)) for s in StatusOrdem}
    return MetricasDasOrdensDTO(
        total=sum(por_status.values()),
        por_status=por_status,
        tempo_medio_por_status_segundos={
            s.value: float(medias[s.value]) for s in StatusOrdem if s.value in medias
        },
    )


class ConsultaDeMetricasSQLAlchemy:
    """``ConsultaDeMetricas`` sobre a ``Session`` da requisicao."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ler(self, agora: datetime) -> MetricasDasOrdensDTO:
        return ler_metricas_das_ordens(self._session, agora)
