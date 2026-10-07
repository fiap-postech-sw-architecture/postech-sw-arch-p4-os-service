"""Instancia da saga de atendimento: o process manager do OS Service.

Uma por OS (``id`` = ``ordem_id`` = ``correlation_id`` das mensagens),
persistida na tabela ``sagas`` como agregado proprio (RFC-004 secao 4; ADR-035
"Estado persistido e eventos fora de ordem"). Aqui ficam so o estado e as
regras, sem I/O: quem le e grava saga e OS e publica os comandos e o
``OrquestradorDaSaga``, pelas portas, numa transacao so.

A saga guarda so codigos (etapa, gatilho, comando, motivo): texto livre e dado
pessoal ficam na OS (RFC-004 secao 7.2).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, NotRequired, TypedDict

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.events import DomainEvent
from src.compartilhado.dominio.exceptions import EntidadeNaoEncontradaException
from src.ordem_servico.dominio.status import StatusOrdem

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime, timedelta
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


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


_E = EtapaSaga

# Ordem linear do fluxo normal; compensando, compensada e falha_na_compensacao
# ficam fora dela (RFC-004 secao 4.1).
FLUXO_DA_SAGA: Final = (
    _E.AGUARDANDO_DIAGNOSTICO,
    _E.AGUARDANDO_ORCAMENTO,
    _E.AGUARDANDO_DECISAO,
    _E.AGUARDANDO_RESERVA,
    _E.AGUARDANDO_PAGAMENTO,
    _E.AGUARDANDO_AGENDAMENTO,
    _E.AGUARDANDO_INICIO,
    _E.EM_EXECUCAO,
    _E.CONCLUIDA,
)
# Etapas de onde a saga ainda sai: os labels de etapa das metricas.
ETAPAS_NAO_FINAIS: Final = tuple(
    e for e in EtapaSaga if e not in {_E.CONCLUIDA, _E.COMPENSADA}
)

# Etapa em que cada evento consumido e esperado (tabela da RFC-004 secao 4.1;
# as falhas de negocio esperam a etapa do passo que falha, e as respostas de
# compensacao, compensando).
ETAPA_ESPERADA: Final[Mapping[str, EtapaSaga]] = MappingProxyType(
    {
        "DiagnosticoIniciado": _E.AGUARDANDO_DIAGNOSTICO,
        "DiagnosticoConcluido": _E.AGUARDANDO_DIAGNOSTICO,
        "OrcamentoGerado": _E.AGUARDANDO_ORCAMENTO,
        "GeracaoDeOrcamentoFalhou": _E.AGUARDANDO_ORCAMENTO,
        "OrcamentoAprovado": _E.AGUARDANDO_DECISAO,
        "OrcamentoRecusado": _E.AGUARDANDO_DECISAO,
        "OrcamentoExpirado": _E.AGUARDANDO_DECISAO,
        "PecasReservadas": _E.AGUARDANDO_RESERVA,
        "ReservaDePecasFalhou": _E.AGUARDANDO_RESERVA,
        "PagamentoSolicitado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoConfirmado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoRecusado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoExpirado": _E.AGUARDANDO_PAGAMENTO,
        "ExecucaoAgendada": _E.AGUARDANDO_AGENDAMENTO,
        "ExecucaoIniciada": _E.AGUARDANDO_INICIO,
        "ExecucaoFinalizada": _E.EM_EXECUCAO,
        "DiagnosticoDescartado": _E.COMPENSANDO,
        "OrcamentoCancelado": _E.COMPENSANDO,
        "ReservaLiberada": _E.COMPENSANDO,
        "PagamentoCancelado": _E.COMPENSANDO,
        "PagamentoEstornado": _E.COMPENSANDO,
        "EstornoDePagamentoFalhou": _E.COMPENSANDO,
        "ExecucaoCancelada": _E.COMPENSANDO,
    }
)
# Etapa seguinte de cada evento do fluxo normal (RFC-004 secao 4.1).
ETAPA_SEGUINTE: Final[Mapping[str, EtapaSaga]] = MappingProxyType(
    {
        "DiagnosticoIniciado": _E.AGUARDANDO_DIAGNOSTICO,
        "DiagnosticoConcluido": _E.AGUARDANDO_ORCAMENTO,
        "OrcamentoGerado": _E.AGUARDANDO_DECISAO,
        "OrcamentoAprovado": _E.AGUARDANDO_RESERVA,
        "PecasReservadas": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoSolicitado": _E.AGUARDANDO_PAGAMENTO,
        "PagamentoConfirmado": _E.AGUARDANDO_AGENDAMENTO,
        "ExecucaoAgendada": _E.AGUARDANDO_INICIO,
        "ExecucaoIniciada": _E.EM_EXECUCAO,
        "ExecucaoFinalizada": _E.CONCLUIDA,
    }
)
# Passo que cada evento conclui: o plano de compensacao desfaz os concluidos
# (RFC-004 secao 4.4).
_PASSO_CONCLUIDO: Final = MappingProxyType(
    {
        "OrcamentoGerado": "T3",
        "PecasReservadas": "T5",
        "PagamentoSolicitado": "T6",
        "ExecucaoAgendada": "T7",
    }
)
# Comandos com resposta automatica, que ganham prazo tecnico (RFC-004 secoes 4.3
# e 4.6). O SolicitarDiagnostico e as esperas humanas nao tem prazo.
COMANDOS_COM_PRAZO: Final = frozenset(
    {
        Comando.GERAR_ORCAMENTO,
        Comando.RESERVAR_PECAS,
        Comando.SOLICITAR_PAGAMENTO,
        Comando.AGENDAR_EXECUCAO,
        Comando.CANCELAR_EXECUCAO,
        Comando.ESTORNAR_PAGAMENTO,
        Comando.LIBERAR_RESERVA,
        Comando.CANCELAR_ORCAMENTO,
        Comando.DESCARTAR_DIAGNOSTICO,
    }
)
GATILHO_ABERTURA: Final = "abertura"


class Classificacao(StrEnum):
    """Como um evento recebido se encaixa na etapa atual (RFC-004 secao 4.5)."""

    PROCESSAR = "processar"
    # Etapa a frente da atual: volta pela fila de retry ate a saga alcanca-lo.
    ADIANTADO = "adiantado"
    # Etapa ja passada, inclusive a resposta que o participante republica
    # quando o comando e reenviado.
    OBSOLETO = "obsoleto"
    # Mesma etapa, com o fato ja aplicado.
    REPETIDO = "repetido"
    # Evento do fluxo normal com a saga encerrada ou em compensacao.
    FORA_DO_FLUXO = "fora_do_fluxo"
    # Resposta de compensacao sem compensacao em curso.
    FORA_DA_COMPENSACAO = "fora_da_compensacao"


class Passo(TypedDict):
    """Linha do tempo da saga (coluna ``passos``): so codigos, nunca texto livre.

    ``gatilho`` e o tipo do evento ou ``abertura``; ``comando`` e
    ``comando_id``, o comando que o passo enviou. ``em`` e ISO 8601 em UTC.
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
    ator: str | None
    # So no passo do ExecucaoAgendada: a fila viva e a da Execucao.
    posicao_na_fila: NotRequired[int]


