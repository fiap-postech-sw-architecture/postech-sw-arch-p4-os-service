from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import jwt
import pytest
import structlog
import structlog.testing
from fastapi.testclient import TestClient

from src.autenticacao.dominio.papel import Papel
from src.autenticacao.infraestrutura.jwt_service import JWTService
from src.autenticacao.interfaces.middleware import (
    _PERMISSOES,
    exigir_papel,
    obter_usuario_atual,
)
from src.compartilhado.dominio.exceptions import (
    AcessoNegadoException,
    FalhaAutenticacaoException,
)
from src.compartilhado.interfaces.dependencies import obter_session
from src.main import criar_app

# 64 bytes: os tokens de algoritmo trocado sao assinados ate com HS512.
_CHAVE = "test-secret".ljust(64, "x")  # gitleaks:allow
_MIDDLEWARE = "src.autenticacao.interfaces.middleware"
_MOCK_SESSION = MagicMock()


def _jwt_service(expiracao_minutos: int = 30) -> JWTService:
    return JWTService(
        chave_secreta=_CHAVE,
        expiracao_minutos=expiracao_minutos,
        refresh_expiracao_minutos=10080,
    )


# O que o gate devolve para qualquer falha de credencial (ADR-039).
_CODIGO_401 = "NAO_AUTENTICADO"
_MENSAGEM_401 = "Credenciais invalidas"


class _FakeCredentials:
    def __init__(self, token: str) -> None:
        self.credentials = token


def _falha_do_gate(creds: _FakeCredentials | None) -> FalhaAutenticacaoException:
    """Roda o gate esperando a falha de credencial e a devolve."""
    with pytest.raises(FalhaAutenticacaoException) as exc:
        obter_usuario_atual(credentials=creds, session=_MOCK_SESSION)  # type: ignore[arg-type]
    return exc.value


@pytest.fixture(autouse=True)
def _jwt_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", _CHAVE)


class TestObterUsuarioAtual:
    def test_sem_credenciais_retorna_401(self) -> None:
        falha = _falha_do_gate(None)
        assert (falha.codigo, falha.mensagem) == (_CODIGO_401, _MENSAGEM_401)
        assert falha.motivo == "missing_token"

    def test_token_invalido_retorna_401(self) -> None:
        falha = _falha_do_gate(_FakeCredentials(token="invalido"))
        assert (falha.codigo, falha.mensagem) == (_CODIGO_401, _MENSAGEM_401)

    def test_token_expirado_retorna_401(self) -> None:
        svc = _jwt_service(expiracao_minutos=-1)
        token = svc.gerar_access_token(
            usuario_id=uuid4(), email="t@t.com", papel="admin"
        )
        falha = _falha_do_gate(_FakeCredentials(token=token))
        assert (falha.codigo, falha.mensagem) == (_CODIGO_401, _MENSAGEM_401)
        assert falha.motivo == "expired_token"

    def test_token_valido_retorna_payload(self) -> None:
        svc = _jwt_service()
        uid = uuid4()
        token = svc.gerar_access_token(uid, "t@t.com", "admin")
        creds = _FakeCredentials(token=token)
        fake_repo = MagicMock()
        fake_repo.esta_revogado = MagicMock(return_value=False)
        with patch(
            "src.autenticacao.infraestrutura.token_revogado_repository.TokenRevogadoSQLAlchemyRepository",
            return_value=fake_repo,
        ):
            payload = obter_usuario_atual(credentials=creds, session=_MOCK_SESSION)  # type: ignore[arg-type]
        assert payload["sub"] == str(uid)

    def test_refresh_token_rejeitado_como_access(self) -> None:
        """TD-029: refresh token nao passa no gate de acesso (type != access -> 401).

        Espelha o check `type == refresh` do fluxo de refresh; defense-in-depth
        para qualquer rota futura apenas-autenticada (hoje so o RBAC contem).
        """
        svc = _jwt_service()
        token = svc.gerar_refresh_token(uuid4())
        # Sem mock do repo de revogacao: o check `type != access` ocorre ANTES da
        # consulta de revogacao, entao o 401 vem do gate de tipo (nao da revogacao).
        falha = _falha_do_gate(_FakeCredentials(token=token))
        assert falha.motivo == "not_an_access_token"

    def test_payload_sem_jti_retorna_401(self) -> None:
        # Fail-closed (p3 #167): sem jti nao ha como consultar a revogacao --
        # rejeitar em vez de pular a checagem e aceitar o token.
        fake_jwt = MagicMock()
        fake_jwt.validar_token.return_value = {
            "sub": str(uuid4()),
            "type": "access",
        }
        creds = _FakeCredentials(token="token-sem-jti")
        with patch(
            "src.autenticacao.interfaces.middleware.obter_jwt_service",
            return_value=fake_jwt,
        ):
            falha = _falha_do_gate(creds)
        assert (falha.codigo, falha.mensagem) == (_CODIGO_401, _MENSAGEM_401)
        assert falha.motivo == "missing_jti"

    def test_token_revogado_retorna_401(self) -> None:
        svc = _jwt_service()
        uid = uuid4()
        token = svc.gerar_access_token(uid, "t@t.com", "admin")
        payload = svc.validar_token(token)
        jti = str(payload["jti"])
        fake_repo = MagicMock()
        fake_repo.esta_revogado = lambda j: j == jti
        with patch(
            "src.autenticacao.infraestrutura.token_revogado_repository.TokenRevogadoSQLAlchemyRepository",
            return_value=fake_repo,
        ):
            falha = _falha_do_gate(_FakeCredentials(token=token))
        assert (falha.codigo, falha.mensagem) == (_CODIGO_401, _MENSAGEM_401)
        assert falha.motivo == "revoked_token"


