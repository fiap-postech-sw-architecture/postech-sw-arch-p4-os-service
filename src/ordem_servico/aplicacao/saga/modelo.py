"""Tipos da saga de atendimento: etapas, passos, envio, fatos e erros.

A saga guarda so codigos (etapa, gatilho, comando, motivo): texto livre e dado
pessoal ficam na OS (RFC-004 secao 7.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from src.compartilhado.dominio.events import DomainEvent
from src.compartilhado.dominio.exceptions import EntidadeNaoEncontradaException

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import Comando


class EtapaSaga(StrEnum):
    """Etapas da instancia (RFC-004 secao 4.1), em minusculas como o status da OS.

    Etapa (estado do orquestrador) e status (o que cliente e atendente veem)
    sao campos diferentes, com dois nomes em comum; o valor e tambem o label
    ``etapa`` das metricas.
    """

    AGUARDANDO_DIAGNOSTICO = "aguardando_diagnostico"
    AGUARDANDO_ORCAMENTO = "aguardando_orcamento"
    AGUARDANDO_DECISAO = "aguardando_decisao"
    AGUARDANDO_RESERVA = "aguardando_reserva"
    AGUARDANDO_PAGAMENTO = "aguardando_pagamento"
    AGUARDANDO_AGENDAMENTO = "aguardando_agendamento"
    AGUARDANDO_INICIO = "aguardando_inicio"
    EM_EXECUCAO = "em_execucao"
    CONCLUIDA = "concluida"
    COMPENSANDO = "compensando"
    COMPENSADA = "compensada"
    FALHA_NA_COMPENSACAO = "falha_na_compensacao"


class RegistroDaSaga(TypedDict):
    """Registro da linha do tempo da saga (coluna ``passos``): so codigos.

    Um por transicao aplicada, inclusive as que nao concluem um passo T (o
    ``DiagnosticoIniciado``, o ``PagamentoSolicitado``): ``gatilho`` e o tipo
    do evento ou ``abertura``; ``comando`` e ``comando_id``, o comando que a
    transicao enviou. ``em`` e ISO 8601 em UTC. Nunca texto livre.
    """

    seq: int
    em: str
    de: str | None
    para: str
    gatilho: str
    mensagem_id: str | None
    comando: str | None
    comando_id: str | None
    motivo: str | None
    ator: str
    # So no registro do ExecucaoAgendada: a fila viva e a da Execucao.
    posicao_na_fila: NotRequired[int]


# JSON ja validado pelo schema do contrato (contratos/, RFC-004 secao 5.5): os
# ``dados`` das mensagens e dos comandos.
type DadosDoContrato = Mapping[str, Any]


class ComandoEmVoo(TypedDict):
    """Comando com prazo tecnico a espera de resposta (RFC-004 secao 7.2).

    ``mensagem_ids`` tem o id de cada envio (o original e, depois, os
    reenvios): a resposta casa pelo ``causation_id``, e o ultimo e o mais
    recente.
    """

    tipo: str
    dados: DadosDoContrato
    mensagem_ids: list[str]
    enviado_em: str


class ItemDoDiagnostico(TypedDict):
    """Servico ou peca do ``DiagnosticoConcluido``, guardado para o ReservarPecas."""

    tipo: str
    codigo: str
    quantidade: int


@dataclass(frozen=True, slots=True)
class Envio:
    """Comando gravado na outbox num passo: tipo, id do envelope e ``dados``.

    Com ``prazo_resposta_em``, o comando tem resposta automatica e vira o
    comando em voo da saga. ``dados`` e uma copia so de leitura (mudar o dict
    de quem montou o envio nao muda o VO) e fica fora do ``hash``.
    """

    tipo: Comando
    id: UUID
    dados: DadosDoContrato = field(default_factory=dict, hash=False)
    prazo_resposta_em: datetime | None = None

    def __post_init__(self) -> None:
        congelados = MappingProxyType(deepcopy(dict(self.dados)))
        object.__setattr__(self, "dados", congelados)


@dataclass(frozen=True, slots=True)
class SagaIniciadaEvent(DomainEvent):
    """Saga aberta junto com a OS."""


@dataclass(frozen=True, slots=True)
class EtapaDaSagaAlteradaEvent(DomainEvent):
    """A saga saiu de ``etapa_anterior`` depois de ``permanencia`` nela."""

    etapa_anterior: EtapaSaga = field(kw_only=True)
    etapa_nova: EtapaSaga = field(kw_only=True)
    permanencia: timedelta = field(kw_only=True)


class TransicaoDaSagaInvalidaError(Exception):
    """Evento aplicado fora da etapa em que ele e esperado: bug de quem chama."""


class SagaNaoEncontradaException(EntidadeNaoEncontradaException):
    """A OS nao tem saga: 404 na API; no consumidor, erro permanente (DLQ)."""

    def __init__(self, ordem_id: UUID) -> None:
        super().__init__(mensagem=f"Saga da ordem {ordem_id} nao encontrada")


def itens_do_diagnostico(dados: DadosDoContrato) -> list[ItemDoDiagnostico]:
    """Itens do ``DiagnosticoConcluido`` so com os campos do contrato.

    O leitor e tolerante (RFC-004 secao 5.5): campo a mais no item nao entra
    na saga nem no ``GerarOrcamento``.
    """
    return [
        {
            "tipo": item["tipo"],
            "codigo": item["codigo"],
            "quantidade": item["quantidade"],
        }
        for item in dados["itens"]
    ]
