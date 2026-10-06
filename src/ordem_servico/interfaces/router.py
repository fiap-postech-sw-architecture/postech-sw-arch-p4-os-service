"""Routers HTTP do contexto Ordem de Servico (brief secao 6, parte OS).

``router`` (JWT + papel): abertura, fila por prioridade, consulta, historico,
cancelamento e entrega. ``router_publico`` (sem token, rate limit por IP):
acompanhamento por placa + documento. As transicoes da saga (diagnostico,
orcamento, pagamento, execucao) chegam por mensageria, nao por rota HTTP.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID  # noqa: TC003

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

# Imports de runtime (nao TYPE_CHECKING): o FastAPI avalia as annotations
# `Annotated[Session, Depends(...)]` e `Request` em runtime.
from sqlalchemy.orm import Session
from starlette.requests import Request  # noqa: TC002

from src.autenticacao.dominio.papel import Papel
from src.autenticacao.interfaces.middleware import exigir_papel
from src.compartilhado.interfaces.auditoria import ator_de
from src.compartilhado.interfaces.dependencies import obter_session
from src.compartilhado.interfaces.middleware import limiter
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_cancelar_ordem,
    obter_consultar_acompanhamento,
    obter_listar_ordens,
    obter_obter_ordem,
    obter_registrar_entrega,
)
from src.ordem_servico.interfaces.schemas import (
    AbrirOrdemRequest,
    AcompanhamentoRequest,
    AcompanhamentoResponse,
    CancelarOrdemRequest,
    HistoricoResponse,
    MudancaDeStatusResponse,
    OrdemDeServicoResponse,
    OrdemListaResponse,
    OrdemResumoResponse,
)

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/ordens-de-servico", tags=["ordens-de-servico"])
router_publico = APIRouter(prefix="/api/v1/publico", tags=["publico"])

_Atendente = Annotated[dict[str, object], Depends(exigir_papel(Papel.ATENDENTE))]
_Leitor = Annotated[
    dict[str, object],
    Depends(exigir_papel(Papel.ATENDENTE, Papel.MECANICO)),
]
_Sessao = Annotated[Session, Depends(obter_session)]

_RESPOSTA_409: dict[int | str, dict[str, Any]] = {
    409: {
        "description": (
            "Transicao invalida para o status atual ou escrita concorrente "
            "(versao divergente)."
        )
    }
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Abre uma ordem de servico em RECEBIDA",
    responses={404: {"description": "Cliente inativo/inexistente ou veiculo alheio."}},
)
def abrir_ordem(
    body: AbrirOrdemRequest, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Abre a OS para um cliente ativo e um veiculo dele (atendente ou admin)."""
    resultado = obter_abrir_ordem(session).executar(
        AbrirOrdemDTO(
            cliente_id=body.cliente_id,
            veiculo_id=body.veiculo_id,
            descricao_problema=body.descricao_problema,
        )
    )
    return OrdemDeServicoResponse.model_validate(resultado)


@router.get("", summary="Fila de ordens por prioridade de status")
def listar_ordens(
    usuario: _Leitor,
    session: _Sessao,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    incluir_encerradas: Annotated[
        bool,
        Query(description="Inclui finalizadas, entregues e canceladas, ao final."),
    ] = False,
) -> OrdemListaResponse:
    """Pagina por prioridade (EM_EXECUCAO primeiro, RECEBIDA por ultimo).

    Dentro do mesmo status, a mais antiga primeiro. Por padrao omite as
    encerradas; ``total`` acompanha o filtro.
    """
    uc = obter_listar_ordens(session)
    itens = uc.executar(
        offset=offset, limit=limit, incluir_encerradas=incluir_encerradas
    )
    return OrdemListaResponse(
        items=[OrdemResumoResponse.model_validate(i) for i in itens],
        total=uc.contar(incluir_encerradas=incluir_encerradas),
        offset=offset,
        limit=limit,
    )


@router.get("/{ordem_id}", summary="Consulta uma ordem")
def obter_ordem(
    ordem_id: UUID, usuario: _Leitor, session: _Sessao
) -> OrdemDeServicoResponse:
    """Status, resumo do orcamento e do pagamento e timestamps da ordem."""
    return OrdemDeServicoResponse.model_validate(
        obter_obter_ordem(session).executar(ordem_id)
    )


@router.get("/{ordem_id}/historico", summary="Linha do tempo de status da ordem")
def obter_historico(
    ordem_id: UUID, usuario: _Leitor, session: _Sessao
) -> HistoricoResponse:
    """Mudancas de status da abertura ate agora (de, para, origem, motivo)."""
    ordem = obter_obter_ordem(session).executar(ordem_id)
    return HistoricoResponse(
        ordem_id=ordem.id,
        mudancas=[MudancaDeStatusResponse.model_validate(m) for m in ordem.historico],
    )


@router.post(
    "/{ordem_id}/cancelamento",
    summary="Cancela a ordem antes do inicio da execucao",
    responses=_RESPOSTA_409,
)
def cancelar_ordem(
    ordem_id: UUID, body: CancelarOrdemRequest, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Cancela com motivo; 409 depois do inicio da execucao ou se encerrada."""
    resultado = obter_cancelar_ordem(session).executar(ordem_id, body.motivo)
    _log.info("order_cancelled_via_api", ordem_id=str(ordem_id), ator=ator_de(usuario))
    return OrdemDeServicoResponse.model_validate(resultado)


@router.post(
    "/{ordem_id}/entrega",
    summary="Registra a entrega do veiculo (FINALIZADA -> ENTREGUE)",
    responses=_RESPOSTA_409,
)
def registrar_entrega(
    ordem_id: UUID, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Entrega ao cliente; 409 se a ordem nao estiver FINALIZADA."""
    return OrdemDeServicoResponse.model_validate(
        obter_registrar_entrega(session).executar(ordem_id)
    )


@router_publico.post(
    "/acompanhamento",
    summary="Consulta publica do status por placa + documento",
    responses={
        404: {"description": "Nenhuma ordem para o par placa + documento."},
        429: {"description": "Rate limit excedido (10/minute por IP)."},
    },
)
@limiter.limit("10/minute")
def acompanhamento(
    request: Request, corpo: AcompanhamentoRequest, session: _Sessao
) -> AcompanhamentoResponse:
    """Status e timestamps da ordem mais recente do par placa + documento.

    ``POST`` com corpo de proposito, como no p3 (TD-034): placa e documento sao
    PII e nao devem ir para a URL (access log de proxy, historico do browser).
    Nao ha variante GET. O 404 tem corpo identico para placa inexistente,
    documento errado e documento ou placa invalidos (digito verificador ou
    formato, recusados antes de qualquer consulta ao banco): anti-enumeracao,
    mesma resposta do p3. O rate limit por IP completa a defesa.
    """
    resultado = obter_consultar_acompanhamento(session).executar(
        placa=corpo.placa, documento=corpo.documento
    )
    if resultado is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ordem nao encontrada"
        )
    return AcompanhamentoResponse.model_validate(resultado)
