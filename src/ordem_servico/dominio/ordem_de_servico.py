"""Aggregate root ``OrdemDeServico`` da fase 4.

A OS deixa de ter itens e de calcular orcamento (agora do Billing e da
Execucao): guarda o problema relatado, o status com o historico das
mudancas e o resumo do orcamento e do pagamento que chegam pelos fatos da
saga (brief secoes 2, 3 e 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.ordem_servico.dominio.events import (
    OrdemAbertaEvent,
    StatusDaOrdemAlteradoEvent,
)
from src.ordem_servico.dominio.historico import MudancaDeStatus, OrigemMudanca
from src.ordem_servico.dominio.maquina_de_status import MaquinaDeStatus
from src.ordem_servico.dominio.resumos import (
    ResumoOrcamento,
    ResumoPagamento,
    StatusPagamento,
)
from src.ordem_servico.dominio.status import StatusOrdem

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro

# Limites dos textos livres (espelhados nas colunas e nos schemas HTTP).
TAMANHO_MAXIMO_DESCRICAO: Final = 1000
TAMANHO_MAXIMO_MOTIVO: Final = 500

# Stateless (so le a tabela de transicoes): uma instancia para todos.
_maquina: Final = MaquinaDeStatus()


def _texto_obrigatorio(valor: str, rotulo: str, maximo: int) -> str:
    texto = (valor or "").strip()
    if not texto:
        msg = f"{rotulo} e obrigatorio"
        raise ValueError(msg)
    if len(texto) > maximo:
        msg = f"{rotulo} excede {maximo} caracteres"
        raise ValueError(msg)
    return texto


@dataclass(eq=False)
class OrdemDeServico(AggregateRoot):
    """Aggregate root do contexto Ordem de Servico.

    Construir via ``OrdemDeServico.abrir``. Cada metodo de transicao valida a
    ``MaquinaDeStatus`` antes de mutar, registra a ``MudancaDeStatus`` no
    historico e emite ``StatusDaOrdemAlteradoEvent`` para a outbox. A
    ``versao`` e controlada pela persistencia (lock otimista): escrita
    concorrente sobre a mesma versao vira ``ConflitoDeConcorrenciaException``.
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
            raise ValueError(msg)
        if self._veiculo_id is None:
            msg = "veiculo_id e obrigatorio"
            raise ValueError(msg)
        self._descricao_problema = _texto_obrigatorio(
            self._descricao_problema,
            "descricao do problema",
            TAMANHO_MAXIMO_DESCRICAO,
        )

    @classmethod
    def abrir(
        cls, *, cliente_id: UUID, veiculo_id: UUID, descricao_problema: str
    ) -> OrdemDeServico:
        """Abre a OS em ``RECEBIDA``: primeira linha do historico + evento."""
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
            ocorrido_em=agora,
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
        return self._cliente_id

    @property
    def veiculo_id(self) -> UUID:
        return self._veiculo_id

    @property
    def descricao_problema(self) -> str:
        return self._descricao_problema

    @property
    def status(self) -> StatusOrdem:
        return self._status

    @property
    def historico(self) -> tuple[MudancaDeStatus, ...]:
        """Linha do tempo das mudancas de status, da abertura ate agora."""
        return tuple(self._historico)

    @property
    def resumo_orcamento(self) -> ResumoOrcamento | None:
        return self._resumo_orcamento

    @property
    def resumo_pagamento(self) -> ResumoPagamento | None:
        return self._resumo_pagamento

    @property
    def motivo_cancelamento(self) -> str | None:
        return self._motivo_cancelamento

    @property
    def versao(self) -> int:
        return self._versao

    @property
    def criado_em(self) -> datetime:
        return self._criado_em

    @property
    def atualizado_em(self) -> datetime:
        return self._atualizado_em

    # ----- fatos da saga (cada um valida, anota no historico e emite evento)

    def registrar_diagnostico_iniciado(self) -> None:
        """``DiagnosticoIniciado`` (Execucao): RECEBIDA -> EM_DIAGNOSTICO."""
        self._transicionar(StatusOrdem.EM_DIAGNOSTICO, origem=OrigemMudanca.EXECUCAO)

    def registrar_orcamento_gerado(
        self, *, orcamento_id: UUID, total: Dinheiro, link_decisao: str
    ) -> None:
        """``OrcamentoGerado`` (Billing).

        EM_DIAGNOSTICO -> AGUARDANDO_APROVACAO, com o resumo do orcamento.
        """
        self._validar_transicao(StatusOrdem.AGUARDANDO_APROVACAO)
        resumo = ResumoOrcamento(
            orcamento_id=orcamento_id, total=total, link_decisao=link_decisao
        )
        self._aplicar_transicao(
            StatusOrdem.AGUARDANDO_APROVACAO, origem=OrigemMudanca.BILLING
        )
        self._resumo_orcamento = resumo

    def registrar_pagamento_solicitado(
        self, *, pagamento_id: UUID, checkout_url: str
    ) -> None:
        """``PagamentoSolicitado`` (Billing).

        AGUARDANDO_APROVACAO -> AGUARDANDO_PAGAMENTO, com o resumo do pagamento.
        """
        self._validar_transicao(StatusOrdem.AGUARDANDO_PAGAMENTO)
        resumo = ResumoPagamento(
            pagamento_id=pagamento_id,
            status=StatusPagamento.SOLICITADO,
            checkout_url=checkout_url,
        )
        self._aplicar_transicao(
            StatusOrdem.AGUARDANDO_PAGAMENTO, origem=OrigemMudanca.BILLING
        )
        self._resumo_pagamento = resumo

    def registrar_aguardando_execucao(self) -> None:
        """``ExecucaoAgendada`` (Execucao).

        AGUARDANDO_PAGAMENTO -> AGUARDANDO_EXECUCAO.
        """
        self._transicionar(
            StatusOrdem.AGUARDANDO_EXECUCAO, origem=OrigemMudanca.EXECUCAO
        )

    def registrar_execucao_iniciada(self) -> None:
        """``ExecucaoIniciada`` (Execucao).

        AGUARDANDO_EXECUCAO -> EM_EXECUCAO: pivot da saga, sem cancelamento depois.
        """
        self._transicionar(StatusOrdem.EM_EXECUCAO, origem=OrigemMudanca.EXECUCAO)

    def finalizar(self) -> None:
        """``ExecucaoFinalizada`` (Execucao): EM_EXECUCAO -> FINALIZADA."""
        self._transicionar(StatusOrdem.FINALIZADA, origem=OrigemMudanca.EXECUCAO)

    def registrar_entrega(self) -> None:
        """Entrega ao cliente (atendimento, fora da saga): FINALIZADA -> ENTREGUE."""
        self._transicionar(StatusOrdem.ENTREGUE, origem=OrigemMudanca.ATENDIMENTO)

    def cancelar(self, motivo: str, origem: OrigemMudanca) -> None:
        """Cancela a OS antes do inicio da execucao.

        A maquina e validada primeiro: depois de EM_EXECUCAO (pivot) ou em
        estado terminal, o erro e ``TransicaoStatusInvalidaException`` (409),
        nao o motivo. Motivo vazio ou acima do limite levanta ``ValueError``.
        """
        self._validar_transicao(StatusOrdem.CANCELADA)
        motivo_normalizado = _texto_obrigatorio(
            motivo, "motivo de cancelamento", TAMANHO_MAXIMO_MOTIVO
        )
        self._aplicar_transicao(
            StatusOrdem.CANCELADA, origem=origem, motivo=motivo_normalizado
        )
        self._motivo_cancelamento = motivo_normalizado

    # ----- mecanica interna

    def _validar_transicao(self, para: StatusOrdem) -> None:
        """Valida SEM mutar: estado invalido falha antes de qualquer trabalho."""
        _maquina.validar_transicao(self._status, para)

    def _aplicar_transicao(
        self, para: StatusOrdem, *, origem: OrigemMudanca, motivo: str | None = None
    ) -> None:
        """Aplica a transicao ja validada: status, historico e evento."""
        de = self._status
        agora = datetime.now(UTC)
        self._status = para
        self._atualizado_em = agora
        self._anotar(de=de, para=para, origem=origem, motivo=motivo, ocorrido_em=agora)
        self._registrar_evento(
            StatusDaOrdemAlteradoEvent(
                agregado_id=self.id,
                status_anterior=de,
                status_novo=para,
                origem=origem,
                ocorrido_em=agora,
            )
        )

    def _transicionar(self, para: StatusOrdem, *, origem: OrigemMudanca) -> None:
        self._validar_transicao(para)
        self._aplicar_transicao(para, origem=origem)

    def _anotar(
        self,
        *,
        de: StatusOrdem | None,
        para: StatusOrdem,
        origem: OrigemMudanca,
        motivo: str | None,
        ocorrido_em: datetime,
    ) -> None:
        self._historico.append(
            MudancaDeStatus(
                _sequencia=len(self._historico) + 1,
                _de=de,
                _para=para,
                _origem=origem,
                _motivo=motivo,
                _ocorrido_em=ocorrido_em,
            )
        )
