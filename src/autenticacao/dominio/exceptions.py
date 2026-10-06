from __future__ import annotations

from src.compartilhado.dominio.exceptions import (
    EntidadeDuplicadaException,
    FalhaAutenticacaoException,
)

# Toda falha de credencial responde 401 com a mesma mensagem
# (``FalhaAutenticacaoException.MENSAGEM``); o ``motivo`` so vai para o log.


class CredenciaisInvalidasException(FalhaAutenticacaoException):
    def __init__(self, motivo: str = "invalid_credentials") -> None:
        super().__init__(motivo=motivo)


class EmailDuplicadoException(EntidadeDuplicadaException):
    def __init__(self, mensagem: str = "Email ja cadastrado") -> None:
        super().__init__(mensagem=mensagem)


class TokenInvalidoException(FalhaAutenticacaoException):
    def __init__(self, motivo: str = "invalid_token") -> None:
        super().__init__(motivo=motivo)


class TokenExpiradoException(FalhaAutenticacaoException):
    def __init__(self, motivo: str = "expired_token") -> None:
        super().__init__(motivo=motivo)


class TokenRevogadoException(FalhaAutenticacaoException):
    def __init__(self, motivo: str = "revoked_token") -> None:
        super().__init__(motivo=motivo)