class ComandoEmVoo(TypedDict):
    """Comando com prazo tecnico a espera de resposta (RFC-004 secao 7.2).

    ``mensagem_ids`` tem o id de cada envio (o original e, depois, os
    reenvios): a resposta casa pelo ``causation_id``, e o ultimo e o mais
    recente.
    """

    tipo: str
    dados: dict[str, Any]
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
    comando em voo da saga.
    """

    tipo: Comando
    id: UUID
    dados: Mapping[str, Any] = field(default_factory=dict)
    prazo_resposta_em: datetime | None = None


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


def itens_do_diagnostico(dados: Mapping[str, Any]) -> list[ItemDoDiagnostico]:
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


@dataclass(eq=False)
class Saga(AggregateRoot):
    """Instancia da saga de uma OS; ``id`` e o ``ordem_id``.

    Construir com ``Saga.iniciar``; o fluxo normal anda com ``avancar``. A
    ``versao`` e o lock otimista da persistencia, como na OS. Listas e dicts
    sao trocados por novos a cada mudanca (a coluna JSONB nao detecta mutacao
    no lugar).
    """

    _etapa: EtapaSaga = field(kw_only=True)
    _iniciada_em: datetime = field(kw_only=True)
    _etapa_desde: datetime = field(kw_only=True)
    _atualizada_em: datetime = field(kw_only=True)
    _motivo: str | None = field(default=None, kw_only=True)
    _falha: str | None = field(default=None, kw_only=True)
    _passos: list[Passo] = field(default_factory=list, kw_only=True, repr=False)
    _passos_concluidos: list[str] = field(default_factory=list, kw_only=True)
    _comando_em_voo: ComandoEmVoo | None = field(default=None, kw_only=True, repr=False)
    _plano_compensacao: list[str] = field(default_factory=list, kw_only=True)
    _itens: list[ItemDoDiagnostico] = field(
        default_factory=list, kw_only=True, repr=False
    )
    _reenvios: int = field(default=0, kw_only=True)
    _prazo_resposta_em: datetime | None = field(default=None, kw_only=True)
    # Contexto W3C da ultima transicao (ADR-043): a persistencia grava o do
    # span corrente ao salvar.
    traceparent: str | None = field(default=None, kw_only=True, repr=False)
    _versao: int = field(default=1, kw_only=True)

    @classmethod
    def iniciar(
        cls, ordem_id: UUID, *, envio: Envio, ator: str | None, agora: datetime
    ) -> Saga:
        """T1: abre a saga em ``aguardando_diagnostico``, com o passo ``abertura``.

        O ``SolicitarDiagnostico`` do ``envio`` nao tem resposta automatica
        (RFC-004 secao 4.3): a saga fica sem comando em voo e sem prazo.
        """
        saga = cls(
            id=ordem_id,
            _etapa=_E.AGUARDANDO_DIAGNOSTICO,
            _iniciada_em=agora,
            _etapa_desde=agora,
            _atualizada_em=agora,
        )
        saga._passos = [
            saga._passo(
                gatilho=GATILHO_ABERTURA,
                de=None,
                mensagem_id=None,
                ator=ator,
                envio=envio,
            )
        ]
        saga._registrar_evento(
            SagaIniciadaEvent(agregado_id=ordem_id, ocorrido_em=agora)
        )
        return saga

    @property
    def ordem_id(self) -> UUID:
        return self.id

    @property
    def etapa(self) -> EtapaSaga:
        return self._etapa

    @property
    def motivo(self) -> str | None:
        """Codigo da compensacao (``orcamento_recusado``, ``cancelamento``...)."""
        return self._motivo

    @property
    def falha(self) -> str | None:
        """``reenvios_esgotados`` ou ``estorno_recusado`` em falha_na_compensacao."""
        return self._falha

    @property
    def passos(self) -> tuple[Passo, ...]:
        """Copia: mudar um passo devolvido nao toca o estado da saga."""
        return tuple(deepcopy(self._passos))

    @property
    def passos_concluidos(self) -> tuple[str, ...]:
        return tuple(self._passos_concluidos)

    @property
    def comando_em_voo(self) -> ComandoEmVoo | None:
        return deepcopy(self._comando_em_voo)

    @property
    def plano_compensacao(self) -> tuple[str, ...]:
        """Compensacoes restantes; a primeira e a pendente."""
        return tuple(self._plano_compensacao)

    @property
    def itens(self) -> tuple[ItemDoDiagnostico, ...]:
        return tuple(deepcopy(self._itens))

    @property
    def reenvios(self) -> int:
        return self._reenvios

    @property
    def prazo_resposta_em(self) -> datetime | None:
        """Prazo tecnico do comando em voo; ``None`` sem resposta automatica."""
        return self._prazo_resposta_em

    @property
    def iniciada_em(self) -> datetime:
        return self._iniciada_em

    @property
    def etapa_desde(self) -> datetime:
        return self._etapa_desde

    @property
    def atualizada_em(self) -> datetime:
        return self._atualizada_em

    @property
    def versao(self) -> int:
        return self._versao

    def classificar(self, tipo: str, ordem: OrdemDeServico) -> Classificacao:
        """Classifica o evento ``tipo`` ANTES de tocar no dominio (RFC-004 secao 4.5).

        Etapa ja passada e saga encerrada ou em compensacao: ignorado. Etapa a
        frente: adiantado. Na mesma etapa, o estado da OS desempata:
        ``DiagnosticoConcluido`` com a OS ainda ``recebida`` e
        ``PagamentoConfirmado``, ``Recusado`` ou ``Expirado`` sem o resumo do
        pagamento sao adiantados; ``DiagnosticoIniciado`` com a OS ja em
        diagnostico e ``PagamentoSolicitado`` com o resumo gravado, repetidos.
        """
        esperada = ETAPA_ESPERADA[tipo]
        if esperada is _E.COMPENSANDO:
            if self._etapa is _E.COMPENSANDO:
                return Classificacao.PROCESSAR
            return Classificacao.FORA_DA_COMPENSACAO
        if self._etapa not in FLUXO_DA_SAGA or self._etapa is _E.CONCLUIDA:
            return Classificacao.FORA_DO_FLUXO
        atual = FLUXO_DA_SAGA.index(self._etapa)
        alvo = FLUXO_DA_SAGA.index(esperada)
        if alvo < atual:
            return Classificacao.OBSOLETO
        if alvo > atual:
            return Classificacao.ADIANTADO
        return _na_mesma_etapa(tipo, ordem)

    def avancar(
        self,
        evento: MensagemRecebida,
        *,
        agora: datetime,
        ator: str | None,
        envio: Envio | None = None,
    ) -> None:
        """Aplica um evento do fluxo normal na etapa em que ele e esperado.

        Anota o passo, vai para a etapa seguinte da tabela da RFC-004 secao
        4.1, marca o passo concluido (T3, T5, T6, T7), guarda os itens do
        diagnostico e troca o comando em voo: o ``envio`` com prazo passa a
        esperar resposta, com ``reenvios`` zerado; sem ele, a saga deixa de
        esperar resposta automatica.

        Raises:
            TransicaoDaSagaInvalidaError: o evento nao e do fluxo normal ou a
                saga nao esta na etapa em que ele e esperado.
        """
        tipo = evento.tipo
        seguinte = ETAPA_SEGUINTE.get(tipo)
        if seguinte is None or ETAPA_ESPERADA[tipo] is not self._etapa:
            msg = f"{tipo} nao avanca a saga na etapa {self._etapa.value}"
            raise TransicaoDaSagaInvalidaError(msg)
        de = self._etapa
        if seguinte is not de:
            self._registrar_evento(
                EtapaDaSagaAlteradaEvent(
                    agregado_id=self.id,
                    etapa_anterior=de,
                    etapa_nova=seguinte,
                    permanencia=agora - self._etapa_desde,
                    ocorrido_em=agora,
                )
            )
            self._etapa = seguinte
            self._etapa_desde = agora
        self._atualizada_em = agora
        passo = self._passo(
            gatilho=tipo, de=de, mensagem_id=evento.id, ator=ator, envio=envio
        )
        if tipo == "ExecucaoAgendada":
            passo["posicao_na_fila"] = evento.dados["posicao_na_fila"]
        self._passos = [*self._passos, passo]
        if tipo == "DiagnosticoConcluido":
            self._itens = itens_do_diagnostico(evento.dados)
        if concluido := _PASSO_CONCLUIDO.get(tipo):
            self._passos_concluidos = [*self._passos_concluidos, concluido]
        self._esperar_resposta(envio)

    # ----- mecanica interna

    def _passo(
        self,
        *,
        gatilho: str,
        de: EtapaSaga | None,
        mensagem_id: UUID | None,
        ator: str | None,
        envio: Envio | None,
    ) -> Passo:
        """Passo novo no instante da alteracao (``atualizada_em``)."""
        return {
            "seq": len(self._passos) + 1,
            "em": self._atualizada_em.isoformat(),
            "de": de.value if de is not None else None,
            "para": self._etapa.value,
            "gatilho": gatilho,
            "mensagem_id": str(mensagem_id) if mensagem_id is not None else None,
            "comando": envio.tipo.value if envio is not None else None,
            "comando_id": str(envio.id) if envio is not None else None,
            "motivo": self._motivo,
            "ator": ator,
        }

    def _esperar_resposta(self, envio: Envio | None) -> None:
        """Comando com prazo vira o comando em voo; sem ele, nada fica em voo."""
        if envio is None or envio.prazo_resposta_em is None:
            self._comando_em_voo = None
            self._prazo_resposta_em = None
            return
        self._comando_em_voo = {
            "tipo": envio.tipo.value,
            "dados": dict(envio.dados),
            "mensagem_ids": [str(envio.id)],
            "enviado_em": self._atualizada_em.isoformat(),
        }
        self._prazo_resposta_em = envio.prazo_resposta_em
        self._reenvios = 0


def _na_mesma_etapa(tipo: str, ordem: OrdemDeServico) -> Classificacao:
    """Fora de ordem dentro da etapa, pelo estado da OS (RFC-004 secao 4.5)."""
    pagamento_solicitado = ordem.resumo_pagamento is not None
    match tipo:
        case "DiagnosticoIniciado" if ordem.status is not StatusOrdem.RECEBIDA:
            return Classificacao.REPETIDO
        case "DiagnosticoConcluido" if ordem.status is StatusOrdem.RECEBIDA:
            return Classificacao.ADIANTADO
        case "PagamentoSolicitado" if pagamento_solicitado:
            return Classificacao.REPETIDO
        case "PagamentoConfirmado" | "PagamentoRecusado" | "PagamentoExpirado" if (
            not pagamento_solicitado
        ):
            return Classificacao.ADIANTADO
    return Classificacao.PROCESSAR
