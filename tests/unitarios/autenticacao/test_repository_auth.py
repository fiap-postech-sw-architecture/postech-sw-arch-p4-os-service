from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.autenticacao.infraestrutura.repository import (
    UsuarioSQLAlchemyRepository,
)
from src.autenticacao.infraestrutura.token_revogado_repository import (
    TokenRevogadoSQLAlchemyRepository,
)


class TestRepositoryAuth:
    def test_obter_por_id(self) -> None:
        session = MagicMock()
        session.get.return_value = None
        repo = UsuarioSQLAlchemyRepository(session=session)
        result = repo.obter_por_id(MagicMock())
        assert result is None

    def test_salvar(self) -> None:
        session = MagicMock()
        repo = UsuarioSQLAlchemyRepository(session=session)
        entity = MagicMock()
        repo.salvar(entity)
        session.add.assert_called_once_with(entity)
        session.flush.assert_called_once()

    def test_email_existe_true(self) -> None:
        session = MagicMock()
        session.scalar.return_value = 1
        repo = UsuarioSQLAlchemyRepository(session=session)
        assert repo.email_existe("test@test.com") is True

    def test_email_existe_false(self) -> None:
        session = MagicMock()
        session.scalar.return_value = 0
        repo = UsuarioSQLAlchemyRepository(session=session)
        assert repo.email_existe("test@test.com") is False


class TestTokenRevogadoRepository:
    @pytest.mark.parametrize(
        ("linhas_inseridas", "revogou_agora"),
        [
            pytest.param(1, True, id="revogou-agora"),
            pytest.param(0, False, id="ja-estava-revogado"),
        ],
    )
    def test_revogar_devolve_se_o_insert_gravou_a_linha(
        self, linhas_inseridas: int, revogou_agora: bool
    ) -> None:
        # O ON CONFLICT DO NOTHING deixa o banco decidir (p3 #121 e #167): zero
        # linhas inseridas e "ja estava revogado", sem IntegrityError. O
        # comportamento real, inclusive em corrida, e testado na integracao.
        session = MagicMock()
        conexao = session.connection.return_value
        conexao.execute.return_value.rowcount = linhas_inseridas
        repo = TokenRevogadoSQLAlchemyRepository(session=session)

        assert repo.revogar("some-jti") is revogou_agora
        conexao.execute.assert_called_once()
        session.add.assert_not_called()

    def test_esta_revogado_false(self) -> None:
        session = MagicMock()
        session.scalar.return_value = False
        repo = TokenRevogadoSQLAlchemyRepository(session=session)
        assert repo.esta_revogado("some-jti") is False

    def test_esta_revogado_true(self) -> None:
        session = MagicMock()
        session.scalar.return_value = True
        repo = TokenRevogadoSQLAlchemyRepository(session=session)
        assert repo.esta_revogado("some-jti") is True
