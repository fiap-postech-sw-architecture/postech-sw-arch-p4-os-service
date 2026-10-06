from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import pytest

from src.autenticacao.interfaces.dependencies import (
    obter_jwt_service,
    obter_login,
    obter_logout,
    obter_refresh_token,
    obter_registrar,
)

_SEGREDO = "s" * 32  # tamanho minimo do HS256


class TestDependenciesAuth:
    def test_obter_jwt_service_sem_chave_levanta_erro(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("JWT_SECRET", None)
            with pytest.raises(RuntimeError, match="JWT_SECRET"):
                obter_jwt_service()

    def test_obter_jwt_service_com_chave(self) -> None:
        with patch.dict(os.environ, {"JWT_SECRET": _SEGREDO}):
            svc = obter_jwt_service()
            assert svc is not None

    def test_access_de_15_minutos_e_refresh_de_7_dias_por_padrao(self) -> None:
        with patch.dict(os.environ, {"JWT_SECRET": _SEGREDO}, clear=True):
            svc = obter_jwt_service()
        sem_assinatura = {"verify_signature": False}
        access = jwt.decode(
            svc.gerar_access_token(uuid4(), "a@pytstop.dev", "admin"),
            options=sem_assinatura,
        )
        refresh = jwt.decode(svc.gerar_refresh_token(uuid4()), options=sem_assinatura)
        assert access["exp"] - access["iat"] == 15 * 60
        assert refresh["exp"] - refresh["iat"] == 10080 * 60

    def test_obter_registrar(self) -> None:
        session = MagicMock()
        uc = obter_registrar(session)
        assert uc is not None

    def test_obter_login(self) -> None:
        session = MagicMock()
        with patch.dict(os.environ, {"JWT_SECRET": _SEGREDO}):
            uc = obter_login(session)
            assert uc is not None

    def test_obter_logout(self) -> None:
        session = MagicMock()
        with patch.dict(os.environ, {"JWT_SECRET": _SEGREDO}):
            uc = obter_logout(session)
            assert uc is not None

    def test_obter_refresh_token(self) -> None:
        session = MagicMock()
        with patch.dict(os.environ, {"JWT_SECRET": _SEGREDO}):
            uc = obter_refresh_token(session)
            assert uc is not None
