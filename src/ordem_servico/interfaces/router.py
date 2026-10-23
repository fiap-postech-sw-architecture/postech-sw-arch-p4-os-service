"""Routers HTTP do contexto Ordem de Servico (RFC-004 secao 6.1, parte OS).

``router`` (JWT + papel): abertura, fila por prioridade, consulta, historico,
cancelamento, entrega e, so para admin, as metricas das ordens (``/metricas``).
``router_sagas`` (admin): estado da saga e retomada da compensacao parada, para a
operacao. ``router_publico`` (sem token, rate limit
por IP): acompanhamento por placa + documento. As transicoes da saga
(diagnostico, orcamento, pagamento, execucao) chegam por mensageria, nao por
rota HTTP.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID  # noqa: TC003

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
from src.compartilhado.interfaces.respostas import erro, limite_excedido
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_cancelar_ordem,
    obter_consultar_acompanhamento,
    obter_listar_ordens,
    obter_obter_metricas,
    obter_obter_ordem,
    obter_obter_saga,
    obter_registrar_entrega,
    obter_retomar_compensacao,
)
from src.ordem_servico.interfaces.schemas import (
    AbrirOrdemRequest,
    AcompanhamentoRequest,
    AcompanhamentoResponse,
    CancelarOrdemRequest,
    HistoricoResponse,
    MetricasResponse,
    MudancaDeStatusResponse,
    OrdemDeServicoResponse,
    OrdemListaResponse,
    OrdemResumoResponse,
    PassoDaSagaResponse,
    SagaResponse,
)

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
_LIMITE_ACOMPANHAMENTO = "10/minute"

# Toda rota de OS e do atendente (admin herda); o mecanico nao tem rota no
# OS Service: trabalha pela fila do Execution Service (ADR-039).
_Atendente = Annotated[dict[str, object], Depends(exigir_papel(Papel.ATENDENTE))]
# A saga e consultada pela operacao (runbook da saga, ADR-039).
_Admin = Annotated[dict[str, object], Depends(exigir_papel(Papel.ADMIN))]
_Sessao = Annotated[Session, Depends(obter_session)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Abre uma ordem de servico em RECEBIDA e inicia a saga",
    responses={404: {"description": "Cliente inativo/inexistente ou veiculo alheio."}},
)
def abrir_ordem(
    body: AbrirOrdemRequest, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Abre a OS para um cliente ativo e um veiculo dele (atendente ou admin).

    No mesmo commit, a saga de atendimento nasce em ``aguardando_diagnostico``
    e o ``SolicitarDiagnostico`` vai para a outbox, rumo a Execucao.
    """
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


# Declarada antes de `/{ordem_id}`: do contrario "metricas" cairia no UUID.
@router.get(
    "/metricas",
    summary="Total, ordens por status e tempo medio em cada status",
    responses={403: {"description": "Papel sem acesso: so admin."}},
)
def obter_metricas(usuario: _Admin, session: _Sessao) -> MetricasResponse:
    """Os numeros que o dashboard de negocio mostra, do banco, na hora (RF-027).

    A permanencia media de cada status e a do historico: da entrada no status ate
    a transicao seguinte da mesma ordem, nas entradas dos ultimos 30 dias. E a
    mesma leitura dos gauges `pytstop_os_por_status` e
    `pytstop_os_tempo_medio_status_segundos`.
    """
    return MetricasResponse.model_validate(obter_obter_metricas(session).executar())


