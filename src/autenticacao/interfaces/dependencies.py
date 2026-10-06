from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING, Final

from src.autenticacao.infraestrutura.jwt_service import (
    JWTService,
    carregar_chave_privada,
    carregar_chave_publica,
)
from src.autenticacao.infraestrutura.password_hasher import PasswordHasher
from src.compartilhado.infraestrutura.database import AMBIENTES_DEV

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.autenticacao.aplicacao.use_cases import (
        Login,
        Logout,
        RefreshToken,
        Registrar,
    )
    from src.autenticacao.dominio.repository import TokenRevogadoRepository


def obter_token_revogado_repo(session: Session) -> TokenRevogadoRepository:
    """Factory do repositorio de tokens revogados (reusada pelo middleware).

    Mantem a construcao do concreto no composition root — o middleware de
    auth consome via Protocol (finding da revisao arquitetural).
    """
    from src.autenticacao.infraestrutura.token_revogado_repository import (
        TokenRevogadoSQLAlchemyRepository,
    )

    return TokenRevogadoSQLAlchemyRepository(session=session)


# kids (RFC 7638) das chaves RSA de demonstracao do docker-compose.yml e do
# .env.example: publicas no git, recusadas fora de development/test. So cresce:
# ao trocar a chave de demonstracao, o kid da antiga fica aqui, porque ela
# continua no historico do git.
KIDS_DAS_CHAVES_DEMO: Final = frozenset({"VmyReO-ecFv1-Etlmu9FZASx9UzxX_BJlxxPB4PUYVY"})

# Tetos da validade dos tokens (ADR-039): o access circula entre os servicos e so
# o OS consulta a revogacao, entao a expiracao curta e o limite; o refresh vale
# ate 30 dias.
ACCESS_MAXIMO_MINUTOS: Final = 60
REFRESH_MAXIMO_MINUTOS: Final = 30 * 24 * 60


def _minutos_do_ambiente(nome: str, padrao: int, maximo: int) -> int:
    """Validade em minutos de ``nome``: inteiro de 1 a ``maximo``.

    Valor ruim e erro de configuracao do servidor (``RuntimeError``), nunca um
    4xx de quem chamou, nem um ``OverflowError`` no primeiro login.
    """
    try:
        minutos = int(os.environ.get(nome, str(padrao)))
    except ValueError:
        minutos = 0
    if not 0 < minutos <= maximo:
        msg = f"{nome} precisa ser um inteiro de 1 a {maximo} (minutos)"
        raise RuntimeError(msg)
    return minutos


def obter_jwt_service() -> JWTService:
    """Servico de tokens com a chave RSA e as validades do ambiente.

    ``JWT_PRIVATE_KEY``: chave privada RSA em PEM, de 2048 bits ou mais (Secret
    no cluster). ``JWT_PREVIOUS_PUBLIC_KEY``, opcional: a publica da chave que
    entra (etapa 1 da rotacao) ou da que sai (etapa 2); so e publicada e aceita
    na validacao, nunca assina. Access de 15 min (ADR-039): o token circula
    entre os servicos e so o OS consulta a revogacao; nos demais o limite e a
    expiracao curta.
    """
    return _jwt_service(
        # Sem espaco nas pontas: um Secret criado com `echo` traz uma quebra de
        # linha, e so isso nao pode derrubar o boot.
        os.environ.get("JWT_PRIVATE_KEY", "").strip(),
        os.environ.get("JWT_PREVIOUS_PUBLIC_KEY", "").strip(),
        _minutos_do_ambiente("JWT_EXPIRATION_MINUTES", 15, ACCESS_MAXIMO_MINUTOS),
        _minutos_do_ambiente(
            "JWT_REFRESH_EXPIRATION_MINUTES", 10080, REFRESH_MAXIMO_MINUTOS
        ),
    )


