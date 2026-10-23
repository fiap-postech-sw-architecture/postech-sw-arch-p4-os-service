from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Runtime import (nao TYPE_CHECKING): com `from __future__ import annotations`,
# o FastAPI avalia a annotation `Annotated[Session, Depends(...)]` em runtime;
# sem o nome no modulo, a resolucao da annotation falha no startup
# (PydanticUserError: not fully defined) — verificado empiricamente.
from sqlalchemy.orm import Session  # noqa: TC002
from starlette.requests import Request  # noqa: TC002

from src.autenticacao.aplicacao.dtos import LoginDTO, RegistrarDTO
from src.autenticacao.dominio.exceptions import TokenInvalidoException
from src.autenticacao.interfaces.dependencies import (
    obter_jwt_service,
    obter_login,
    obter_logout,
    obter_refresh_token,
    obter_registrar,
)
from src.autenticacao.interfaces.middleware import exigir_papel
from src.autenticacao.interfaces.schemas import (
    JwksResponse,
    LoginRequest,
    RefreshRequest,
    RegistrarRequest,
    TokenResponse,
    UsuarioResponse,
)
from src.compartilhado.dominio.exceptions import FalhaAutenticacaoException
from src.compartilhado.interfaces.dependencies import obter_session
from src.compartilhado.interfaces.middleware import limiter
from src.compartilhado.interfaces.respostas import erro, limite_excedido

router = APIRouter(prefix="/api/v1/autenticacao", tags=["autenticacao"])
# Fora de /api/v1: o caminho padrao do JWKS, lido por Billing e Execucao.
router_jwks = APIRouter(tags=["autenticacao"])

# auto_error=False: o HTTPBearer default responde 403 sem WWW-Authenticate
# para header ausente; o 401 manual abaixo espelha `obter_usuario_atual`.
_bearer_scheme = HTTPBearer(auto_error=False)

# O mesmo limite vale na rota (`@limiter.limit`) e no 429 do OpenAPI.
_LIMITE_CREDENCIAL = "5/minute"
_LIMITE_SESSAO = "10/minute"
_LIMITE_JWKS = "60/minute"


def _nao_autenticado(descricao: str) -> dict[str, Any]:
    """401 da rota: toda falha de credencial tem o mesmo codigo e a mesma mensagem."""
    return erro(descricao, "NAO_AUTENTICADO", FalhaAutenticacaoException.MENSAGEM)


@router.post(
    "/login",
    summary="Autentica e emite par de tokens",
    responses={
        401: _nao_autenticado(
            "E-mail inexistente e senha errada: a mesma resposta para os dois."
        ),
        429: limite_excedido(_LIMITE_CREDENCIAL),
    },
)
@limiter.limit(_LIMITE_CREDENCIAL)
def login(
    request: Request,
    body: LoginRequest,
    session: Annotated[Session, Depends(obter_session)],
) -> TokenResponse:
    """Valida e-mail + senha e devolve access + refresh tokens (rate limit 5/min).

    E-mail inexistente e senha errada respondem o mesmo 401 (`NAO_AUTENTICADO`,
    "Credencial ausente, invalida ou expirada"), para nao revelar quais e-mails
    existem.
    """
    uc = obter_login(session)
    dto = LoginDTO(email=body.email, senha=body.senha)
    result = uc.executar(dto)
    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type=result.token_type,
    )


@router.post(
    "/registrar",
    status_code=status.HTTP_201_CREATED,
    summary="Registra um novo usuario (admin)",
    responses={
        401: _nao_autenticado("Credencial ausente, invalida, expirada ou revogada."),
        403: erro(
            "Papel sem acesso: so admin.",
            "ACESSO_NEGADO",
            "Papel nao autorizado para esta operacao",
        ),
        409: erro("E-mail ja cadastrado.", "ENTIDADE_DUPLICADA", "Email ja cadastrado"),
        429: limite_excedido(_LIMITE_CREDENCIAL),
    },
)
@limiter.limit(_LIMITE_CREDENCIAL)
def registrar(
    request: Request,
    body: RegistrarRequest,
    usuario: Annotated[dict[str, object], Depends(exigir_papel("admin"))],
    session: Annotated[Session, Depends(obter_session)],
) -> UsuarioResponse:
    """Cria um usuario com o papel informado. Endpoint admin-gated (RBAC).

    E-mail ja cadastrado -> 409. O papel e obrigatorio no corpo (nunca um
    admin silencioso por omissao).
    """
    uc = obter_registrar(session)
    dto = RegistrarDTO(email=body.email, senha=body.senha, papel=body.papel)
    result = uc.executar(dto)
    return UsuarioResponse(id=result.id, email=result.email, papel=result.papel)


