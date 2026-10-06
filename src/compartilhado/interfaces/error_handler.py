from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.compartilhado.dominio.exceptions import (
    AcessoNegadoException,
    ConflitoDeConcorrenciaException,
    DomainException,
    EntidadeDuplicadaException,
    EntidadeNaoEncontradaException,
    FalhaAutenticacaoException,
    TransicaoStatusInvalidaException,
    ValorInvalidoException,
    ViolacaoRegraDeNegocioException,
)
from src.compartilhado.infraestrutura.logging import redigir_pii_erro

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request

logger = structlog.get_logger(__name__)

_EXCEPTION_STATUS_MAP: dict[type[DomainException], int] = {
    EntidadeNaoEncontradaException: 404,
    ViolacaoRegraDeNegocioException: 409,
    TransicaoStatusInvalidaException: 409,
    ConflitoDeConcorrenciaException: 409,
    EntidadeDuplicadaException: 409,
    FalhaAutenticacaoException: 401,
    AcessoNegadoException: 403,
    ValorInvalidoException: 422,
}


# DomainException fora do mapa acima e, por definicao, uma regra de negocio
# violada -> 409 Conflict e o default mais fiel (nunca 500: a excecao e
# esperada e carrega codigo/mensagem proprios).
_STATUS_DEFAULT = 409

# HTTPException do roteamento (rota inexistente, metodo errado) e das rotas:
# os mesmos codigos de Billing e Execucao.
_CODIGOS_HTTP: dict[int, str] = {
    401: "NAO_AUTENTICADO",
    403: "ACESSO_NEGADO",
    404: "ENTIDADE_NAO_ENCONTRADA",
    405: "METODO_NAO_PERMITIDO",
}
# O Starlette usa a frase HTTP em ingles como detail ("Not Found").
_MENSAGENS_PADRAO: dict[int, str] = {
    404: "Recurso nao encontrado",
    405: "Metodo nao permitido",
}


def _status_para(exc: DomainException) -> int:
    """Resolve o status HTTP pela hierarquia da excecao (mais especifico vence).

    Percorre ``type(exc).__mro__`` (subclasse antes do pai) e retorna o primeiro
    match no mapa: assim uma subclasse mapeada a um status proprio sempre vence o
    ancestral, independentemente da ordem de insercao do dict. Sem match ->
    ``_STATUS_DEFAULT`` (409).
    """
    for classe in type(exc).__mro__:
        code = _EXCEPTION_STATUS_MAP.get(classe)
        if code is not None:
            return code
    return _STATUS_DEFAULT


def _obter_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "desconhecido")


def _criar_envelope(codigo: str, mensagem: str, request_id: str) -> dict[str, object]:
    return {
        "erro": {
            "codigo": codigo,
            "mensagem": mensagem,
            "id_requisicao": request_id,
        }
    }


