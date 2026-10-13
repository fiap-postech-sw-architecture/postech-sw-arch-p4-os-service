"""Aggregate root ``OrdemDeServico``: o problema relatado, o status com o historico
e o resumo do orcamento e do pagamento que chegam pelos fatos da saga (RFC-004
secoes 4.2, 4.4 e 6.1). O orcamento e o pagamento sao do Billing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaException,
    ValorInvalidoException,
    ViolacaoRegraDeNegocioException,
)
from src.ordem_servico.dominio.events import (
    OrdemAbertaEvent,
    StatusDaOrdemAlteradoEvent,
)
from src.ordem_servico.dominio.historico import MudancaDeStatus, OrigemMudanca
from src.ordem_servico.dominio.leitura_da_ordem import LeituraDaOrdem
from src.ordem_servico.dominio.maquina_de_status import MaquinaDeStatus
from src.ordem_servico.dominio.resumos import (
    ResumoOrcamento,
    ResumoPagamento,
    StatusPagamento,
    pagamento_confirmado,
    pagamento_no_estado,
    pagamento_solicitado,
)
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.dominio.texto_livre import (
    TAMANHO_MAXIMO_DESCRICAO,
    TAMANHO_MAXIMO_MOTIVO,
    texto_obrigatorio,
)

if TYPE_CHECKING:
    from uuid import UUID

# Stateless (so le a tabela de transicoes): uma instancia para todos.
_maquina: Final = MaquinaDeStatus()
_S = StatusOrdem
_O = OrigemMudanca


@dataclass(eq=False)
class OrdemDeServico(AggregateRoot, LeituraDaOrdem):
    """Aggregate root do contexto Ordem de Servico.

    Construir via ``OrdemDeServico.abrir``. Cada transicao valida a
    ``MaquinaDeStatus`` antes de mutar, anota a ``MudancaDeStatus`` no historico
    e registra o ``StatusDaOrdemAlteradoEvent``. A ``versao`` e o lock otimista
    da persistencia (escrita concorrente vira ``ConflitoDeConcorrenciaException``).
    """

    _cliente_id: UUID = field(kw_only=True, repr=False)
    _veiculo_id: UUID = field(kw_only=True, repr=False)
    # Texto livre do atendimento: fora do repr, pode conter PII.
    _descricao_problema: str = field(kw_only=True, repr=False)
    _status: StatusOrdem = field(default=_S.RECEBIDA, kw_only=True)
    _historico: list[MudancaDeStatus] = field(
        default_factory=list, kw_only=True, repr=False
    )
    _resumo_orcamento: ResumoOrcamento | None = field(
        default=None, kw_only=True, repr=False
    )
    _resumo_pagamento: ResumoPagamento | None = field(
        default=None, kw_only=True, repr=False
    )
    _motivo_cancelamento: str | None = field(default=None, kw_only=True, repr=False)
    _versao: int = field(default=1, kw_only=True)
    _criado_em: datetime = field(
        default_factory=lambda: datetime.now(UTC), kw_only=True, repr=False
    )
    _atualizado_em: datetime = field(
        default_factory=lambda: datetime.now(UTC), kw_only=True, repr=False
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        # Defende o None explicito (kw_only sem default ja barra a omissao).
        if self._cliente_id is None:
            msg = "cliente_id e obrigatorio"
            raise ValorInvalidoException(msg)
        if self._veiculo_id is None:
            msg = "veiculo_id e obrigatorio"
            raise ValorInvalidoException(msg)
        self._descricao_problema = texto_obrigatorio(
            self._descricao_problema,
            "descricao do problema",
            TAMANHO_MAXIMO_DESCRICAO,
        )

    @classmethod
    def abrir(
        cls,
        *,
        cliente_id: UUID,
        veiculo_id: UUID,
        descricao_problema: str,
        ator: str,
    ) -> OrdemDeServico:
        """Abre a OS em ``RECEBIDA``: primeira linha do historico + evento.

        ``ator`` (aqui e em cada transicao) e quem a provoca: o ``sub`` do JWT
        ou o processo, gravado no historico (RFC-004 secao 7.2).
        """
        agora = datetime.now(UTC)
        ordem = cls(
            _cliente_id=cliente_id,
            _veiculo_id=veiculo_id,
            _descricao_problema=descricao_problema,
            _criado_em=agora,
            _atualizado_em=agora,
        )
        ordem._anotar(
            de=None, para=_S.RECEBIDA, origem=_O.ATENDIMENTO, motivo=None, ator=ator
        )
        ordem._registrar_evento(
            OrdemAbertaEvent(
                agregado_id=ordem.id,
                cliente_id=cliente_id,
                veiculo_id=veiculo_id,
                ocorrido_em=agora,
            )
        )
        return ordem

    # ----- fatos da saga (cada transicao valida a maquina, anota o historico
    # e emite o evento; o status segue a tabela da RFC-004 secao 4.1)

    def registrar_diagnostico_iniciado(self, *, ator: str) -> None:
        """``DiagnosticoIniciado`` (Execucao): RECEBIDA -> EM_DIAGNOSTICO."""
        self._transicionar(_S.EM_DIAGNOSTICO, origem=_O.EXECUCAO, ator=ator)

    def registrar_orcamento_gerado(self, resumo: ResumoOrcamento, *, ator: str) -> None:
        """``OrcamentoGerado`` (Billing): EM_DIAGNOSTICO -> AGUARDANDO_APROVACAO.

        Guarda o resumo do orcamento, de onde sai o link de decisao do cliente.
        """
        self._transicionar(_S.AGUARDANDO_APROVACAO, origem=_O.BILLING, ator=ator)
        self._resumo_orcamento = resumo

    def registrar_pecas_reservadas(self, *, ator: str) -> None:
        """``PecasReservadas`` (Execucao): AGUARDANDO_APROVACAO -> AGUARDANDO_PAGAMENTO.

        Sem o resumo do orcamento (o pagamento e pedido sobre ele), levanta
        ``ViolacaoRegraDeNegocioException`` antes de mudar.
        """
        _maquina.validar_transicao(self._status, _S.AGUARDANDO_PAGAMENTO)
        if self._resumo_orcamento is None:
            msg = f"Ordem {self.id} sem orcamento gerado"
            raise ViolacaoRegraDeNegocioException(msg)
        self._aplicar_transicao(_S.AGUARDANDO_PAGAMENTO, origem=_O.EXECUCAO, ator=ator)

    def registrar_estado_do_pagamento(
        self,
        status: StatusPagamento,
        *,
        ator: str,
        resumo: ResumoPagamento | None = None,
    ) -> None:
        """O estado do pagamento que o Billing informou (RFC-004 secoes 4.1 e 4.4).

        ``solicitado`` grava o ``resumo`` do checkout (so com a OS aguardando
        pagamento) e nao muda o status; ``confirmado`` leva AGUARDANDO_PAGAMENTO
        -> AGUARDANDO_EXECUCAO, e o ``ator`` entra no historico; recusado,
        expirado, cancelado e estornado so trocam o resumo, com a OS em qualquer
        status (a tabela de origens e a de ``resumos``).

        Raises:
            TransicaoStatusInvalidaException: o fato nao cabe no status da OS.
            ViolacaoRegraDeNegocioException: ``resumo`` faltando ou sobrando, ou
                pagamento que o estado nao admite.
        """
        if (resumo is None) is (status is StatusPagamento.SOLICITADO):
            msg = "O resumo acompanha o pagamento solicitado, e so ele"
            raise ViolacaoRegraDeNegocioException(msg)
        if resumo is not None:
            self._solicitar_pagamento(resumo)
        elif status is StatusPagamento.CONFIRMADO:
            self._confirmar_pagamento(ator)
        else:
            self._resumo_pagamento = pagamento_no_estado(self._resumo_pagamento, status)
            self._atualizado_em = datetime.now(UTC)

    def registrar_execucao_iniciada(self, *, ator: str) -> None:
        """``ExecucaoIniciada`` (Execucao), o pivot da saga.

        AGUARDANDO_EXECUCAO -> EM_EXECUCAO; depois dele nao ha cancelamento, e o
        pedido que perdeu a corrida para o inicio e descartado (RFC-004 secao 4.5).
        """
        self._transicionar(_S.EM_EXECUCAO, origem=_O.EXECUCAO, ator=ator)
        self._motivo_cancelamento = None

    def finalizar(self, *, ator: str) -> None:
        """``ExecucaoFinalizada`` (Execucao): EM_EXECUCAO -> FINALIZADA."""
        self._transicionar(_S.FINALIZADA, origem=_O.EXECUCAO, ator=ator)

    def registrar_entrega(self, *, ator: str) -> None:
        """Entrega ao cliente (atendimento, fora da saga): FINALIZADA -> ENTREGUE."""
        self._transicionar(_S.ENTREGUE, origem=_O.ATENDIMENTO, ator=ator)

    def registrar_pedido_de_cancelamento(self, motivo: str) -> None:
        """Guarda o texto do pedido de cancelamento, sem mudar o status.

        A saga desfaz os passos e so entao cancela a OS com ele (RFC-004 4.4);
        ate la o texto fica aqui, ao alcance da eliminacao de dados da LGPD.
        """
        _maquina.validar_transicao(self._status, _S.CANCELADA)
        self._motivo_cancelamento = texto_obrigatorio(
            motivo, "motivo de cancelamento", TAMANHO_MAXIMO_MOTIVO
        )
        self._atualizado_em = datetime.now(UTC)

    def cancelar(self, motivo: str, origem: OrigemMudanca, *, ator: str) -> None:
        """Cancela a OS antes do pivot, com o ``motivo`` validado como o do pedido."""
        self.registrar_pedido_de_cancelamento(motivo)
        self._aplicar_transicao(
            _S.CANCELADA, origem=origem, motivo=self._motivo_cancelamento, ator=ator
        )

    # ----- mecanica interna

    def _solicitar_pagamento(self, resumo: ResumoPagamento) -> None:
        if self._status is not _S.AGUARDANDO_PAGAMENTO:
            raise TransicaoStatusInvalidaException(
                mensagem="Pagamento so e solicitado com a ordem aguardando "
                f"pagamento; status atual: {self._status.value}"
            )
        self._resumo_pagamento = pagamento_solicitado(self._resumo_pagamento, resumo)
        self._atualizado_em = datetime.now(UTC)

    def _confirmar_pagamento(self, ator: str) -> None:
        _maquina.validar_transicao(self._status, _S.AGUARDANDO_EXECUCAO)
        confirmado = pagamento_confirmado(self._resumo_pagamento)
        self._aplicar_transicao(_S.AGUARDANDO_EXECUCAO, origem=_O.BILLING, ator=ator)
        self._resumo_pagamento = confirmado

    def _aplicar_transicao(
        self,
        para: StatusOrdem,
        *,
        origem: OrigemMudanca,
        ator: str,
        motivo: str | None = None,
    ) -> None:
        """Aplica a transicao ja validada: status, historico e evento."""
        de = self._status
        agora = datetime.now(UTC)
        self._status = para
        self._atualizado_em = agora
        self._anotar(de=de, para=para, origem=origem, motivo=motivo, ator=ator)
        self._registrar_evento(
            StatusDaOrdemAlteradoEvent(
                agregado_id=self.id,
                status_anterior=de,
                status_novo=para,
                origem=origem,
                ocorrido_em=agora,
            )
        )

    def _transicionar(
        self, para: StatusOrdem, *, origem: OrigemMudanca, ator: str
    ) -> None:
        _maquina.validar_transicao(self._status, para)
        self._aplicar_transicao(para, origem=origem, ator=ator)

    def _anotar(
        self,
        *,
        de: StatusOrdem | None,
        para: StatusOrdem,
        origem: OrigemMudanca,
        motivo: str | None,
        ator: str,
    ) -> None:
        """Linha nova do historico, no instante da alteracao (``atualizado_em``)."""
        self._historico.append(
            MudancaDeStatus(
                _sequencia=len(self._historico) + 1,
                _de=de,
                _para=para,
                _origem=origem,
                _motivo=motivo,
                _ator=ator,
                _ocorrido_em=self._atualizado_em,
            )
        )