@router.post(
    "/logout",
    summary="Revoga o access e, opcionalmente, o refresh",
    responses={
        401: _nao_autenticado(
            "Header `Authorization` ausente, ou token invalido, expirado ou que "
            "nao e de acesso."
        ),
        429: limite_excedido(_LIMITE_SESSAO),
    },
)
@limiter.limit(_LIMITE_SESSAO)
def logout(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    session: Annotated[Session, Depends(obter_session)],
    refresh_token: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, str]:
    """Revoga o access token do header; se o refresh vier no corpo, revoga-o tambem.

    Sem o refresh, encerra so o access (compat com clientes que so mandam o
    header). Header ausente -> 401.
    """
    if credentials is None:
        # Mesmo 401 (e mesmo envelope) do token invalido no logout.
        raise TokenInvalidoException(motivo="missing_token")
    # refresh_token e OPCIONAL no corpo (p3 #118): quando enviado, o logout
    # revoga tambem o refresh e encerra a sessao por completo (CWE-613). Corpo
    # ausente mantem o comportamento anterior (revoga so o access) — sem 422
    # para clientes que so mandam o header.
    uc = obter_logout(session)
    return uc.executar(credentials.credentials, refresh_token=refresh_token)


@router.post(
    "/refresh",
    summary="Rotaciona o par de tokens via refresh",
    responses={
        401: _nao_autenticado("Refresh invalido, expirado, ja usado ou revogado."),
        429: limite_excedido(_LIMITE_SESSAO),
    },
)
@limiter.limit(_LIMITE_SESSAO)
def refresh(
    request: Request,
    body: RefreshRequest,
    session: Annotated[Session, Depends(obter_session)],
) -> TokenResponse:
    """Troca um refresh token valido por um novo par (access + refresh).

    Single-use: o refresh apresentado e revogado atomicamente. Refresh
    invalido/expirado/ja consumido -> 401.
    """
    uc = obter_refresh_token(session)
    result = uc.executar(body.refresh_token)
    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type=result.token_type,
    )


@router_jwks.get(
    "/.well-known/jwks.json",
    summary="Chaves publicas que validam os tokens (JWKS)",
    # O response_model valida o que o servico monta: um JWKS inconsistente e
    # erro do servidor (500), nao 422 de quem chamou.
    response_model=JwksResponse,
    responses={
        200: {
            "description": (
                "JWK Set com a chave que assina os tokens e, durante uma rotacao, "
                "a outra chave publicada."
            ),
            "headers": {
                "Cache-Control": {
                    "description": "Os consumidores podem guardar o JWKS por 10 min.",
                    "schema": {"type": "string", "example": "public, max-age=600"},
                }
            },
        },
        429: limite_excedido(_LIMITE_JWKS),
    },
)
@limiter.limit(_LIMITE_JWKS)
# `async` de proposito: a rota so monta um dict em memoria e, no event loop,
# responde mesmo com o threadpool das rotas sincronas cheio.
async def jwks(request: Request, response: Response) -> dict[str, list[dict[str, str]]]:
    """JWK Set (RFC 7517) com as chaves publicas que validam os tokens do servico.

    Publico, sem token: Billing e Execucao validam os JWT localmente com estas
    chaves (ADR-039). A resposta traz a chave que assina e, durante uma rotacao,
    a outra chave publicada.
    """
    response.headers["Cache-Control"] = "public, max-age=600"
    return obter_jwt_service().jwks()
