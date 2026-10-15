"""DTOs da camada de aplicacao do contexto Ordem de Servico.

Contratos imutaveis de entrada e saida dos casos de uso: tipos primitivos,
``Decimal`` para dinheiro (nunca float) e tuplas. Nenhum VO de dominio vaza.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from src.ordem_servico.aplicacao.saga.modelo import RegistroDaSaga


@dataclass(frozen=True, slots=True)
class AbrirOrdemDTO:
    """Comando de abertura da OS."""

    cliente_id: UUID
    veiculo_id: UUID
    # Texto livre: fora do repr (pode conter PII).
    descricao_problema: str = field(repr=False)
    # sub do JWT de quem abre (vai para o historico).
    ator: str


@dataclass(frozen=True, slots=True)
class RetratoDoVeiculo:
    """Veiculo como a Execucao o recebe no ``SolicitarDiagnostico`` (RFC-004 secao 5.3).

    A placa e dado pessoal: fora do repr.
    """

    placa: str = field(repr=False)
    marca: str
    modelo: str
    ano: int


@dataclass(frozen=True, slots=True)
class ContatoDoCliente:
    """Nome e contato do cliente, so para o e-mail da notificacao.

    O ``contato`` e o campo livre do cadastro (telefone, e-mail ou os dois):
    ``emails`` extrai o endereco. Dado pessoal: fora do repr.
    """

    nome: str = field(repr=False)
    contato: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MudancaDeStatusDTO:
    """Linha da linha do tempo de status (``de`` e ``None`` na abertura)."""

    sequencia: int
    de: str | None
    para: str
    origem: str
    motivo: str | None = field(repr=False)
    ator: str | None
    ocorrido_em: datetime


@dataclass(frozen=True, slots=True)
class ResumoOrcamentoDTO:
    """Resumo do orcamento do Billing guardado na OS."""

    orcamento_id: UUID
    total: Decimal
    moeda: str
    # Link assinado (credencial de quem o tiver): fora do repr.
    link_decisao: str = field(repr=False)
    valido_ate: datetime


@dataclass(frozen=True, slots=True)
class ResumoPagamentoDTO:
    """Resumo do pagamento do Billing guardado na OS."""

    pagamento_id: UUID
    status: str
    valor: Decimal
    moeda: str
    checkout_url: str = field(repr=False)
    expira_em: datetime


@dataclass(frozen=True, slots=True)
class OrdemDeServicoDTO:
    """Projecao completa da ordem (retorno dos casos de uso)."""

    id: UUID
    cliente_id: UUID
    veiculo_id: UUID
    descricao_problema: str = field(repr=False)
    status: str
    orcamento: ResumoOrcamentoDTO | None
    pagamento: ResumoPagamentoDTO | None
    motivo_cancelamento: str | None = field(repr=False)
    versao: int
    criado_em: datetime
    atualizado_em: datetime
    historico: tuple[MudancaDeStatusDTO, ...]
    # Etapa da saga (``None`` so para OS sem saga) e os passos dela.
    etapa: str | None
    passos: tuple[RegistroDaSaga, ...]


@dataclass(frozen=True, slots=True)
class OrdemResumoDTO:
    """Projecao enxuta para a listagem paginada."""

    id: UUID
    cliente_id: UUID
    veiculo_id: UUID
    status: str
    criado_em: datetime
    atualizado_em: datetime


@dataclass(frozen=True, slots=True)
class AcompanhamentoDTO:
    """Projecao publica: so status e timestamps (sem dado do cliente)."""

    status: str
    criado_em: datetime
    atualizado_em: datetime


@dataclass(frozen=True, slots=True)
class MetricasDasOrdensDTO:
    """Total, OS por status e permanencia media por status (RF-027 da fase 3).

    Os mesmos numeros do ``GET /api/v1/ordens-de-servico/metricas`` e dos gauges
    ``pytstop_os_por_status`` e ``pytstop_os_tempo_medio_status_segundos``.
    ``por_status`` leva os nove status (zero onde nao ha OS);
    ``tempo_medio_por_status_segundos``, so os status com ao menos uma
    permanencia concluida na janela (entrada ate a transicao seguinte da mesma OS).
    """

    total: int
    por_status: Mapping[str, int]
    tempo_medio_por_status_segundos: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ComandoEmVooDTO:
    """Comando com prazo tecnico a espera de resposta: tipo e hora do envio."""

    tipo: str
    enviado_em: datetime


@dataclass(frozen=True, slots=True)
class SagaDTO:
    """Estado da saga para a operacao (RFC-004 secoes 4.7 e 6.1): so codigos."""

    ordem_id: UUID
    etapa: str
    motivo: str | None
    falha: str | None
    plano_compensacao: tuple[str, ...]
    comando_em_voo: ComandoEmVooDTO | None
    reenvios: int
    prazo_resposta_em: datetime | None
    passos: tuple[RegistroDaSaga, ...]
