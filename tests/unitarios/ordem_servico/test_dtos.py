"""Texto livre e links de capacidade ficam fora do ``repr`` dos DTOs.

O ``repr`` vai para log e traceback: descricao e motivos podem ter PII, e o
link de decisao e o checkout sao credenciais de quem os tiver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from src.ordem_servico.aplicacao.dtos import (
    AbrirOrdemDTO,
    MudancaDeStatusDTO,
    OrdemDeServicoDTO,
    ResumoOrcamentoDTO,
    ResumoPagamentoDTO,
)

_AGORA = datetime(2026, 10, 6, 12, tzinfo=UTC)
_SEGREDO = "Joao 11999990000"
_LINK = "https://billing.pytstop.local/publico/orcamentos/tok-secreto"


def test_abrir_ordem_nao_expoe_a_descricao() -> None:
    dto = AbrirOrdemDTO(
        cliente_id=uuid4(),
        veiculo_id=uuid4(),
        descricao_problema=_SEGREDO,
        ator=str(uuid4()),
    )
    assert "Joao" not in repr(dto)


def test_mudanca_de_status_nao_expoe_o_motivo() -> None:
    dto = MudancaDeStatusDTO(
        sequencia=2,
        de="recebida",
        para="cancelada",
        origem="atendimento",
        motivo=_SEGREDO,
        ator=str(uuid4()),
        ocorrido_em=_AGORA,
    )
    assert "Joao" not in repr(dto)


def test_ordem_nao_expoe_texto_livre_nem_links() -> None:
    dto = OrdemDeServicoDTO(
        id=uuid4(),
        cliente_id=uuid4(),
        veiculo_id=uuid4(),
        descricao_problema=_SEGREDO,
        status="cancelada",
        orcamento=ResumoOrcamentoDTO(
            orcamento_id=uuid4(),
            total=Decimal("350.00"),
            moeda="BRL",
            link_decisao=_LINK,
            valido_ate=_AGORA,
        ),
        pagamento=ResumoPagamentoDTO(
            pagamento_id=uuid4(),
            status="solicitado",
            valor=Decimal("350.00"),
            moeda="BRL",
            checkout_url=_LINK,
            expira_em=_AGORA,
        ),
        motivo_cancelamento=_SEGREDO,
        versao=3,
        criado_em=_AGORA,
        atualizado_em=_AGORA,
        historico=(),
        etapa="compensando",
        passos=(),
    )
    texto = repr(dto)
    assert "Joao" not in texto
    assert "tok-secreto" not in texto
