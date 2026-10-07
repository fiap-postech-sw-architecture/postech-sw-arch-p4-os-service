"""Aggregate root ``OrdemDeServico`` da fase 4.

A OS deixa de ter itens e de calcular orcamento (agora do Billing e da
Execucao): guarda o problema relatado, o status com o historico das
mudancas e o resumo do orcamento e do pagamento que chegam pelos fatos da
saga (RFC-004 secoes 4.2, 4.4 e 6.1).
"""

from __future__ import annotations

import unicodedata
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
from src.ordem_servico.dominio.maquina_de_status import MaquinaDeStatus
from src.ordem_servico.dominio.resumos import (
    ResumoOrcamento,
    ResumoPagamento,
    pagamento_confirmado,
    pagamento_solicitado,
)
from src.ordem_servico.dominio.status import StatusOrdem

if TYPE_CHECKING:
    from uuid import UUID

# Limites dos textos livres (espelhados nas colunas e nos schemas HTTP).
TAMANHO_MAXIMO_DESCRICAO: Final = 1000
TAMANHO_MAXIMO_MOTIVO: Final = 500

# Stateless (so le a tabela de transicoes): uma instancia para todos.
_maquina: Final = MaquinaDeStatus()


def _texto_obrigatorio(valor: str, rotulo: str, maximo: int) -> str:
    """Texto livre aparado, nao vazio, dentro do limite e sem controle.

    CRLF vira LF (quebra de linha de formulario); qualquer outro caractere de
    controle (NUL inclusive, que o Postgres recusa no flush) e rejeitado: so
    quebra de linha e tabulacao passam. Violacao levanta
    ``ValorInvalidoException`` (422).
    """
    texto = (valor or "").replace("\r\n", "\n").strip()
    if not texto:
        msg = f"{rotulo} e obrigatorio"
        raise ValorInvalidoException(msg)
    if len(texto) > maximo:
        msg = f"{rotulo} excede {maximo} caracteres"
        raise ValorInvalidoException(msg)
    if any(c not in "\n\t" and unicodedata.category(c) == "Cc" for c in texto):
        msg = f"{rotulo} tem caractere de controle"
        raise ValorInvalidoException(msg)
    return texto