@router.get("/{ordem_id}", summary="Consulta uma ordem", responses=_RESPOSTA_404)
def obter_ordem(
    ordem_id: UUID, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Status, etapa da saga, resumo do orcamento e do pagamento e timestamps.

    O status e a etapa saem da mesma leitura: nunca divergem.
    """
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
    """Mudancas de status (de, para, origem, ator, motivo) e registros da saga.

    Duas listas, cada uma na sua ordem: o instante da mudanca da OS e o do
    registro da saga vem de leituras de relogio diferentes, e intercala-las
    poderia mostrar o efeito antes da causa.
    """
    ordem = obter_obter_ordem(session).executar(ordem_id)
    return HistoricoResponse(
        ordem_id=ordem.id,
        mudancas=[MudancaDeStatusResponse.model_validate(m) for m in ordem.historico],
        passos=[PassoDaSagaResponse.model_validate(p) for p in ordem.passos],
    )


@router.post(
    "/{ordem_id}/cancelamento",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Pede o cancelamento: a saga desfaz os passos e cancela a ordem",
    responses={
        202: {
            "description": (
                "Cancelamento aceito: a OS mantem o status e a saga entra em "
                "`compensando`. O pedido repetido, com a compensacao em curso ou "
                "parada, tambem responde 202, sem efeito."
            )
        },
        404: erro(
            "Ordem inexistente (ou sem saga, o que uma OS aberta nao deveria ser).",
            "ENTIDADE_NAO_ENCONTRADA",
            "Ordem de servico 5f0c7a52-3d0e-4c39-9d76-0f2b8f4f6a11 nao encontrada",
        ),
        409: erro(
            "`TRANSICAO_STATUS_INVALIDA`: execucao ja iniciada (o ponto sem "
            "retorno) ou ordem ja cancelada. `CONFLITO_DE_CONCORRENCIA`: escrita "
            "concorrente que voltou nas tres leituras.",
            "TRANSICAO_STATUS_INVALIDA",
            "Cancelamento recusado: execução já iniciada",
        ),
        422: erro(
            "`VALOR_INVALIDO`: motivo vazio, longo demais ou com caractere de "
            "controle ou de formatacao. O corpo fora do schema (sem `motivo`, campo "
            "extra) sai no formato de validacao de schema, `{detail, id_requisicao}`.",
            "VALOR_INVALIDO",
            "motivo de cancelamento e obrigatorio",
        ),
    },
)
def cancelar_ordem(
    ordem_id: UUID, body: CancelarOrdemRequest, usuario: _Atendente, session: _Sessao
) -> OrdemDeServicoResponse:
    """Pede o cancelamento antes do inicio da execucao (RFC-004 secao 4.4).

    202 com a ordem e a etapa ``compensando``: a OS guarda o motivo e mantem o
    status ate a saga desfazer os passos feitos, um por vez; so entao fica
    ``cancelada`` (acompanhe pela etapa e pelo historico). Com a compensacao em
    curso ou parada, o pedido repetido devolve 202 com a etapa atual, sem
    efeito. Depois do inicio da execucao, 409 ("Cancelamento recusado:
    execução já iniciada"); ordem ja cancelada, 409 ("Ordem já cancelada").
    Toda OS tem saga desde a abertura: sem ela, 404.
    """
    resultado = obter_cancelar_ordem(session).executar(
        ordem_id, body.motivo, ator=ator_de(usuario)
    )
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


@router_sagas.post(
    "/{ordem_id}/compensacao",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retoma a compensacao parada (falha_na_compensacao)",
    responses={
        202: {
            "description": (
                "Retomada aceita: a compensacao pendente sai de novo, com id novo, "
                "prazo novo e contador de reenvios zerado, e a saga volta a "
                "`compensando`."
            )
        },
        404: erro(
            "Ordem sem saga.",
            "ENTIDADE_NAO_ENCONTRADA",
            "Saga da ordem 5f0c7a52-3d0e-4c39-9d76-0f2b8f4f6a11 nao encontrada",
        ),
        409: erro(
            "`TRANSICAO_STATUS_INVALIDA`: a saga nao esta em "
            "`falha_na_compensacao`. `CONFLITO_DE_CONCORRENCIA`: o consumidor "
            "gravou a saga no meio; repita o pedido.",
            "TRANSICAO_STATUS_INVALIDA",
            "Retomada recusada: a saga não está em falha na compensação",
        ),
    },
)
def retomar_compensacao(
    ordem_id: UUID, usuario: _Admin, session: _Sessao
) -> SagaResponse:
    """Reenvia a compensacao pendente da saga parada (RFC-004 secao 4.7).

    So em `falha_na_compensacao`, depois de o operador resolver a causa (runbook da
    saga): o comando sai de novo com `id` novo, a saga volta a `compensando` e
    segue o plano ate `compensada`. O participante que ja tinha feito a
    compensacao responde com o desfecho registrado. Fora da falha, 409.
    """
    return SagaResponse.model_validate(
        obter_retomar_compensacao(session).executar(ordem_id, ator=ator_de(usuario))
    )


@router_publico.post(
    "/acompanhamento",
    summary="Consulta publica do status por placa + documento",
    responses={
        404: {"description": "Nenhuma ordem para o par placa + documento."},
        429: limite_excedido(_LIMITE_ACOMPANHAMENTO),
    },
)
@limiter.limit(_LIMITE_ACOMPANHAMENTO)
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
