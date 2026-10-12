"""Tipos da saga de atendimento: etapas, registros, envio, transicao, fatos e erros.

A saga guarda so codigos (etapa, gatilho, comando, motivo): texto livre e dado
pessoal ficam na OS (RFC-004 secao 7.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, NotRequired, TypedDict

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


class MotivoCompensacao(StrEnum):
    """Por que a saga compensa (RFC-004 secao 9): um codigo, nunca texto livre.

    Vai no ``motivo`` dos comandos de compensacao (RFC-004 secao 5.3), na saga
    e no label ``motivo`` de ``pytstop_saga_compensacoes_total``. O texto que o
    atendente escreve no cancelamento fica so na OS.
    """

    ORCAMENTO_RECUSADO = "orcamento_recusado"
    ORCAMENTO_EXPIRADO = "orcamento_expirado"
    GERACAO_FALHOU = "geracao_falhou"
    RESERVA_FALHOU = "reserva_falhou"
    PAGAMENTO_RECUSADO = "pagamento_recusado"
    PAGAMENTO_EXPIRADO = "pagamento_expirado"
    CANCELAMENTO = "cancelamento"
    PRAZO_TECNICO = "prazo_tecnico"


# Palavras do ``gatilho`` que nao sao tipo de mensagem (lista fechada): o
# registro de um evento leva o tipo dele, e cada pedido ou decisao do proprio
# OS, uma destas.
GATILHO_ABERTURA: Final = "abertura"
GATILHO_CANCELAMENTO_SOLICITADO: Final = "cancelamento_solicitado"
GATILHO_CANCELAMENTO_RECUSADO: Final = "cancelamento_recusado"
GATILHO_REENVIO: Final = "reenvio"
GATILHO_PRAZO_ESGOTADO: Final = "prazo_esgotado"
GATILHO_RETOMADA: Final = "retomada"
GATILHOS_DE_PROCESSO: Final = frozenset(
    {
        GATILHO_ABERTURA,
        GATILHO_CANCELAMENTO_SOLICITADO,
        GATILHO_CANCELAMENTO_RECUSADO,
        GATILHO_REENVIO,
        GATILHO_PRAZO_ESGOTADO,
        GATILHO_RETOMADA,
    }
)


class RegistroDaSaga(TypedDict):
    """Registro da linha do tempo da saga (coluna ``passos``): so codigos.

    Um por transicao aplicada, inclusive as que nao concluem um passo T (o
    ``DiagnosticoIniciado``, o ``PagamentoSolicitado``): ``gatilho`` e o tipo
    do evento ou uma das palavras de ``GATILHOS_DE_PROCESSO``; ``comando`` e
    ``comando_id``, o comando que a transicao enviou. ``em`` e ISO 8601 em
    UTC. Nunca texto livre.
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
    # So no registro do reenvio por prazo: quantos reenvios o comando ja teve.
    reenvio: NotRequired[int]


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

    def em_voo(self, enviado_em: datetime) -> ComandoEmVoo:
        """O comando em voo que este envio abre: o id dele e o primeiro envio."""
        return {
            "tipo": self.tipo.value,
            "dados": deepcopy(dict(self.dados)),
            "mensagem_ids": [str(self.id)],
            "enviado_em": enviado_em.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Gatilho:
    """O que mudou a saga, quando e por quem: o cabecalho de cada registro.

    ``tipo`` e o tipo do evento ou uma palavra de ``GATILHOS_DE_PROCESSO``
    (``abertura`` e ``cancelamento_solicitado`` nos pedidos pela API);
    ``mensagem_id``, o id do evento (``None`` nos pedidos); ``ator``, o
    processo ou o ``sub`` de quem pediu.
    """

    tipo: str
    mensagem_id: UUID | None
    em: datetime
    ator: str

    def registro(
        self,
        seq: int,
        de: EtapaSaga | None,
        para: EtapaSaga,
        envio: Envio | None,
        motivo: str | None,
    ) -> RegistroDaSaga:
        """O registro da saga deste gatilho: de que etapa para qual e o que enviou."""
        return {
            "seq": seq,
            "em": self.em.isoformat(),
            "de": de.value if de is not None else None,
            "para": para.value,
            "gatilho": self.tipo,
            "mensagem_id": str(self.mensagem_id) if self.mensagem_id else None,
            "comando": envio.tipo.value if envio is not None else None,
            "comando_id": str(envio.id) if envio is not None else None,
            "motivo": motivo,
            "ator": self.ator,
        }


@dataclass(frozen=True, slots=True)
class Transicao:
    """O que um gatilho faz com a saga (RFC-004 secoes 4.1 e 4.4).

    ``seguinte`` e a etapa depois dele; ``comando``, o que o envio tem de ser
    (nenhum envio com ``None``); ``concluido``, o passo T que ele conclui, que
    a compensacao desfaz. ``motivo``, ``plano`` e ``falha`` sao os da
    compensacao depois dele (vazios no fluxo normal), e ``gatilho``, a palavra
    que o registro leva no lugar do tipo do evento (``cancelamento_recusado``).
    """

    seguinte: EtapaSaga
    comando: Comando | None = None
    concluido: str | None = None
    motivo: str | None = None
    plano: tuple[Comando, ...] = ()
    falha: str | None = None
    gatilho: str | None = None


@dataclass(frozen=True, slots=True)
class SagaIniciadaEvent(DomainEvent):
    """Saga aberta junto com a OS."""


@dataclass(frozen=True, slots=True)
class EtapaDaSagaAlteradaEvent(DomainEvent):
    """A saga saiu de ``etapa_anterior`` depois de ``permanencia`` nela."""

    etapa_anterior: EtapaSaga = field(kw_only=True)
    etapa_nova: EtapaSaga = field(kw_only=True)
    permanencia: timedelta = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class CompensacaoIniciadaEvent(DomainEvent):
    """A saga saiu do fluxo normal para compensar, pelo ``motivo``."""

    motivo: MotivoCompensacao = field(kw_only=True)


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


def pecas_dos_itens(itens: Sequence[ItemDoDiagnostico]) -> list[dict[str, Any]]:
    """Os itens ``peca`` como o ``ReservarPecas`` os pede: ``{sku, quantidade}``.

    Lista vazia vale (diagnostico so com servicos).
    """
    return [
        {"sku": item["codigo"], "quantidade": item["quantidade"]}
        for item in itens
        if item["tipo"] == "peca"
    ]
