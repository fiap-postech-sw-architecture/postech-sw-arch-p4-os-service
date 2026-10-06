from __future__ import annotations

from contextlib import suppress
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Runtime import (nao TYPE_CHECKING): com `from __future__ import annotations`,
# o FastAPI avalia a annotation `Annotated[Session, Depends(...)]` em runtime;
# sem o nome no modulo, a resolucao da annotation falha no startup
# (PydanticUserError: not fully defined) — verificado empiricamente.
from sqlalchemy.orm import Session

from src.autenticacao.dominio.papel import Papel
from src.autenticacao.interfaces.dependencies import (
    obter_jwt_service,
    obter_token_revogado_repo,
)
from src.compartilhado.dominio.exceptions import FalhaAutenticacaoException
from src.compartilhado.interfaces.dependencies import obter_session

if TYPE_CHECKING:
    from collections.abc import Callable

_bearer_scheme = HTTPBearer(auto_error=False)
_log = structlog.get_logger(__name__)


def _nao_autenticado(motivo: str) -> HTTPException:
    """401 com a mensagem unica (ADR-039); o motivo vai so para o log."""
    _log.warning("authentication_failed", reason=motivo)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=FalhaAutenticacaoException.MENSAGEM,
        headers={"WWW-Authenticate": "Bearer"},
    )


def obter_usuario_atual(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    session: Annotated[Session, Depends(obter_session)],
) -> dict[str, object]:
    if credentials is None:
        raise _nao_autenticado("missing_token")
    try:
        payload = obter_jwt_service().validar_token(credentials.credentials)
    except FalhaAutenticacaoException as e:
        raise _nao_autenticado(e.motivo) from None
    # TD-029: o gate de acesso so aceita access tokens; um refresh token
    # (type="refresh") nao pode autenticar uma requisicao -- espelha o check
    # `type == refresh` do fluxo de refresh. Defense-in-depth alem do RBAC.
    if payload.get("type") != "access":
        raise _nao_autenticado("not_an_access_token")
    # Fail-closed: um payload sem jti nao consegue provar que NAO foi
    # revogado -- rejeitar em vez de pular a checagem de revogacao.
    jti = payload.get("jti")
    if jti is None:
        raise _nao_autenticado("missing_jti")
    if obter_token_revogado_repo(session).esta_revogado(str(jti)):
        raise _nao_autenticado("revoked_token")
    return payload


# Hierarquia de papeis: admin herda atendente e mecanico. Chaves e valores
# tipados como Papel (enum unico em dominio/) para evitar drift por string
# literal. MappingProxyType + frozenset impedem mutacao em tempo de execucao
# (defesa contra escalacao de privilegio via monkey-patch do _PERMISSOES).
_PERMISSOES: MappingProxyType[Papel, frozenset[Papel]] = MappingProxyType(
    {
        Papel.ADMIN: frozenset({Papel.ADMIN, Papel.ATENDENTE, Papel.MECANICO}),
        Papel.ATENDENTE: frozenset({Papel.ATENDENTE}),
        Papel.MECANICO: frozenset({Papel.MECANICO}),
    }
)


def _papel_do_token(usuario: dict[str, object]) -> Papel:
    """Claim ``papel`` do token.

    Ausente, desconhecido ou de tipo errado e falha de credencial (401, ADR-039):
    nenhum token emitido por este servico e assim. O 403 fica para o papel
    valido sem permissao na rota.
    """
    bruto = usuario.get("papel")
    if isinstance(bruto, str):
        with suppress(ValueError):
            return Papel(bruto)
    raise _nao_autenticado("invalid_role_claim")


def exigir_papel(
    *papeis: str,
) -> Callable[..., dict[str, object]]:
    if not papeis:
        raise ValueError("exigir_papel requer ao menos um papel")
    try:
        papeis_exigidos: frozenset[Papel] = frozenset(Papel(p) for p in papeis)
    except ValueError as exc:
        raise ValueError(f"exigir_papel recebeu papel invalido: {exc}") from exc

    def verificar(
        usuario: Annotated[dict[str, object], Depends(obter_usuario_atual)],
    ) -> dict[str, object]:
        papel = _papel_do_token(usuario)
        if not _PERMISSOES.get(papel, frozenset()) & papeis_exigidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Papel nao autorizado",
            )
        return usuario

    return verificar