def _token(algoritmo: str = "HS256", **claims: object) -> str:
    payload: dict[str, object] = {
        "sub": str(uuid4()),
        "jti": str(uuid4()),
        "type": "access",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(payload, _CHAVE, algorithm=algoritmo)


# (token, motivo no log). `None` = sem header Authorization.
_FALHAS_DE_CREDENCIAL = [
    pytest.param(None, "missing_token", id="sem-token"),
    pytest.param("lixo", "invalid_token", id="malformado"),
    pytest.param(
        _token(exp=datetime.now(UTC) - timedelta(minutes=1)),
        "expired_token",
        id="expirado",
    ),
    pytest.param(_token("HS384"), "invalid_algorithm", id="outro-algoritmo"),
    pytest.param(_token(type="refresh"), "not_an_access_token", id="refresh"),
    pytest.param(_token(jti="revogado"), "revoked_token", id="revogado"),
]


@pytest.fixture
def token_revogado(monkeypatch: pytest.MonkeyPatch) -> None:
    """So o jti ``revogado`` consta na lista de tokens revogados."""
    repo = MagicMock()
    repo.esta_revogado = lambda jti: jti == "revogado"
    monkeypatch.setattr(
        f"{_MIDDLEWARE}.obter_token_revogado_repo", lambda _session: repo
    )


@pytest.mark.usefixtures("token_revogado")
class TestRespostaUniformeDoGate:
    """ADR-039: toda falha de credencial e a mesma excecao; o motivo so no log."""

    @pytest.mark.parametrize(("token", "motivo"), _FALHAS_DE_CREDENCIAL)
    def test_mesma_falha_e_motivo_proprio(self, token: str | None, motivo: str) -> None:
        creds = None if token is None else _FakeCredentials(token=token)

        falha = _falha_do_gate(creds)

        assert (falha.codigo, falha.mensagem) == (_CODIGO_401, _MENSAGEM_401)
        assert falha.motivo == motivo
        assert motivo not in str(falha)


@pytest.mark.usefixtures("token_revogado")
class TestEnvelopeDoGateNaRota:
    """O mesmo gate pela rota real: 401 no envelope de erro, motivo so no log."""

    @staticmethod
    def _chamar(token: str | None) -> tuple[httpx.Response, list[dict[str, Any]]]:
        app = criar_app()
        app.dependency_overrides[obter_session] = lambda: MagicMock()
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        with structlog.testing.capture_logs() as logs:
            resp = TestClient(app).get("/api/v1/ordens-de-servico", headers=headers)
        return resp, logs

    @pytest.fixture(autouse=True)
    def _logger_capturavel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # O logger do handler pode estar fixado por um configurar_logging()
        # anterior (cache_logger_on_first_use): troca por um novo para o
        # capture_logs enxergar o evento.
        monkeypatch.setattr(
            "src.compartilhado.interfaces.error_handler.logger",
            structlog.get_logger(),
        )

    @pytest.mark.parametrize(
        ("token", "motivo"),
        [
            *_FALHAS_DE_CREDENCIAL,
            pytest.param(_token(), "invalid_role_claim", id="sem-papel"),
            pytest.param(
                _token(papel="desconhecido"), "invalid_role_claim", id="papel-invalido"
            ),
            pytest.param(
                _token(papel=42), "invalid_role_claim", id="papel-de-tipo-errado"
            ),
        ],
    )
    def test_401_no_envelope_com_o_motivo_so_no_log(
        self, token: str | None, motivo: str
    ) -> None:
        resp, logs = self._chamar(token)

        assert resp.status_code == 401
        assert resp.json() == {
            "erro": {
                "codigo": _CODIGO_401,
                "mensagem": _MENSAGEM_401,
                "id_requisicao": resp.headers["X-Request-ID"],
            }
        }
        assert resp.headers["WWW-Authenticate"] == "Bearer"
        (evento,) = [e for e in logs if e["event"] == "dominio_excecao_tratada"]
        assert (evento["codigo"], evento["status"], evento["reason"]) == (
            _CODIGO_401,
            401,
            motivo,
        )

    def test_403_no_envelope_para_papel_valido_sem_permissao(self) -> None:
        resp, logs = self._chamar(_token(papel="mecanico"))

        assert resp.status_code == 403
        assert resp.json() == {
            "erro": {
                "codigo": "ACESSO_NEGADO",
                "mensagem": "Papel nao autorizado",
                "id_requisicao": resp.headers["X-Request-ID"],
            }
        }
        assert "WWW-Authenticate" not in resp.headers
        (evento,) = [e for e in logs if e["event"] == "dominio_excecao_tratada"]
        assert (evento["codigo"], evento["status"]) == ("ACESSO_NEGADO", 403)


class TestExigirPapel:
    def test_papel_permitido(self) -> None:
        verificar = exigir_papel("admin")
        result = verificar({"papel": "admin", "sub": "123"})  # type: ignore[operator]
        assert result["papel"] == "admin"

    def test_papel_nao_permitido(self) -> None:
        verificar = exigir_papel("admin")
        with pytest.raises(AcessoNegadoException) as exc:
            verificar({"papel": "atendente", "sub": "123"})  # type: ignore[operator]
        assert (exc.value.codigo, exc.value.mensagem) == (
            "ACESSO_NEGADO",
            "Papel nao autorizado",
        )

    def test_multiplos_papeis_permitidos(self) -> None:
        verificar = exigir_papel("admin", "mecanico")
        result = verificar({"papel": "mecanico", "sub": "123"})  # type: ignore[operator]
        assert result["papel"] == "mecanico"


class TestHierarquiaDePapeis:
    @pytest.mark.parametrize(
        ("papel_usuario", "papel_exigido"),
        [
            pytest.param("admin", "admin", id="admin-acessa-admin"),
            pytest.param("admin", "atendente", id="admin-acessa-atendente"),
            pytest.param("admin", "mecanico", id="admin-acessa-mecanico"),
            pytest.param("atendente", "atendente", id="atendente-acessa-atendente"),
            pytest.param("mecanico", "mecanico", id="mecanico-acessa-mecanico"),
        ],
    )
    def test_papel_aceito(self, papel_usuario: str, papel_exigido: str) -> None:
        verificar = exigir_papel(papel_exigido)
        result = verificar({"papel": papel_usuario, "sub": "u1"})  # type: ignore[operator]
        assert result["papel"] == papel_usuario

    @pytest.mark.parametrize(
        ("papel_usuario", "papel_exigido"),
        [
            pytest.param("atendente", "admin", id="atendente-nega-admin"),
            pytest.param("atendente", "mecanico", id="atendente-nega-mecanico"),
            pytest.param("mecanico", "admin", id="mecanico-nega-admin"),
            pytest.param("mecanico", "atendente", id="mecanico-nega-atendente"),
        ],
    )
    def test_papel_nao_herda_para_cima_ou_lateral(
        self, papel_usuario: str, papel_exigido: str
    ) -> None:
        verificar = exigir_papel(papel_exigido)
        with pytest.raises(AcessoNegadoException):
            verificar({"papel": papel_usuario, "sub": "u1"})  # type: ignore[operator]


def _afirma_401_por_papel_invalido(claims: dict[str, object]) -> None:
    """Papel ausente, desconhecido ou de tipo errado: a mesma falha do gate."""
    verificar = exigir_papel("admin")
    with pytest.raises(FalhaAutenticacaoException) as exc:
        verificar(claims)  # type: ignore[operator]

    assert (exc.value.codigo, exc.value.mensagem) == (_CODIGO_401, _MENSAGEM_401)
    assert exc.value.motivo == "invalid_role_claim"


class TestEdgeCasesExigirPapel:
    @pytest.mark.parametrize(
        "papel_valor",
        [
            pytest.param("desconhecido", id="papel-desconhecido"),
            pytest.param("cliente", id="papel-fora-do-enum"),
            pytest.param("", id="papel-string-vazia"),
            pytest.param("ADMIN", id="papel-case-incorreto"),
        ],
    )
    def test_papel_string_fora_do_enum_retorna_401(self, papel_valor: str) -> None:
        _afirma_401_por_papel_invalido({"papel": papel_valor, "sub": "u1"})

    @pytest.mark.parametrize(
        "papel_valor",
        [
            pytest.param(None, id="papel-none"),
            pytest.param(42, id="papel-int"),
            pytest.param(["admin"], id="papel-list"),
            pytest.param({"nome": "admin"}, id="papel-dict"),
        ],
    )
    def test_papel_tipo_nao_string_retorna_401(self, papel_valor: object) -> None:
        _afirma_401_por_papel_invalido({"papel": papel_valor, "sub": "u1"})

    def test_payload_sem_papel_retorna_401(self) -> None:
        _afirma_401_por_papel_invalido({"sub": "u1"})

    def test_exigir_papel_sem_argumentos_levanta_value_error(self) -> None:
        with pytest.raises(ValueError, match="requer ao menos um papel"):
            exigir_papel()

    def test_exigir_papel_com_valor_fora_do_enum_levanta_value_error(self) -> None:
        with pytest.raises(ValueError, match="papel invalido"):
            exigir_papel("admin", "desconhecido")


class TestGuardasDePermissoes:
    def test_permissoes_cobrem_todos_os_papeis_do_enum(self) -> None:
        assert set(_PERMISSOES.keys()) == set(Papel)

    def test_permissoes_e_mapping_imutavel(self) -> None:
        assert isinstance(_PERMISSOES, Mapping)
        assert isinstance(_PERMISSOES, MappingProxyType)

    def test_valores_de_permissoes_sao_frozenset(self) -> None:
        for papel, permitidos in _PERMISSOES.items():
            assert isinstance(permitidos, frozenset), (
                f"{papel} tem permitidos mutavel: {type(permitidos).__name__}"
            )

    def test_permissoes_nao_podem_ser_mutadas_em_runtime(self) -> None:
        with pytest.raises(TypeError):
            _PERMISSOES[Papel.ATENDENTE] = frozenset({Papel.ADMIN})  # type: ignore[index]
        with pytest.raises(AttributeError):
            _PERMISSOES[Papel.ATENDENTE].add(Papel.ADMIN)


class TestEnvLimpeza:
    def test_jwt_secret_nao_vaza_entre_testes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="JWT_SECRET nao configurado"):
            obter_usuario_atual(
                credentials=_FakeCredentials(token="qualquer"),
                session=_MOCK_SESSION,
            )