@dataclass(eq=False)
class OrdemDeServico(AggregateRoot):
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
    _status: StatusOrdem = field(default=StatusOrdem.RECEBIDA, kw_only=True)
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
        self._descricao_problema = _texto_obrigatorio(
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
            de=None,
            para=StatusOrdem.RECEBIDA,
            origem=OrigemMudanca.ATENDIMENTO,
            motivo=None,
            ator=ator,
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

    @property
    def cliente_id(self) -> UUID:
        """Cliente dono do veiculo atendido."""
        return self._cliente_id

    @property
    def veiculo_id(self) -> UUID:
        """Veiculo recebido para o servico."""
        return self._veiculo_id

    @property
    def descricao_problema(self) -> str:
        """Problema relatado no atendimento: texto livre ja aparado e validado."""
        return self._descricao_problema

    @property
    def status(self) -> StatusOrdem:
        """Status atual; so muda pelos fatos de dominio, que validam a maquina."""
        return self._status

    @property
    def historico(self) -> tuple[MudancaDeStatus, ...]:
        """Linha do tempo das mudancas de status, da abertura ate agora."""
        return tuple(self._historico)

    @property
    def resumo_orcamento(self) -> ResumoOrcamento | None:
        """Resumo do orcamento do Billing; ``None`` ate o ``OrcamentoGerado``."""
        return self._resumo_orcamento

    @property
    def resumo_pagamento(self) -> ResumoPagamento | None:
        """Resumo do pagamento do Billing; ``None`` ate o ``PagamentoSolicitado``."""
        return self._resumo_pagamento

    @property
    def motivo_cancelamento(self) -> str | None:
        """Motivo informado no cancelamento; ``None`` se a ordem nao foi cancelada."""
        return self._motivo_cancelamento

    @property
    def versao(self) -> int:
        """Versao do lock otimista: comeca em 1 e sobe a cada gravacao."""
        return self._versao

    @property
    def criado_em(self) -> datetime:
        """Instante da abertura da ordem (UTC)."""
        return self._criado_em

    @property
    def atualizado_em(self) -> datetime:
        """Instante da ultima alteracao do agregado (UTC)."""
        return self._atualizado_em

    # ----- fatos da saga (cada transicao valida, anota no historico e emite
    # evento; o status segue a tabela de etapas da RFC-004 secao 4.1)

    def registrar_diagnostico_iniciado(self, *, ator: str) -> None:
        """``DiagnosticoIniciado`` (Execucao): RECEBIDA -> EM_DIAGNOSTICO."""
        self._transicionar(
            StatusOrdem.EM_DIAGNOSTICO, origem=OrigemMudanca.EXECUCAO, ator=ator
        )

    def registrar_orcamento_gerado(self, resumo: ResumoOrcamento, *, ator: str) -> None:
        """``OrcamentoGerado`` (Billing): EM_DIAGNOSTICO -> AGUARDANDO_APROVACAO.

        Guarda o resumo do orcamento, de onde sai o link de decisao do cliente.
        """
        self._transicionar(
            StatusOrdem.AGUARDANDO_APROVACAO, origem=OrigemMudanca.BILLING, ator=ator
        )
        self._resumo_orcamento = resumo

    def registrar_pecas_reservadas(self, *, ator: str) -> None:
        """``PecasReservadas`` (Execucao): AGUARDANDO_APROVACAO -> AGUARDANDO_PAGAMENTO.

        O pagamento e pedido em seguida, sobre o orcamento gerado: sem o resumo
        dele, levanta ``ViolacaoRegraDeNegocioException`` antes de mudar.
        """
        self._validar_transicao(StatusOrdem.AGUARDANDO_PAGAMENTO)
        if self._resumo_orcamento is None:
            msg = f"Ordem {self.id} sem orcamento gerado"
            raise ViolacaoRegraDeNegocioException(msg)
        self._aplicar_transicao(
            StatusOrdem.AGUARDANDO_PAGAMENTO, origem=OrigemMudanca.EXECUCAO, ator=ator
        )

    def registrar_pagamento_solicitado(self, resumo: ResumoPagamento) -> None:
        """``PagamentoSolicitado`` (Billing): grava o resumo, sem mudar o status.

        So com a OS aguardando pagamento (``TransicaoStatusInvalidaException``);
        o ciclo do resumo e o de ``resumos.pagamento_solicitado``.
        """
        if self._status is not StatusOrdem.AGUARDANDO_PAGAMENTO:
            raise TransicaoStatusInvalidaException(
                mensagem="Pagamento so e solicitado com a ordem aguardando "
                f"pagamento; status atual: {self._status.value}"
            )
        self._resumo_pagamento = pagamento_solicitado(self._resumo_pagamento, resumo)
        self._atualizado_em = datetime.now(UTC)

    def registrar_pagamento_confirmado(self, *, ator: str) -> None:
        """``PagamentoConfirmado`` (Billing): o resumo passa a confirmado.

        AGUARDANDO_PAGAMENTO -> AGUARDANDO_EXECUCAO: o agendamento que vem depois
        e tecnico e nao falha por regra de negocio (RFC-004 secao 4.1).
        """
        self._validar_transicao(StatusOrdem.AGUARDANDO_EXECUCAO)
        confirmado = pagamento_confirmado(self._resumo_pagamento)
        self._aplicar_transicao(
            StatusOrdem.AGUARDANDO_EXECUCAO, origem=OrigemMudanca.BILLING, ator=ator
        )
        self._resumo_pagamento = confirmado

    def registrar_execucao_iniciada(self, *, ator: str) -> None:
        """``ExecucaoIniciada`` (Execucao), o pivot da saga.

        AGUARDANDO_EXECUCAO -> EM_EXECUCAO; depois dele nao ha cancelamento.
        """
        self._transicionar(
            StatusOrdem.EM_EXECUCAO, origem=OrigemMudanca.EXECUCAO, ator=ator
        )

    def finalizar(self, *, ator: str) -> None:
        """``ExecucaoFinalizada`` (Execucao): EM_EXECUCAO -> FINALIZADA."""
        self._transicionar(
            StatusOrdem.FINALIZADA, origem=OrigemMudanca.EXECUCAO, ator=ator
        )

    def registrar_entrega(self, *, ator: str) -> None:
        """Entrega ao cliente (atendimento, fora da saga): FINALIZADA -> ENTREGUE."""
        self._transicionar(
            StatusOrdem.ENTREGUE, origem=OrigemMudanca.ATENDIMENTO, ator=ator
        )

    def cancelar(self, motivo: str, origem: OrigemMudanca, *, ator: str) -> None:
        """Cancela a OS antes do inicio da execucao.

        A maquina e validada primeiro: depois de EM_EXECUCAO (pivot) ou em
        estado terminal, o erro e ``TransicaoStatusInvalidaException`` (409),
        nao o motivo. Motivo vazio, acima do limite ou com caractere de
        controle levanta ``ValorInvalidoException`` (422).
        """
        self._validar_transicao(StatusOrdem.CANCELADA)
        motivo_normalizado = _texto_obrigatorio(
            motivo, "motivo de cancelamento", TAMANHO_MAXIMO_MOTIVO
        )
        self._aplicar_transicao(
            StatusOrdem.CANCELADA, origem=origem, motivo=motivo_normalizado, ator=ator
        )
        self._motivo_cancelamento = motivo_normalizado

    # ----- mecanica interna

    def _validar_transicao(self, para: StatusOrdem) -> None:
        """Valida SEM mutar: estado invalido falha antes de qualquer trabalho."""
        _maquina.validar_transicao(self._status, para)

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
        self._validar_transicao(para)
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