@functools.lru_cache(maxsize=4)
def _jwt_service(
    pem: str, pem_anterior: str, expiracao: int, refresh_expiracao: int
) -> JWTService:
    # Uma instancia por configuracao: ler e conferir a chave RSA custa
    # milissegundos, e o gate roda em toda requisicao. Configuracao ausente ou
    # invalida e erro do servidor (RuntimeError), nunca um 4xx do cliente.
    if not pem:
        msg = "JWT_PRIVATE_KEY nao configurada: chave privada RSA (PEM), 2048 bits+"
        raise RuntimeError(msg)
    try:
        chave_privada = carregar_chave_privada(pem)
    except ValueError as exc:
        msg = f"JWT_PRIVATE_KEY invalida: {exc}"
        raise RuntimeError(msg) from exc
    try:
        chave_anterior = carregar_chave_publica(pem_anterior) if pem_anterior else None
    except ValueError as exc:
        msg = f"JWT_PREVIOUS_PUBLIC_KEY invalida: {exc}"
        raise RuntimeError(msg) from exc
    return JWTService(
        chave_privada=chave_privada,
        expiracao_minutos=expiracao,
        refresh_expiracao_minutos=refresh_expiracao,
        chave_anterior=chave_anterior,
    )


def validar_chave_jwt_no_startup() -> None:
    """Fora de development/test, aborta o boot sem configuracao de JWT utilizavel.

    Chave ausente, ilegivel, que nao e RSA, com menos de 2048 bits ou igual a
    de demonstracao (publica no git), como atual ou como anterior: um token
    assinado com a chave de demonstracao seria aceito pelos tres servicos. Aborta
    tambem com validade fora dos limites (access de 1 a 60 min, refresh de 1 min
    a 30 dias).
    """
    if os.environ.get("ENVIRONMENT", "development").lower() in AMBIENTES_DEV:
        return
    kids = {chave["kid"] for chave in obter_jwt_service().jwks()["keys"]}
    if kids & KIDS_DAS_CHAVES_DEMO:
        msg = (
            "JWT_PRIVATE_KEY ou JWT_PREVIOUS_PUBLIC_KEY usa a chave RSA de "
            "demonstracao, publica no git: proibida fora de development/test. "
            "Gere outra (openssl genpkey -algorithm RSA "
            "-pkeyopt rsa_keygen_bits:2048) e injete via Secret."
        )
        raise RuntimeError(msg)


def obter_registrar(session: Session) -> Registrar:
    from src.autenticacao.aplicacao.use_cases import Registrar
    from src.autenticacao.infraestrutura.repository import (
        UsuarioSQLAlchemyRepository,
    )
    from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork

    return Registrar(
        repo=UsuarioSQLAlchemyRepository(session=session),
        uow=SQLAlchemyUnitOfWork(session_factory=lambda: session),
        password_hasher=PasswordHasher(),
    )


def obter_login(session: Session) -> Login:
    from src.autenticacao.aplicacao.use_cases import Login
    from src.autenticacao.infraestrutura.repository import (
        UsuarioSQLAlchemyRepository,
    )

    return Login(
        repo=UsuarioSQLAlchemyRepository(session=session),
        jwt_service=obter_jwt_service(),
        password_hasher=PasswordHasher(),
    )


def obter_logout(session: Session) -> Logout:
    from src.autenticacao.aplicacao.use_cases import Logout
    from src.autenticacao.infraestrutura.token_revogado_repository import (
        TokenRevogadoSQLAlchemyRepository,
    )
    from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork

    return Logout(
        jwt_service=obter_jwt_service(),
        token_repo=TokenRevogadoSQLAlchemyRepository(session=session),
        uow=SQLAlchemyUnitOfWork(session_factory=lambda: session),
    )


def obter_refresh_token(session: Session) -> RefreshToken:
    from src.autenticacao.aplicacao.use_cases import RefreshToken
    from src.autenticacao.infraestrutura.repository import (
        UsuarioSQLAlchemyRepository,
    )
    from src.autenticacao.infraestrutura.token_revogado_repository import (
        TokenRevogadoSQLAlchemyRepository,
    )
    from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork

    return RefreshToken(
        jwt_service=obter_jwt_service(),
        token_repo=TokenRevogadoSQLAlchemyRepository(session=session),
        usuario_repo=UsuarioSQLAlchemyRepository(session=session),
        uow=SQLAlchemyUnitOfWork(session_factory=lambda: session),
    )
