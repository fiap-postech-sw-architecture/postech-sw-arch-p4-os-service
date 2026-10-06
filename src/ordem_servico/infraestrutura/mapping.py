"""Mapeamento imperativo SQLAlchemy do agregado ``OrdemDeServico``.

Tabelas ``ordens_de_servico`` e ``historico_status_ordem`` (uma linha por
``MudancaDeStatus``). Decisoes:

- ``versao`` e o ``version_id_col`` do mapper: todo UPDATE sai com
  ``WHERE versao = <lida>`` e incrementa; 0 linhas afetadas vira
  ``StaleDataError`` (o repositorio traduz para 409). E o *reread value* do
  material de SAGA aplicado no banco.
- Enums viram VARCHAR com o ``.value`` (``values_callable``), sem tipo
  nativo no Postgres: novo status nao exige ALTER TYPE.
- Os resumos de orcamento/pagamento sao colunas planas; os listeners
  ``load``/``refresh`` recompoem os VOs e ``before_insert``/``before_update``
  os decompoem (mesmo padrao do p3 para ``Dinheiro``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import registry, relationship

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.infraestrutura.database import metadata
from src.ordem_servico.dominio.historico import MudancaDeStatus, OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import (
    TAMANHO_MAXIMO_DESCRICAO,
    TAMANHO_MAXIMO_MOTIVO,
    OrdemDeServico,
)
from src.ordem_servico.dominio.resumos import (
    TAMANHO_MAXIMO_URL,
    ResumoOrcamento,
    ResumoPagamento,
    StatusPagamento,
)
from src.ordem_servico.dominio.status import StatusOrdem

_TAMANHO_ENUM = 30


def _enum(tipo: type[Any], nome: str) -> Enum:
    """VARCHAR com o ``.value`` do enum (sem CHECK e sem tipo nativo)."""
    return Enum(
        tipo,
        name=nome,
        native_enum=False,
        create_constraint=False,
        length=_TAMANHO_ENUM,
        values_callable=lambda membros: [m.value for m in membros],
    )


ordens_de_servico_table = Table(
    "ordens_de_servico",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("cliente_id", Uuid, ForeignKey("clientes.id"), nullable=False),
    Column("veiculo_id", Uuid, ForeignKey("veiculos.id"), nullable=False),
    Column("descricao_problema", String(TAMANHO_MAXIMO_DESCRICAO), nullable=False),
    Column("status", _enum(StatusOrdem, "status_ordem"), nullable=False),
    Column("orcamento_id", Uuid, nullable=True),
    Column("orcamento_total", Numeric(12, 2), nullable=True),
    Column("orcamento_moeda", String(3), nullable=True),
    Column("orcamento_link_decisao", String(TAMANHO_MAXIMO_URL), nullable=True),
    Column("pagamento_id", Uuid, nullable=True),
    Column(
        "pagamento_status", _enum(StatusPagamento, "status_pagamento"), nullable=True
    ),
    Column("pagamento_checkout_url", String(TAMANHO_MAXIMO_URL), nullable=True),
    Column("motivo_cancelamento", String(TAMANHO_MAXIMO_MOTIVO), nullable=True),
    Column("versao", Integer, nullable=False),
    Column("criado_em", DateTime(timezone=True), nullable=False),
    Column("atualizado_em", DateTime(timezone=True), nullable=False),
)

# Filtros por cliente/veiculo + status: OS ativa no LGPD/desativacao do
# cliente e o join do acompanhamento publico por placa.
Index(
    "ix_ordens_de_servico_cliente_status",
    ordens_de_servico_table.c.cliente_id,
    ordens_de_servico_table.c.status,
)
Index(
    "ix_ordens_de_servico_veiculo_status",
    ordens_de_servico_table.c.veiculo_id,
    ordens_de_servico_table.c.status,
)

historico_status_ordem_table = Table(
    "historico_status_ordem",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "ordem_id",
        Uuid,
        ForeignKey("ordens_de_servico.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("sequencia", Integer, nullable=False),
    Column("de", _enum(StatusOrdem, "status_ordem"), nullable=True),
    Column("para", _enum(StatusOrdem, "status_ordem"), nullable=False),
    Column("origem", _enum(OrigemMudanca, "origem_mudanca"), nullable=False),
    Column("motivo", String(TAMANHO_MAXIMO_MOTIVO), nullable=True),
    Column("ocorrido_em", DateTime(timezone=True), nullable=False),
    # Uma linha por posicao: a UNIQUE tambem e o indice do load por ordem_id
    # e barra duas transicoes gravando a mesma posicao.
    UniqueConstraint(
        "ordem_id", "sequencia", name="uq_historico_status_ordem_sequencia"
    ),
)

_COLUNAS_ORCAMENTO = (
    "_orcamento_id",
    "_orcamento_total",
    "_orcamento_moeda",
    "_orcamento_link_decisao",
)
_COLUNAS_PAGAMENTO = ("_pagamento_id", "_pagamento_status", "_pagamento_checkout_url")


def _resumo_orcamento(estado: dict[str, Any]) -> ResumoOrcamento | None:
    if estado.get("_orcamento_id") is None:
        return None
    return ResumoOrcamento(
        orcamento_id=estado["_orcamento_id"],
        total=Dinheiro(
            valor=estado["_orcamento_total"], moeda=estado["_orcamento_moeda"]
        ),
        link_decisao=estado["_orcamento_link_decisao"],
    )


def _resumo_pagamento(estado: dict[str, Any]) -> ResumoPagamento | None:
    if estado.get("_pagamento_id") is None:
        return None
    return ResumoPagamento(
        pagamento_id=estado["_pagamento_id"],
        status=estado["_pagamento_status"],
        checkout_url=estado["_pagamento_checkout_url"],
    )


def _colunas_do_orcamento(resumo: ResumoOrcamento | None) -> tuple[object, ...]:
    if resumo is None:
        return (None,) * len(_COLUNAS_ORCAMENTO)
    return (
        resumo.orcamento_id,
        resumo.total.valor,
        resumo.total.moeda,
        resumo.link_decisao,
    )


def _colunas_do_pagamento(resumo: ResumoPagamento | None) -> tuple[object, ...]:
    if resumo is None:
        return (None,) * len(_COLUNAS_PAGAMENTO)
    return (resumo.pagamento_id, resumo.status, resumo.checkout_url)


def _reconstruir_os(target: OrdemDeServico, *_args: object) -> None:
    """Listener ``load``/``refresh``: recompoe os VOs a partir das colunas planas.

    ``refresh`` (aridade diferente, absorvida por ``*_args``) cobre a releitura
    por session.refresh/expire: sem ele os VOs ficariam stale.
    """
    estado: dict[str, Any] = target.__dict__
    object.__setattr__(target, "_resumo_orcamento", _resumo_orcamento(estado))
    object.__setattr__(target, "_resumo_pagamento", _resumo_pagamento(estado))
    # SQLAlchemy nao chama __init__ na reidratacao: rearma a lista de eventos
    # para os metodos de dominio funcionarem em instancia carregada.
    object.__setattr__(target, "_eventos_pendentes", [])


def _decompor_os(_mapper: object, _connection: object, target: OrdemDeServico) -> None:
    """Listener ``before_insert``/``before_update``: VOs -> colunas planas."""
    colunas = zip(
        (*_COLUNAS_ORCAMENTO, *_COLUNAS_PAGAMENTO),
        (
            *_colunas_do_orcamento(target.resumo_orcamento),
            *_colunas_do_pagamento(target.resumo_pagamento),
        ),
        strict=True,
    )
    for nome, valor in colunas:
        setattr(target, nome, valor)


_mapeamento_iniciado = False


def iniciar_mapeamentos() -> None:
    global _mapeamento_iniciado  # noqa: PLW0603  # init-once flag
    if _mapeamento_iniciado:
        return
    _mapeamento_iniciado = True

    mapper_registry = registry()

    mapper_registry.map_imperatively(
        MudancaDeStatus,
        historico_status_ordem_table,
        properties={
            "id": historico_status_ordem_table.c.id,
            "_sequencia": historico_status_ordem_table.c.sequencia,
            "_de": historico_status_ordem_table.c.de,
            "_para": historico_status_ordem_table.c.para,
            "_origem": historico_status_ordem_table.c.origem,
            "_motivo": historico_status_ordem_table.c.motivo,
            "_ocorrido_em": historico_status_ordem_table.c.ocorrido_em,
        },
    )

    t = ordens_de_servico_table
    mapper_registry.map_imperatively(
        OrdemDeServico,
        t,
        version_id_col=t.c.versao,
        properties={
            "id": t.c.id,
            "_cliente_id": t.c.cliente_id,
            "_veiculo_id": t.c.veiculo_id,
            "_descricao_problema": t.c.descricao_problema,
            "_status": t.c.status,
            "_orcamento_id": t.c.orcamento_id,
            "_orcamento_total": t.c.orcamento_total,
            "_orcamento_moeda": t.c.orcamento_moeda,
            "_orcamento_link_decisao": t.c.orcamento_link_decisao,
            "_pagamento_id": t.c.pagamento_id,
            "_pagamento_status": t.c.pagamento_status,
            "_pagamento_checkout_url": t.c.pagamento_checkout_url,
            "_motivo_cancelamento": t.c.motivo_cancelamento,
            "_versao": t.c.versao,
            "_criado_em": t.c.criado_em,
            "_atualizado_em": t.c.atualizado_em,
            "_historico": relationship(
                MudancaDeStatus,
                lazy="selectin",
                cascade="all, delete-orphan",
                order_by=historico_status_ordem_table.c.sequencia,
            ),
        },
    )

    event.listen(OrdemDeServico, "load", _reconstruir_os)
    event.listen(OrdemDeServico, "refresh", _reconstruir_os)
    event.listen(OrdemDeServico, "before_insert", _decompor_os)
    event.listen(OrdemDeServico, "before_update", _decompor_os)
