from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError

from src.autenticacao.aplicacao.dtos import TokenDTO, UsuarioDTO
from src.autenticacao.dominio.exceptions import (
    CredenciaisInvalidasException,
    EmailDuplicadoException,
    TokenExpiradoException,
    TokenInvalidoException,
    TokenRevogadoException,
)
from src.autenticacao.dominio.usuario import Usuario

if TYPE_CHECKING:
    from src.autenticacao.aplicacao.dtos import LoginDTO, RegistrarDTO
    from src.autenticacao.aplicacao.ports import JWTServicePort, PasswordHasherPort
    from src.autenticacao.dominio.repository import (
        TokenRevogadoRepository,
        UsuarioRepository,
    )
    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork

_log = structlog.get_logger(__name__)

# Hash bcrypt fixo de uma string aleatoria constante (nao corresponde a senha
# de ninguem). Quando o e-mail nao existe, o Login verifica a senha contra este
# hash antes de falhar: sem isso, o retorno imediato do ramo "usuario is None"
# seria um oraculo de timing (CWE-208) revelando quais e-mails estao
# cadastrados.
_HASH_DUMMY_TIMING = "$2b$12$avojtsGVsT2GPpVLbG3xj.X1U5TrhWpmU6wFYHA035hvRoejQwVmC"


