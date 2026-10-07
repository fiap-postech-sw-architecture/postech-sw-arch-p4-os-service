"""Routers HTTP do contexto Ordem de Servico (RFC-004 secao 6.1, parte OS).

``router`` (JWT + papel): abertura, fila por prioridade, consulta, historico,
cancelamento e entrega. ``router_sagas`` (admin): estado da saga para a
operacao. ``router_publico`` (sem token, rate limit por IP): acompanhamento
por placa + documento. As transicoes da saga (diagnostico, orcamento,
pagamento, execucao) chegam por mensageria, nao por rota HTTP.
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
    obter_obter_saga,
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
    PassoDaSagaResponse,
    SagaResponse,
)

_log = structlog.get_logger(__name__)

# Respostas documentadas no OpenAPI (Any: e o tipo do parametro `responses`
# do FastAPI). 401 e 403 valem para toda rota autenticada do router.
_RESPOSTAS_AUTENTICADAS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Credencial ausente, invalida, expirada ou revogada."},
    403: {"description": "Papel sem acesso: so atendente e admin."},
}
_RESPOSTA_404: dict[int | str, dict[str, Any]] = {
    404: {"description": "Ordem inexistente."}
}
_RESPOSTA_409: dict[int | str, dict[str, Any]] = {
    409: {
        "description": (
            "Transicao invalida para o status atual ou escrita concorrente "
            "(versao divergente)."
        )
    }
}

router = APIRouter(
    prefix="/api/v1/ordens-de-servico",
    tags=["ordens-de-servico"],
    responses=_RESPOSTAS_AUTENTICADAS,
)
router_sagas = APIRouter(
    prefix="/api/v1/sagas",
    tags=["sagas"],
    responses={
        401: _RESPOSTAS_AUTENTICADAS[401],
        403: {"description": "Papel sem acesso: so admin."},
    },
)
router_publico = APIRouter(prefix="/api/v1/publico", tags=["publico"])

# Toda rota de OS e do atendente (admin herda); o mecanico nao tem rota no
# OS Service: trabalha pela fila do Execution Service (ADR-039).
_Atendente = Annotated[dict[str, object], Depends(exigir_papel(Papel.ATENDENTE))]
# A saga e consultada pela operacao (runbook da saga, ADR-039).
_Admin = Annotated[dict[str, object], Depends(exigir_papel(Papel.ADMIN))]
_Sessao = Annotated[Session, Depends(obter_session)]


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
            ator=ator_de(usuario),
        )
    )
    return OrdemDeServicoResponse.model_validate(resultado)


@router.get("", summary="Fila de ordens por prioridade de status")
def listar_ordens(
    usuario: _Atendente,
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


@router.get("/{ordem_id}", summary="Consulta uma ordem", responses=_RESPOSTA_404)
def obter_ordem(
    ordem_id: UUID, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Status, resumo do orcamento e do pagamento e timestamps da ordem."""
    return OrdemDeServicoResponse.model_validate(
        obter_obter_ordem(session).executar(ordem_id)
    )


@router.get(
    "/{ordem_id}/historico",
    summary="Linha do tempo de status da ordem",
    responses=_RESPOSTA_404,
)
def obter_historico(
    ordem_id: UUID, usuario: _Atendente, session: _Sessao
) -> HistoricoResponse:
    """Mudancas de status (de, para, origem, ator, motivo) e passos da saga."""
    ordem = obter_obter_ordem(session).executar(ordem_id)
    return HistoricoResponse(
        ordem_id=ordem.id,
        mudancas=[MudancaDeStatusResponse.model_validate(m) for m in ordem.historico],
        passos=[PassoDaSagaResponse.model_validate(p) for p in ordem.passos],
    )


@router.post(
    "/{ordem_id}/cancelamento",
    summary="Cancela a ordem antes do inicio da execucao",
    responses={**_RESPOSTA_404, **_RESPOSTA_409},
)
def cancelar_ordem(
    ordem_id: UUID, body: CancelarOrdemRequest, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Cancela com motivo; 409 depois do inicio da execucao ou se encerrada."""
    ator = ator_de(usuario)
    resultado = obter_cancelar_ordem(session).executar(ordem_id, body.motivo, ator=ator)
    _log.info("order_cancelled_via_api", ordem_id=str(ordem_id), ator=ator)
    return OrdemDeServicoResponse.model_validate(resultado)


@router.post(
    "/{ordem_id}/entrega",
    summary="Registra a entrega do veiculo (FINALIZADA -> ENTREGUE)",
    responses={**_RESPOSTA_404, **_RESPOSTA_409},
)
def registrar_entrega(
    ordem_id: UUID, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Entrega ao cliente; 409 se a ordem nao estiver FINALIZADA."""
    return OrdemDeServicoResponse.model_validate(
        obter_registrar_entrega(session).executar(ordem_id, ator=ator_de(usuario))
    )


@router_sagas.get(
    "/{ordem_id}",
    summary="Estado da saga de uma ordem (operacao)",
    responses={404: {"description": "Ordem sem saga."}},
)
def obter_saga(ordem_id: UUID, usuario: _Admin, session: _Sessao) -> SagaResponse:
    """Etapa, motivo, falha, plano restante, comando em voo, reenvios, prazo e passos.

    E a primeira consulta do runbook da saga (RFC-004 secao 4.7).
    """
    return SagaResponse.model_validate(obter_obter_saga(session).executar(ordem_id))


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
    Nao ha variante GET. O 404 (envelope de erro, ``ENTIDADE_NAO_ENCONTRADA``)
    tem o mesmo codigo e a mesma mensagem para placa inexistente, documento
    errado e documento ou placa invalidos (digito verificador ou formato,
    recusados antes de qualquer consulta ao banco): anti-enumeracao. O rate
    limit por IP completa a defesa.
    """
    resultado = obter_consultar_acompanhamento(session).executar(
        placa=corpo.placa, documento=corpo.documento
    )
    if resultado is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ordem nao encontrada"
        )
    return AcompanhamentoResponse.model_validate(resultado)