def registrar_error_handlers(app: FastAPI) -> None:
    """Registra handlers que mapeiam DomainException para envelopes HTTP.

    Cada DomainException levantada no request vira um JSONResponse com o envelope
    `{erro: {codigo, mensagem, id_requisicao}}`. Os codigos suportados sao 401,
    403, 404, 409 e 422; a `HTTPException` (rota inexistente, metodo errado)
    sai no mesmo envelope. A invariante de agregado (`ValorInvalidoException`) e a
    de value object (`ValueError`, com a mensagem sem PII) viram 422
    VALOR_INVALIDO -- ver p3 #83. O resto vira 500 ERRO_INTERNO com o
    `id_requisicao` na resposta; o log leva o traceback, ou so tipo, `pgcode` e
    constraint no caso de `DBAPIError` (ver `resposta_erro_interno`).
    """

    @app.exception_handler(DomainException)
    async def _domain_exception_handler(
        request: Request, exc: DomainException
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        status_code = _status_para(exc)
        # Negacoes de dominio (401/404/409) tambem sao registradas -- sem log,
        # picos de 404/409 (enumeration, corrida de duplicidade, transicao
        # invalida) ficariam invisiveis. Nivel WARNING: esperado, mas
        # operacionalmente relevante. So o `codigo` estavel, nunca a mensagem
        # (que pode carregar dado do request).
        # Falha de credencial: a mensagem publica e unica; o motivo so no log.
        motivo = exc.motivo if isinstance(exc, FalhaAutenticacaoException) else None
        logger.warning(
            "dominio_excecao_tratada",
            codigo=exc.codigo,
            status=status_code,
            request_id=request_id,
            reason=motivo,
        )
        return JSONResponse(
            status_code=status_code,
            content=_criar_envelope(exc.codigo, exc.mensagem, request_id),
            headers=(
                {"WWW-Authenticate": "Bearer"}
                if status_code == HTTPStatus.UNAUTHORIZED
                else None
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        codigo = _CODIGOS_HTTP.get(exc.status_code, f"HTTP_{exc.status_code}")
        logger.warning(
            "http_exception_handled",
            codigo=codigo,
            status=exc.status_code,
            request_id=request_id,
        )
        mensagem = str(exc.detail)
        padrao = _MENSAGENS_PADRAO.get(exc.status_code)
        if padrao is not None and mensagem == HTTPStatus(exc.status_code).phrase:
            mensagem = padrao
        # Mantem os headers da excecao (o `Allow` do 405).
        return JSONResponse(
            status_code=exc.status_code,
            content=_criar_envelope(codigo, mensagem, request_id),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # O detail default do FastAPI/Pydantic ecoa o `input` cru (e um `ctx`
        # de conteudo variavel) de cada campo invalido -- PII enviada num campo
        # malformado voltaria no corpo do 422 (TD-033). Mantemos o contrato
        # `{"detail": [...]}` (a UI consome a lista) mas cada item carrega so o
        # trio estavel type/loc/msg: as msgs do Pydantic descrevem a REGRA
        # violada, nao o valor recebido.
        request_id = _obter_request_id(request)
        detalhes = [
            {"type": erro.get("type"), "loc": erro.get("loc"), "msg": erro.get("msg")}
            for erro in exc.errors()
        ]
        logger.warning(
            "validacao_schema_tratada_422",
            request_id=request_id,
            erros=[(d["type"], d["loc"]) for d in detalhes],
        )
        # `id_requisicao` como chave IRMA de `detail`: correlaciona o 422 com
        # os logs sem quebrar o contrato da UI (que le a lista de `detail`).
        return JSONResponse(
            status_code=422,
            content={"detail": detalhes, "id_requisicao": request_id},
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        request_id = _obter_request_id(request)
        logger.warning(
            "value_error_tratado_422",
            request_id=request_id,
            exc_info=exc,
        )
        # str(exc) pode ecoar o valor recebido (ex.: CPF cru numa invariante
        # de value object) -- redige PII antes de devolver ao cliente.
        return JSONResponse(
            status_code=422,
            content=_criar_envelope(
                "VALOR_INVALIDO", redigir_pii_erro(str(exc)), request_id
            ),
        )

    @app.exception_handler(Exception)
    async def _generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # Rede de seguranca: o SecurityHeadersMiddleware ja converte o erro
        # das rotas; aqui so chega o que escapar de um middleware externo.
        return resposta_erro_interno(request, exc)


def resposta_erro_interno(request: Request, exc: Exception) -> JSONResponse:
    """500 no envelope do contrato, com o erro registrado sem PII.

    Erro do driver (``DBAPIError``) vai para o log so com tipo, ``pgcode`` e
    constraint: a mensagem do Postgres traz os valores da linha (``DETAIL:
    Key (placa)=(...)``). Os demais levam o traceback.
    """
    request_id = _obter_request_id(request)
    if isinstance(exc, DBAPIError):
        diagnostico = getattr(exc.orig, "diag", None)
        logger.error(
            "erro_interno",
            request_id=request_id,
            error=type(exc.orig).__name__,
            pgcode=getattr(exc.orig, "pgcode", None),
            constraint=getattr(diagnostico, "constraint_name", None),
        )
    else:
        logger.error("erro_interno", request_id=request_id, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=_criar_envelope(
            "ERRO_INTERNO",
            "Erro interno do servidor",
            request_id,
        ),
    )