class Registrar:
    def __init__(
        self,
        repo: UsuarioRepository,
        uow: UnitOfWork,
        password_hasher: PasswordHasherPort,
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._password_hasher = password_hasher

    def executar(self, dto: RegistrarDTO) -> UsuarioDTO:
        # E-mail normalizado para lowercase na entrada: armazenar e buscar
        # sempre em caixa baixa evita duplicatas por caixa e login que falha
        # conforme a caixa digitada.
        email = dto.email.lower()
        if self._repo.email_existe(email):
            raise EmailDuplicadoException()
        senha_hash = self._password_hasher.hash_senha(dto.senha)
        usuario = Usuario.criar(email=email, senha_hash=senha_hash, papel=dto.papel)
        # O guard email_existe acima e check-then-insert: dois registros
        # concorrentes do mesmo e-mail passam ambos no check e o segundo
        # estoura o UNIQUE no flush/commit. O IntegrityError vira o mesmo 409
        # do caminho sequencial em vez de 500.
        try:
            with self._uow:
                self._repo.salvar(usuario)
                self._uow.commit()
        except IntegrityError:
            raise EmailDuplicadoException() from None
        return UsuarioDTO(id=usuario.id, email=usuario.email, papel=usuario.papel.value)


class Login:
    def __init__(
        self,
        repo: UsuarioRepository,
        jwt_service: JWTServicePort,
        password_hasher: PasswordHasherPort,
    ) -> None:
        self._repo = repo
        self._jwt_service = jwt_service
        self._password_hasher = password_hasher

    def executar(self, dto: LoginDTO) -> TokenDTO:
        usuario = self._repo.obter_por_email(dto.email.lower())
        if usuario is None:
            # Equaliza o custo com o ramo de senha errada (CWE-208): verifica
            # contra um hash dummy para o tempo nao denunciar e-mails validos.
            self._password_hasher.verificar_senha(dto.senha, _HASH_DUMMY_TIMING)
            raise CredenciaisInvalidasException(motivo="unknown_email")
        if not self._password_hasher.verificar_senha(dto.senha, usuario.senha_hash):
            raise CredenciaisInvalidasException(motivo="wrong_password")
        access = self._jwt_service.gerar_access_token(
            usuario_id=usuario.id, papel=usuario.papel.value
        )
        refresh = self._jwt_service.gerar_refresh_token(
            usuario_id=usuario.id,
        )
        return TokenDTO(access_token=access, refresh_token=refresh)


class Logout:
    def __init__(
        self,
        jwt_service: JWTServicePort,
        token_repo: TokenRevogadoRepository,
        uow: UnitOfWork,
    ) -> None:
        self._jwt_service = jwt_service
        self._token_repo = token_repo
        self._uow = uow

    def executar(
        self, token: str, *, refresh_token: str | None = None
    ) -> dict[str, str]:
        """Revoga o access token e, se fornecido, o refresh do mesmo usuario.

        Sem revogar o refresh (p3 #118, CWE-613) o logout so encerra o
        access: um ``POST /refresh`` com o refresh emitido no mesmo login ainda
        cunharia um novo par apos o logout, deixando a sessao viva. O cliente
        envia o refresh no corpo para encerrar a sessao por completo; revogar
        o refresh e best-effort — refresh ausente/invalido/expirado ou de outro
        usuario nao falha a revogacao do access.
        """
        payload = self._jwt_service.validar_token(token)
        # Simetria com o gate de acesso (TD-029): so um ACCESS token encerra a
        # sessao; um refresh valido no header nao pode autenticar o logout.
        if payload.get("type") != "access":
            raise TokenInvalidoException(motivo="not_an_access_token")
        jtis = [str(payload["jti"])]
        if refresh_token is not None:
            jti_refresh = self._jti_refresh_para_revogar(
                refresh_token, sub=str(payload.get("sub"))
            )
            if jti_refresh is not None:
                jtis.append(jti_refresh)
        with self._uow:
            # Sempre na mesma ordem: dois logouts simultaneos da mesma sessao
            # (pods diferentes) esperam um pelo outro no UNIQUE do jti e
            # travariam em deadlock se revogassem os dois jti em ordens opostas.
            for jti in sorted(jtis):
                self._token_repo.revogar(jti)
            self._uow.commit()
        return {"mensagem": "Logout realizado com sucesso"}

    def _jti_refresh_para_revogar(self, refresh_token: str, *, sub: str) -> str | None:
        """jti do refresh se for um refresh valido do MESMO usuario; senao None."""
        try:
            payload = self._jwt_service.validar_token(refresh_token)
        except (TokenExpiradoException, TokenInvalidoException):
            return None
        if payload.get("type") != "refresh" or str(payload.get("sub")) != sub:
            return None
        return str(payload["jti"])


class RefreshToken:
    def __init__(
        self,
        jwt_service: JWTServicePort,
        token_repo: TokenRevogadoRepository,
        usuario_repo: UsuarioRepository,
        uow: UnitOfWork,
    ) -> None:
        self._jwt_service = jwt_service
        self._token_repo = token_repo
        self._usuario_repo = usuario_repo
        self._uow = uow

    def executar(self, refresh_token: str) -> TokenDTO:
        payload = self._jwt_service.validar_token(refresh_token)
        if payload.get("type") != "refresh":
            raise TokenInvalidoException(motivo="not_a_refresh_token")
        jti = str(payload["jti"])
        if self._token_repo.esta_revogado(jti):
            raise self._reuso_detectado(payload)
        usuario_id = UUID(str(payload["sub"]))
        usuario = self._usuario_repo.obter_por_id(usuario_id)
        if usuario is None:
            raise CredenciaisInvalidasException(motivo="user_not_found")
        with self._uow:
            # Single-use atomico: o check esta_revogado acima e check-then-act
            # e dois refreshes concorrentes do MESMO token passariam ambos.
            # `revogar` devolve False quando o jti ja foi consumido -- o
            # perdedor da corrida recebe 401 em vez de um segundo par valido.
            if not self._token_repo.revogar(jti):
                raise self._reuso_detectado(payload)
            self._uow.commit()
        access = self._jwt_service.gerar_access_token(
            usuario_id=usuario.id, papel=usuario.papel.value
        )
        refresh = self._jwt_service.gerar_refresh_token(
            usuario_id=usuario.id,
        )
        return TokenDTO(access_token=access, refresh_token=refresh)

    @staticmethod
    def _reuso_detectado(payload: dict[str, object]) -> TokenRevogadoException:
        """401 de um refresh ja usado ou revogado, com o evento que avisa quem opera.

        O cliente repetiu o pedido, o refresh foi revogado no logout ou ele
        vazou e alguem o usa depois do dono: a tabela de revogados e a mesma
        para os tres casos, e o log e o unico sinal, com o usuario e o ``jti``
        reapresentado. A resposta e a de sempre e a descendencia do refresh segue
        valida: a revogacao da familia (RFC 9700, 4.14.2) esta como divida no
        MEMORY.
        """
        _log.warning(
            "refresh_reuse_detected", sub=str(payload["sub"]), jti=str(payload["jti"])
        )
        return TokenRevogadoException()
