"""Mapeamento imperativo SQLAlchemy da ``OrdemDeServico`` e da ``Saga``.

Tabelas ``ordens_de_servico``, ``historico_status_ordem`` (uma linha por
``MudancaDeStatus``) e ``sagas`` (uma instancia por OS, RFC-004 secao 7.2).
Decisoes:

- ``versao`` e o ``version_id_col`` do mapper: todo UPDATE sai com
  ``WHERE versao = <lida>`` e incrementa; 0 linhas afetadas vira
  ``StaleDataError`` (o repositorio traduz para 409). E o *reread value* do
  material de SAGA aplicado no banco.
- Enums viram VARCHAR com o ``.value`` (``values_callable``), sem tipo
  nativo no Postgres: novo status nao exige ALTER TYPE.
- Os resumos de orcamento/pagamento sao ``composite`` sobre colunas planas:
  o atributo e instrumentado, entao trocar so o VO (ex.: novo estado do
  pagamento sem mudanca de status) suja a instancia, sai no UPDATE e sobe a
  ``versao``. Todas as colunas nulas = OS ainda sem aquele resumo.
- A saga tem o proprio ``version_id_col`` (``sagas.versao``) e guarda
  passos, plano, comando em voo e itens em JSONB: a ``Saga`` troca a lista
  ou o dict inteiro a cada mudanca, porque mutacao no lugar nao suja o
  atributo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    JSON,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, composite, registry, relationship

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.infraestrutura.database import metadata
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
)
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.saga import Saga
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

if TYPE_CHECKING:
    from collections.abc import Iterable
    from enum import StrEnum

    from src.compartilhado.dominio.aggregate_root import AggregateRoot

_TAMANHO_ENUM = 30
_TAMANHO_CODIGO = 30
# JSONB no Postgres; a variante sqlite so existe para create_all de teste.
_JSON = JSONB().with_variant(JSON(), "sqlite")
# traceparent W3C da versao 00: 55 caracteres.
_TAMANHO_TRACEPARENT = 55


def _enum(tipo: type[StrEnum], nome: str) -> Enum:
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
    Column("orcamento_valido_ate", DateTime(timezone=True), nullable=True),
    Column("pagamento_id", Uuid, nullable=True),
    Column(
        "pagamento_status", _enum(StatusPagamento, "status_pagamento"), nullable=True
    ),
    Column("pagamento_valor", Numeric(12, 2), nullable=True),
    Column("pagamento_moeda", String(3), nullable=True),
    Column("pagamento_checkout_url", String(TAMANHO_MAXIMO_URL), nullable=True),
    Column("pagamento_expira_em", DateTime(timezone=True), nullable=True),
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
    # sub do JWT (UUID) ou o processo; nulo so nas linhas anteriores a coluna.
    Column("ator", String(64), nullable=True),
    Column("ocorrido_em", DateTime(timezone=True), nullable=False),
    # Uma linha por posicao: a UNIQUE tambem e o indice do load por ordem_id
    # e barra duas transicoes gravando a mesma posicao.
    UniqueConstraint(
        "ordem_id", "sequencia", name="uq_historico_status_ordem_sequencia"
    ),
)

sagas_table = Table(
    "sagas",
    metadata,
    Column(
        "ordem_id",
        Uuid,
        ForeignKey("ordens_de_servico.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("etapa", _enum(EtapaSaga, "etapa_saga"), nullable=False),
    # Codigos (motivo da compensacao, falha); texto livre nunca entra na saga.
    Column("motivo", String(_TAMANHO_CODIGO), nullable=True),
    Column("falha", String(_TAMANHO_CODIGO), nullable=True),
    Column("passos", _JSON, nullable=False),
    Column("passos_concluidos", _JSON, nullable=False),
    Column("comando_em_voo", _JSON, nullable=True),
    Column("plano_compensacao", _JSON, nullable=False),
    Column("itens", _JSON, nullable=False),
    Column("reenvios", Integer, nullable=False),
    Column("prazo_resposta_em", DateTime(timezone=True), nullable=True),
    Column("traceparent", String(_TAMANHO_TRACEPARENT), nullable=True),
    Column("iniciada_em", DateTime(timezone=True), nullable=False),
    Column("etapa_desde", DateTime(timezone=True), nullable=False),
    Column("atualizada_em", DateTime(timezone=True), nullable=False),
    Column("versao", Integer, nullable=False),
)
# Candidatas a prazo vencido, por indice (RFC-004 secao 4.6).
Index(
    "ix_sagas_prazo",
    sagas_table.c.prazo_resposta_em,
    postgresql_where=sagas_table.c.prazo_resposta_em.is_not(None),
)
# Gauges da saga: instancias ativas e a mais antiga por etapa (RFC-004 secao 9).
Index(
    "ix_sagas_ativas",
    sagas_table.c.etapa,
    sagas_table.c.etapa_desde,
    postgresql_where=sagas_table.c.etapa.not_in(["concluida", "compensada"]),
)

_COLUNAS_ORCAMENTO = (
    "_orcamento_id",
    "_orcamento_total",
    "_orcamento_moeda",
    "_orcamento_link_decisao",
    "_orcamento_valido_ate",
)
_COLUNAS_PAGAMENTO = (
    "_pagamento_id",
    "_pagamento_status",
    "_pagamento_valor",
    "_pagamento_moeda",
    "_pagamento_checkout_url",
    "_pagamento_expira_em",
)


# Fabricas dos composites: recebem os valores crus das colunas, na ordem de
# _COLUNAS_*, e o VO revalida tudo (Any: o tipo vem do driver, nao do dominio).
def _orcamento_das_colunas(*colunas: Any) -> ResumoOrcamento | None:  # noqa: ANN401
    orcamento_id, total, moeda, link_decisao, valido_ate = colunas
    if orcamento_id is None:
        return None
    return ResumoOrcamento(
        orcamento_id=orcamento_id,
        total=Dinheiro(valor=total, moeda=moeda),
        link_decisao=link_decisao,
        valido_ate=valido_ate,
    )


def _pagamento_das_colunas(*colunas: Any) -> ResumoPagamento | None:  # noqa: ANN401
    pagamento_id, status, valor, moeda, checkout_url, expira_em = colunas
    if pagamento_id is None:
        return None
    return ResumoPagamento(
        pagamento_id=pagamento_id,
        status=status,
        valor=Dinheiro(valor=valor, moeda=moeda),
        checkout_url=checkout_url,
        expira_em=expira_em,
    )


def _colunas_do_orcamento(resumo: ResumoOrcamento) -> tuple[object, ...]:
    return (
        resumo.orcamento_id,
        resumo.total.valor,
        resumo.total.moeda,
        resumo.link_decisao,
        resumo.valido_ate,
    )


def _colunas_do_pagamento(resumo: ResumoPagamento) -> tuple[object, ...]:
    return (
        resumo.pagamento_id,
        resumo.status,
        resumo.valor.valor,
        resumo.valor.moeda,
        resumo.checkout_url,
        resumo.expira_em,
    )


_RESUMOS: Final = frozenset({"_resumo_orcamento", "_resumo_pagamento"})


def _ao_carregar(target: AggregateRoot, _contexto: object) -> None:
    """Listener ``load``: o SQLAlchemy nao chama ``__init__`` na reidratacao,
    entao cria a lista de eventos para os metodos de dominio funcionarem."""
    object.__setattr__(target, "_eventos_pendentes", [])


def _ao_recarregar(
    target: OrdemDeServico, _contexto: object, atributos: Iterable[str] | None
) -> None:
    """Listener ``refresh``: estado relido do banco descarta eventos pendentes.

    O composite remonta o VO no primeiro acesso depois de um flush e avisa
    com um ``refresh`` so com a chave dele. Isso nao e releitura do banco e
    nao pode apagar os eventos pendentes do agregado.
    """
    if atributos is not None and set(atributos) <= _RESUMOS:
        return
    object.__setattr__(target, "_eventos_pendentes", [])


def _gravar_contexto_de_trace(
    sessao: Session, _contexto: object, _instancias: object
) -> None:
    """Antes do flush, a saga alterada leva o contexto do span corrente (ADR-043).

    No mesmo UPDATE da transicao, mesmo quando o flush vem de outro
    repositorio (o da OS grava antes da saga); fora de um span, fica o
    anterior.
    """
    traceparent = cabecalhos_do_contexto_atual().get("traceparent")
    if traceparent is None:
        return
    for instancia in (*sessao.new, *sessao.dirty):
        if isinstance(instancia, Saga):
            instancia.registrar_contexto_de_trace(traceparent)


_mapeamento_iniciado = False


def iniciar_mapeamentos() -> None:
    """Mapeia ``OrdemDeServico``, ``MudancaDeStatus`` e ``Saga`` nas tabelas.

    Idempotente: so a primeira chamada faz algo. Quem chama e
    ``bootstrap.iniciar_todos_mapeamentos``, depois de ``cliente_veiculo``,
    cujas tabelas as chaves estrangeiras da ordem referenciam. Liga o
    ``__composite_values__`` dos resumos e os listeners de ``load`` e
    ``refresh`` que mantem a lista de eventos pendentes.
    """
    global _mapeamento_iniciado  # noqa: PLW0603  # init-once flag
    if _mapeamento_iniciado:
        return
    _mapeamento_iniciado = True

    # O composite le os valores do VO por ``__composite_values__``; o metodo e
    # pendurado aqui (mapeamento imperativo) para o dominio nao conhecer a
    # ordem das colunas.
    ResumoOrcamento.__composite_values__ = _colunas_do_orcamento  # type: ignore[attr-defined]
    ResumoPagamento.__composite_values__ = _colunas_do_pagamento  # type: ignore[attr-defined]

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
            "_ator": historico_status_ordem_table.c.ator,
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
            "_orcamento_valido_ate": t.c.orcamento_valido_ate,
            "_resumo_orcamento": composite(_orcamento_das_colunas, *_COLUNAS_ORCAMENTO),
            "_pagamento_id": t.c.pagamento_id,
            "_pagamento_status": t.c.pagamento_status,
            "_pagamento_valor": t.c.pagamento_valor,
            "_pagamento_moeda": t.c.pagamento_moeda,
            "_pagamento_checkout_url": t.c.pagamento_checkout_url,
            "_pagamento_expira_em": t.c.pagamento_expira_em,
            "_resumo_pagamento": composite(_pagamento_das_colunas, *_COLUNAS_PAGAMENTO),
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

    sg = sagas_table
    mapper_registry.map_imperatively(
        Saga,
        sg,
        version_id_col=sg.c.versao,
        properties={
            "id": sg.c.ordem_id,
            "_etapa": sg.c.etapa,
            "_motivo": sg.c.motivo,
            "_falha": sg.c.falha,
            "_passos": sg.c.passos,
            "_passos_concluidos": sg.c.passos_concluidos,
            "_comando_em_voo": sg.c.comando_em_voo,
            "_plano_compensacao": sg.c.plano_compensacao,
            "_itens": sg.c.itens,
            "_reenvios": sg.c.reenvios,
            "_prazo_resposta_em": sg.c.prazo_resposta_em,
            "_traceparent": sg.c.traceparent,
            "_iniciada_em": sg.c.iniciada_em,
            "_etapa_desde": sg.c.etapa_desde,
            "_atualizada_em": sg.c.atualizada_em,
            "_versao": sg.c.versao,
        },
    )

    event.listen(OrdemDeServico, "load", _ao_carregar)
    event.listen(OrdemDeServico, "refresh", _ao_recarregar)
    event.listen(Saga, "load", _ao_carregar)
    event.listen(Session, "before_flush", _gravar_contexto_de_trace)
