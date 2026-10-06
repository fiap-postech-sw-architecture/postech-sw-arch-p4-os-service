"""Security-focused tests for the OS Service.

Covers JWT security (algorithm enforcement, expiration, revocation),
password hashing (bcrypt), security headers (CSP, HSTS, X-Frame-Options),
CORS configuration validation, PII scrubbing, input validation (Pydantic
extra=forbid), and RBAC route-level access control with role verification.

SQL injection and XSS are not tested explicitly because the stack (SQLAlchemy
ORM parametrized queries + JSON-only API with CSP: default-src 'none')
mitigates these by design.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.testclient import TestClient

from src.autenticacao.dominio.exceptions import (
    TokenExpiradoException,
    TokenInvalidoException,
)
from src.autenticacao.dominio.papel import Papel
from src.autenticacao.infraestrutura.password_hasher import (
    hash_senha,
    verificar_senha,
)
from src.autenticacao.interfaces.middleware import (
    exigir_papel,
    obter_usuario_atual,
)
from src.autenticacao.interfaces.schemas import (
    LoginRequest,
    RefreshRequest,
    RegistrarRequest,
)
from src.cliente_veiculo.interfaces.schemas import (
    AdicionarVeiculoRequest,
    AtualizarClienteRequest,
    ConsentimentoRequest,
    CriarClienteRequest,
)
from src.compartilhado.dominio.exceptions import (
    AcessoNegadoException,
    FalhaAutenticacaoException,
)
from src.compartilhado.infraestrutura.encryption import EncryptionService
from src.compartilhado.infraestrutura.logging import scrub_pii
from src.compartilhado.interfaces.middleware import (
    _SENHA_DO_BANCO_DEMO,
    SecurityHeadersMiddleware,
    configurar_cors,
    validar_segredos_no_startup,
)
from src.ordem_servico.interfaces.schemas import (
    AbrirOrdemRequest,
    AcompanhamentoRequest,
    CancelarOrdemRequest,
)
from tests.chaves_jwt import (
    CHAVE,
    CHAVE_PEM,
    KID,
    OUTRA_CHAVE,
    adulterar,
    claims,
    forjar_hmac_com_a_chave_publica,
    forjar_sem_assinatura,
    jwt_service,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCredentials:
    def __init__(self, token: str) -> None:
        self.credentials = token


_MOCK_SESSION = MagicMock()


def _patch_revocation(revogados: set[str] | None = None):
    """Context manager patch for token revocation check in middleware."""
    revogados = revogados or set()
    fake_repo = MagicMock()
    fake_repo.esta_revogado = lambda jti: jti in revogados
    return patch(
        "src.autenticacao.infraestrutura.token_revogado_repository.TokenRevogadoSQLAlchemyRepository",
        return_value=fake_repo,
    )


# ===========================================================================
# 1. JWT Security Tests
# ===========================================================================


class TestJWTAlgorithmEnforcement:
    """O algoritmo e fixo em RS256: HMAC com a chave publica, outro RSA e none caem."""

    def test_rs256_accepted(self) -> None:
        svc = jwt_service()
        uid = uuid4()
        token = svc.gerar_access_token(uid, "admin")
        assert svc.validar_token(token)["sub"] == str(uid)

    @pytest.mark.parametrize("algoritmo", ["HS256", "HS384", "HS512"])
    def test_hmac_assinado_com_a_chave_publica_rejected(self, algoritmo: str) -> None:
        # Troca de algoritmo: o PEM publico (que esta no JWKS) como segredo HMAC.
        token = forjar_hmac_com_a_chave_publica(algoritmo)
        with pytest.raises(TokenInvalidoException) as exc:
            jwt_service().validar_token(token)
        assert exc.value.motivo == "invalid_algorithm"

    def test_rs512_com_a_chave_certa_rejected(self) -> None:
        token = jwt.encode(claims(), CHAVE, algorithm="RS512", headers={"kid": KID})
        with pytest.raises(TokenInvalidoException) as exc:
            jwt_service().validar_token(token)
        assert exc.value.motivo == "invalid_algorithm"

    def test_none_algorithm_rejected(self) -> None:
        """The 'none' algorithm attack must be blocked."""
        with pytest.raises(TokenInvalidoException):
            jwt_service().validar_token(forjar_sem_assinatura())


class TestJWTExpiredToken:
    """Expired tokens must be rejected."""

    def test_expired_token_raises_exception(self) -> None:
        svc = jwt_service(expiracao_minutos=-1)
        token = svc.gerar_access_token(uuid4(), "admin")
        with pytest.raises(TokenExpiradoException):
            svc.validar_token(token)


class TestJWTTamperedPayload:
    """Tokens whose signature no longer matches must be rejected."""

    def test_tampered_payload_rejected(self) -> None:
        token = jwt_service().gerar_access_token(uuid4(), "atendente")
        with pytest.raises(TokenInvalidoException):
            jwt_service().validar_token(adulterar(token, papel="admin"))

    def test_wrong_key_rejected(self) -> None:
        token = jwt_service(chave=OUTRA_CHAVE).gerar_access_token(uuid4(), "admin")
        with pytest.raises(TokenInvalidoException):
            jwt_service().validar_token(token)


class TestJWTTokenRevocation:
    """Token revocation must prevent access with a revoked token."""

    @pytest.fixture(autouse=True)
    def _set_jwt_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JWT_PRIVATE_KEY", CHAVE_PEM)
        monkeypatch.delenv("JWT_PREVIOUS_PUBLIC_KEY", raising=False)

    def test_revoked_token_returns_401(self) -> None:
        svc = jwt_service()
        uid = uuid4()
        token = svc.gerar_access_token(uid, "admin")
        payload = svc.validar_token(token)
        jti = str(payload["jti"])
        with _patch_revocation(revogados={jti}):
            creds = _FakeCredentials(token=token)
            with pytest.raises(FalhaAutenticacaoException) as exc:
                obter_usuario_atual(credentials=creds, session=_MOCK_SESSION)  # type: ignore[arg-type]
            # ADR-039: a mesma mensagem de qualquer outra falha de credencial.
            assert exc.value.mensagem == "Credencial ausente, invalida ou expirada"
            assert exc.value.motivo == "revoked_token"

    def test_non_revoked_token_accepted(self) -> None:
        svc = jwt_service()
        uid = uuid4()
        token = svc.gerar_access_token(uid, "admin")
        with _patch_revocation(revogados=set()):
            creds = _FakeCredentials(token=token)
            payload = obter_usuario_atual(credentials=creds, session=_MOCK_SESSION)  # type: ignore[arg-type]
            assert payload["sub"] == str(uid)


# ===========================================================================
# 2. Password Security Tests
# ===========================================================================


class TestPasswordSecurity:
    """Password hashing must enforce length constraints and produce secure hashes."""

    def test_minimum_12_chars_enforced(self) -> None:
        with pytest.raises(ValueError, match="pelo menos 12"):
            hash_senha("short11char")

    def test_exactly_11_chars_rejected(self) -> None:
        with pytest.raises(ValueError, match="pelo menos 12"):
            hash_senha("a" * 11)

    def test_exactly_12_chars_accepted(self) -> None:
        hashed = hash_senha("a" * 12)
        assert hashed is not None
        assert len(hashed) > 0

    def test_max_128_chars_enforced(self) -> None:
        with pytest.raises(ValueError, match="no maximo 128"):
            hash_senha("a" * 129)

    def test_exactly_72_bytes_accepted(self) -> None:
        """Bcrypt truncates at 72 bytes; passwords up to 72 chars must work."""
        hashed = hash_senha("a" * 72)
        assert hashed is not None
        assert len(hashed) > 0

    def test_empty_password_rejected(self) -> None:
        with pytest.raises(ValueError, match="pelo menos 12"):
            hash_senha("")

    def test_same_password_produces_different_hashes(self) -> None:
        """Bcrypt must use random salt, producing different hashes each time."""
        password = "secure-password-12chars"
        h1 = hash_senha(password)
        h2 = hash_senha(password)
        assert h1 != h2

    def test_verify_correct_password(self) -> None:
        password = "secure-password-12chars"
        hashed = hash_senha(password)
        assert verificar_senha(password, hashed) is True

    def test_verify_incorrect_password(self) -> None:
        password = "secure-password-12chars"
        hashed = hash_senha(password)
        assert verificar_senha("wrong-password-12chars", hashed) is False

    def test_hash_is_not_plaintext(self) -> None:
        password = "secure-password-12chars"
        hashed = hash_senha(password)
        assert password not in hashed


# ===========================================================================
# 3. Security Headers Tests
# ===========================================================================


def _criar_app_com_headers() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/test")
    def test_endpoint() -> dict[str, str]:
        return {"ok": "true"}

    return app


class TestSecurityHeaders:
    """SecurityHeadersMiddleware must set all required security headers."""

    def test_x_content_type_options(self) -> None:
        client = TestClient(_criar_app_com_headers())
        response = client.get("/test")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self) -> None:
        client = TestClient(_criar_app_com_headers())
        response = client.get("/test")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_strict_transport_security(self) -> None:
        client = TestClient(_criar_app_com_headers())
        response = client.get("/test")
        hsts = response.headers.get("Strict-Transport-Security", "")
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    def test_cache_control_no_store(self) -> None:
        client = TestClient(_criar_app_com_headers())
        response = client.get("/test")
        assert response.headers.get("Cache-Control") == "no-store"

    def test_content_security_policy(self) -> None:
        client = TestClient(_criar_app_com_headers())
        response = client.get("/test")
        assert response.headers.get("Content-Security-Policy") == "default-src 'none'"

    def test_x_request_id_present(self) -> None:
        client = TestClient(_criar_app_com_headers())
        response = client.get("/test")
        request_id = response.headers.get("X-Request-ID", "")
        assert len(request_id) > 0

    def test_all_security_headers_in_single_response(self) -> None:
        client = TestClient(_criar_app_com_headers())
        response = client.get("/test")
        required_headers = [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Strict-Transport-Security",
            "Cache-Control",
            "Content-Security-Policy",
            "X-Request-ID",
        ]
        for header in required_headers:
            assert header in response.headers, f"Missing header: {header}"


# ===========================================================================
# 4. CORS Configuration Tests
# ===========================================================================


class TestCORSConfiguration:
    """CORS origins must be read from environment and parsed correctly."""

    @staticmethod
    def _get_cors_origins(app: FastAPI) -> list[str]:
        for m in getattr(app, "user_middleware", []):
            if getattr(m, "cls", type(m)).__name__ == "CORSMiddleware":
                return list(m.kwargs.get("allow_origins", []))
        return []

    @staticmethod
    def _has_cors_middleware(app: FastAPI) -> bool:
        return any(
            getattr(m, "cls", type(m)).__name__ == "CORSMiddleware"
            for m in getattr(app, "user_middleware", [])
        )

    def test_cors_origins_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "CORS_ORIGINS", "http://localhost:3000,https://app.example.com"
        )
        app = FastAPI()
        configurar_cors(app)
        assert self._has_cors_middleware(app)
        origins = self._get_cors_origins(app)
        assert "http://localhost:3000" in origins
        assert "https://app.example.com" in origins

    def test_empty_cors_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ORIGINS", "")
        app = FastAPI()
        configurar_cors(app)
        assert self._has_cors_middleware(app)

    def test_cors_origins_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        app = FastAPI()
        configurar_cors(app)
        assert self._has_cors_middleware(app)

    def test_cors_origins_with_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "CORS_ORIGINS", " http://localhost:3000 , https://app.example.com "
        )
        app = FastAPI()
        configurar_cors(app)
        assert self._has_cors_middleware(app)
        origins = self._get_cors_origins(app)
        assert "http://localhost:3000" in origins
        assert "https://app.example.com" in origins


# ===========================================================================
# 5. PII Protection Tests
# ===========================================================================


class TestPIIScrubbing:
    """PII must be scrubbed from log entries to prevent data leakage."""

    def test_cpf_with_formatting_scrubbed(self) -> None:
        event: dict[str, object] = {"event": "CPF do cliente: 123.456.789-00"}
        result = scrub_pii(None, "info", event)
        assert "123.456.789-00" not in str(result["event"])
        assert "***" in str(result["event"])

    def test_cpf_without_formatting_scrubbed(self) -> None:
        event: dict[str, object] = {"event": "CPF: 12345678900"}
        result = scrub_pii(None, "info", event)
        assert "12345678900" not in str(result["event"])

    def test_cnpj_with_formatting_scrubbed(self) -> None:
        event: dict[str, object] = {"event": "CNPJ: 12.345.678/0001-90"}
        result = scrub_pii(None, "info", event)
        assert "12.345.678/0001-90" not in str(result["event"])

    def test_cnpj_without_formatting_scrubbed(self) -> None:
        event: dict[str, object] = {"event": "CNPJ: 12345678000190"}
        result = scrub_pii(None, "info", event)
        assert "12345678000190" not in str(result["event"])

    def test_email_partially_masked(self) -> None:
        event: dict[str, object] = {"event": "Email: user@example.com"}
        result = scrub_pii(None, "info", event)
        text = str(result["event"])
        assert "user@example.com" not in text
        assert "u***@example.com" in text

    def test_long_email_partially_masked(self) -> None:
        event: dict[str, object] = {"event": "Contact: longname@domain.org"}
        result = scrub_pii(None, "info", event)
        text = str(result["event"])
        assert "longname@domain.org" not in text
        assert "l***@domain.org" in text

    def test_multiple_pii_in_same_field(self) -> None:
        event: dict[str, object] = {"event": "CPF 123.456.789-00, Email admin@test.com"}
        result = scrub_pii(None, "info", event)
        text = str(result["event"])
        assert "123.456.789-00" not in text
        assert "admin@test.com" not in text

    def test_non_string_values_unchanged(self) -> None:
        event: dict[str, object] = {"count": 42, "flag": True}
        result = scrub_pii(None, "info", event)
        assert result["count"] == 42
        assert result["flag"] is True


class TestEncryptionServiceSecurity:
    """Encryption service must encrypt and decrypt PII correctly."""

    def test_encrypt_produces_ciphertext(self) -> None:
        enc = EncryptionService()
        plaintext = "123.456.789-00"
        ciphertext = enc.encrypt(plaintext)
        assert ciphertext != plaintext

    def test_decrypt_recovers_plaintext(self) -> None:
        enc = EncryptionService()
        plaintext = "sensitive-document-data"
        ciphertext = enc.encrypt(plaintext)
        assert enc.decrypt(ciphertext) == plaintext

    def test_encrypt_produces_different_ciphertext_each_time(self) -> None:
        """Fernet uses random IV, so same plaintext yields different ciphertext."""
        enc = EncryptionService()
        plaintext = "test-data"
        c1 = enc.encrypt(plaintext)
        c2 = enc.encrypt(plaintext)
        assert c1 != c2

    def test_decrypt_invalid_ciphertext_returns_original(self) -> None:
        enc = EncryptionService()
        raw = "not-encrypted-text"
        assert enc.decrypt(raw) == raw


# ===========================================================================
# 6. Input Validation Tests (Pydantic)
# ===========================================================================


class TestLoginRequestValidation:
    """LoginRequest must reject extra fields (mass assignment prevention)."""

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError) as exc:
            LoginRequest(
                email="a@b.com",
                senha="password12chars",
                is_admin=True,  # type: ignore[call-arg]
            )
        assert "extra" in str(exc.value).lower()

    def test_rejects_short_password(self) -> None:
        with pytest.raises(ValidationError):
            LoginRequest(email="a@b.com", senha="short")

    def test_accepts_valid_request(self) -> None:
        req = LoginRequest(email="a@b.com", senha="password12chars")
        assert req.email == "a@b.com"


class TestRegistrarRequestValidation:
    """RegistrarRequest exige email, senha (>=12), papel interno; rejeita extras.

    Regressao do p3 #84: papel e OBRIGATORIO (sem default ADMIN silencioso) e
    limitado aos valores internos: admin/mecanico/atendente.
    """

    def test_rejects_password_too_short(self) -> None:
        with pytest.raises(ValidationError):
            RegistrarRequest(email="a@b.com", senha="short11char", papel=Papel.ADMIN)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError) as exc:
            RegistrarRequest(
                email="a@b.com",
                senha="password12chars",
                papel=Papel.ADMIN,
                superuser=True,  # type: ignore[call-arg]
            )
        assert "extra" in str(exc.value).lower()

    def test_papel_obrigatorio(self) -> None:
        # Omitir papel deve falhar (422), nunca cair num default ADMIN.
        with pytest.raises(ValidationError) as exc:
            RegistrarRequest(email="a@b.com", senha="password12chars")  # type: ignore[call-arg]
        erro = str(exc.value).lower()
        assert "papel" in erro
        assert "required" in erro or "missing" in erro

    def test_rejects_papel_invalido(self) -> None:
        with pytest.raises(ValidationError):
            RegistrarRequest(
                email="a@b.com",
                senha="password12chars",
                papel="superuser",  # type: ignore[arg-type]
            )

    def test_rejects_papel_cliente(self) -> None:
        # O papel de cliente da fase 3 (token da Lambda de CPF) nao existe aqui.
        with pytest.raises(ValidationError):
            RegistrarRequest(
                email="a@b.com",
                senha="password12chars",
                papel="cliente",  # type: ignore[arg-type]
            )

    def test_schema_anuncia_somente_papeis_internos(self) -> None:
        schema = RegistrarRequest.model_json_schema()
        assert set(schema["$defs"]["Papel"]["enum"]) == {
            "admin",
            "mecanico",
            "atendente",
        }

    @pytest.mark.parametrize("papel", [Papel.ADMIN, Papel.MECANICO, Papel.ATENDENTE])
    def test_accepts_cada_papel_valido(self, papel: Papel) -> None:
        req = RegistrarRequest(email="a@b.com", senha="password12chars", papel=papel)
        assert req.email == "a@b.com"
        assert req.papel is papel


class TestRefreshRequestValidation:
    """RefreshRequest must reject extra fields."""

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError) as exc:
            RefreshRequest(
                refresh_token="some-token",
                admin=True,  # type: ignore[call-arg]
            )
        assert "extra" in str(exc.value).lower()


class TestAllSchemasExtraForbid:
    """All request schemas across all bounded contexts must have extra='forbid'."""

    def test_autenticacao_schemas_forbid_extra(self) -> None:
        schemas = [LoginRequest, RegistrarRequest, RefreshRequest]
        for schema_cls in schemas:
            config = schema_cls.model_config
            assert config.get("extra") == "forbid", (
                f"{schema_cls.__name__} does not have extra='forbid'"
            )

    def test_cliente_veiculo_schemas_forbid_extra(self) -> None:
        schemas = [
            CriarClienteRequest,
            AtualizarClienteRequest,
            AdicionarVeiculoRequest,
            ConsentimentoRequest,
        ]
        for schema_cls in schemas:
            config = schema_cls.model_config
            assert config.get("extra") == "forbid", (
                f"{schema_cls.__name__} does not have extra='forbid'"
            )

    def test_ordem_servico_schemas_forbid_extra(self) -> None:
        schemas = [
            AbrirOrdemRequest,
            CancelarOrdemRequest,
            AcompanhamentoRequest,
        ]
        for schema_cls in schemas:
            config = schema_cls.model_config
            assert config.get("extra") == "forbid", (
                f"{schema_cls.__name__} does not have extra='forbid'"
            )


# ===========================================================================
# 7. Authorization Tests
# ===========================================================================


class TestLGPDEndpointsRequireAuth:
    """LGPD endpoints (dados-pessoais, consentimento) must require authentication.

    We verify this by checking that the route definitions include a dependency
    on exigir_papel (via its inner ``verificar`` function), which itself calls
    obter_usuario_atual.
    """

    @staticmethod
    def _route_has_auth_dependency(route: object) -> bool:
        """Check if a route has an exigir_papel-generated dependency."""
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            return False
        for dep in dependant.dependencies:
            call = getattr(dep, "call", None)
            if call is not None and "verificar" in getattr(call, "__name__", ""):
                return True
        return False

    def test_dados_pessoais_route_requires_auth(self) -> None:
        from src.cliente_veiculo.interfaces.router import router

        dados_pessoais_routes = [
            r
            for r in router.routes
            if hasattr(r, "path") and "dados-pessoais" in r.path  # type: ignore[union-attr]
        ]
        assert len(dados_pessoais_routes) > 0, "No dados-pessoais routes found"
        for route in dados_pessoais_routes:
            assert self._route_has_auth_dependency(route), (
                f"Route {route.path} has no auth dependency"  # type: ignore[union-attr]
            )

    def test_consentimento_route_requires_auth(self) -> None:
        from src.cliente_veiculo.interfaces.router import router

        consentimento_routes = [
            r
            for r in router.routes
            if hasattr(r, "path") and "consentimento" in r.path  # type: ignore[union-attr]
        ]
        assert len(consentimento_routes) > 0, "No consentimento routes found"
        for route in consentimento_routes:
            assert self._route_has_auth_dependency(route), (
                f"Route {route.path} has no auth dependency"  # type: ignore[union-attr]
            )

    def test_exportar_dados_route_requires_auth(self) -> None:
        from src.cliente_veiculo.interfaces.router import router

        export_routes = [
            r
            for r in router.routes
            if hasattr(r, "path") and "exportar" in r.path  # type: ignore[union-attr]
        ]
        assert len(export_routes) > 0, "No export routes found"
        for route in export_routes:
            assert self._route_has_auth_dependency(route), (
                f"Route {route.path} has no auth dependency"  # type: ignore[union-attr]
            )


# ===========================================================================
# 8. RBAC Tests
# ===========================================================================


class TestRBACExigirPapel:
    """exigir_papel must accept authorized roles and reject unauthorized ones."""

    def test_single_role_accepted(self) -> None:
        verificar = exigir_papel("admin")
        result = verificar({"papel": "admin", "sub": "u1"})  # type: ignore[operator]
        assert result["papel"] == "admin"

    def test_single_role_rejected(self) -> None:
        verificar = exigir_papel("admin")
        with pytest.raises(AcessoNegadoException) as exc:
            verificar({"papel": "mecanico", "sub": "u1"})  # type: ignore[operator]
        assert exc.value.codigo == "ACESSO_NEGADO"
        assert "nao autorizado" in exc.value.mensagem.lower()

    def test_multiple_roles_first_accepted(self) -> None:
        verificar = exigir_papel("admin", "mecanico")
        result = verificar({"papel": "admin", "sub": "u1"})  # type: ignore[operator]
        assert result["papel"] == "admin"

    def test_multiple_roles_second_accepted(self) -> None:
        verificar = exigir_papel("admin", "mecanico")
        result = verificar({"papel": "mecanico", "sub": "u1"})  # type: ignore[operator]
        assert result["papel"] == "mecanico"

    def test_multiple_roles_unlisted_rejected(self) -> None:
        verificar = exigir_papel("admin", "mecanico")
        with pytest.raises(AcessoNegadoException):
            verificar({"papel": "atendente", "sub": "u1"})  # type: ignore[operator]

    def test_empty_papel_is_a_credential_failure(self) -> None:
        verificar = exigir_papel("admin")
        with pytest.raises(FalhaAutenticacaoException) as exc:
            verificar({"papel": "", "sub": "u1"})  # type: ignore[operator]
        assert exc.value.mensagem == "Credencial ausente, invalida ou expirada"

    def test_missing_papel_key_is_a_credential_failure(self) -> None:
        verificar = exigir_papel("admin")
        with pytest.raises(FalhaAutenticacaoException) as exc:
            verificar({"sub": "u1"})  # type: ignore[operator]
        assert exc.value.mensagem == "Credencial ausente, invalida ou expirada"

    def test_no_credentials_returns_401(self) -> None:
        """obter_usuario_atual with None credentials raises 401."""
        with pytest.raises(FalhaAutenticacaoException) as exc:
            obter_usuario_atual(credentials=None, session=_MOCK_SESSION)
        assert exc.value.codigo == "NAO_AUTENTICADO"


class TestRBACRouteDeclarations:
    """Verify route-level role assignments match security requirements."""

    @staticmethod
    def _get_route_papeis(route: object) -> set[str]:
        """Extract the papeis from the exigir_papel closure on a route."""
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            return set()
        for dep in dependant.dependencies:
            call = getattr(dep, "call", None)
            if call is not None and "verificar" in getattr(call, "__name__", ""):
                closure = getattr(call, "__closure__", None)
                if closure:
                    for cell in closure:
                        val = cell.cell_contents
                        if isinstance(val, tuple | frozenset | set) and val:
                            try:
                                return {str(v) for v in val}
                            except TypeError:
                                continue
        return set()

    @staticmethod
    def _route_has_auth_dependency(route: object) -> bool:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            return False
        for dep in dependant.dependencies:
            call = getattr(dep, "call", None)
            if call is not None and "verificar" in getattr(call, "__name__", ""):
                return True
        return False

    @staticmethod
    def _rota_os(metodo: str, sufixo: str) -> object:
        from src.ordem_servico.interfaces.router import router

        rotas = [
            r
            for r in router.routes
            if r.path == router.prefix + sufixo  # type: ignore[attr-defined]
            and metodo in r.methods  # type: ignore[attr-defined]
        ]
        assert len(rotas) == 1, f"{metodo} {sufixo}"
        return rotas[0]

    @pytest.mark.parametrize(
        ("metodo", "sufixo"),
        [
            ("POST", ""),
            ("GET", ""),
            ("GET", "/{ordem_id}"),
            ("GET", "/{ordem_id}/historico"),
            ("POST", "/{ordem_id}/cancelamento"),
            ("POST", "/{ordem_id}/entrega"),
        ],
    )
    def test_toda_rota_de_os_e_so_do_atendente(self, metodo: str, sufixo: str) -> None:
        """ADR-039: o mecanico nao tem rota no OS Service (admin herda atendente)."""
        rota = self._rota_os(metodo, sufixo)
        assert self._route_has_auth_dependency(rota)
        assert self._get_route_papeis(rota) == {"atendente"}

    def test_registrar_requires_admin(self) -> None:
        from src.autenticacao.interfaces.router import router

        registrar_routes = [
            r
            for r in router.routes
            if hasattr(r, "path") and r.path.endswith("/registrar")  # type: ignore[union-attr]
        ]
        assert len(registrar_routes) == 1
        assert self._route_has_auth_dependency(registrar_routes[0])
        papeis = self._get_route_papeis(registrar_routes[0])
        assert papeis == {"admin"}


# ===========================================================================
# 9. Startup Secret Validation Tests (p3 #74)
# ===========================================================================


# Segredo forte hipotetico: >= 32 bytes e fora do denylist de demo. Usado nos
# casos que devem PASSAR para provar que a guarda so barra fraco/demo, nao tudo.
_SEGREDO_FORTE = "x9Qx7!aZ_kP3#wL2$mN8vR1tY6uB4eC0sD-producao-only"
# Literais de demonstracao publicos no git (docker-compose.yml): a guarda os
# rejeita em producao. `gitleaks:allow` -- base64 de 44 chars e senha de demo.
_DEMO_ENC_KEY = "Chqh4o4QURACWBUSdtXjAxhOQt6HhxAfEg9rtvsABKU="  # gitleaks:allow
_DEMO_ADMIN_PASSWORD = "admin-demo-os-2026"  # gitleaks:allow


# Chave Fernet valida gerada a cada execucao (a guarda instancia o Fernet).
_CHAVE_FERNET = Fernet.generate_key().decode()


def _set_segredos_validos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Define segredos fortes/nao-demo; cada teste corrompe so o que testa."""
    monkeypatch.setenv("ENCRYPTION_KEY", _CHAVE_FERNET)
    monkeypatch.delenv("DATABASE_URL", raising=False)


class TestValidarSegredosNoStartupProducao:
    """Em producao a guarda aborta o boot para segredo fraco ou de demo."""

    def test_encryption_key_demo_em_producao_levanta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        monkeypatch.setenv("ENCRYPTION_KEY", _DEMO_ENC_KEY)
        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            validar_segredos_no_startup()

    def test_literais_de_demo_sao_os_do_docker_compose(self) -> None:
        # Drift guard: o denylist precisa acompanhar o compose de dev.
        raiz = Path(__file__).resolve().parents[2]
        compose = (raiz / "docker-compose.yml").read_text()
        for literal in (_DEMO_ENC_KEY, _DEMO_ADMIN_PASSWORD):
            assert literal in compose
        assert f"POSTGRES_PASSWORD: {_SENHA_DO_BANCO_DEMO}" in compose

    def test_env_example_usa_os_mesmos_literais_de_demo(self) -> None:
        # O .env.example (rodar fora do compose) repete os valores do compose:
        # o denylist tambem os cobre.
        raiz = Path(__file__).resolve().parents[2]
        exemplo = (raiz / ".env.example").read_text()
        for literal in (_DEMO_ENC_KEY, _DEMO_ADMIN_PASSWORD):
            assert f"={literal}  # gitleaks:allow" in exemplo
        assert f"POSTGRES_PASSWORD={_SENHA_DO_BANCO_DEMO}  #" in exemplo

    def test_senha_do_banco_de_demo_em_producao_levanta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        monkeypatch.setenv(
            "DATABASE_URL",
            f"postgresql://pytstop:{_SENHA_DO_BANCO_DEMO}@db:5432/os",
        )
        with pytest.raises(RuntimeError, match=r"DATABASE_URL.*demonstracao"):
            validar_segredos_no_startup()

    def test_senha_do_banco_real_em_producao_passa(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "postgresql://os:s3nh4-real@db:5432/os")
        validar_segredos_no_startup()  # nao deve levantar

    @pytest.mark.parametrize(
        "chave",
        [
            pytest.param("nao-e-fernet", id="texto"),
            pytest.param(_SEGREDO_FORTE + "-enc", id="longa-sem-base64"),
        ],
    )
    def test_encryption_key_malformada_em_producao_levanta(
        self, monkeypatch: pytest.MonkeyPatch, chave: str
    ) -> None:
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        monkeypatch.setenv("ENCRYPTION_KEY", chave)
        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY invalida"):
            validar_segredos_no_startup()

    def test_admin_password_demo_em_producao_levanta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADMIN_PASSWORD = literal demo do docker-compose.yml -> aborta em producao."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        monkeypatch.setenv("ADMIN_PASSWORD", _DEMO_ADMIN_PASSWORD)
        with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
            validar_segredos_no_startup()

    def test_admin_password_forte_em_producao_passa(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADMIN_PASSWORD forte/nao-demo nao e barrada (a guarda so veta o demo)."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        monkeypatch.setenv("ADMIN_PASSWORD", _SEGREDO_FORTE + "-admin")
        validar_segredos_no_startup()  # nao deve levantar

    def test_admin_password_ausente_em_producao_passa(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADMIN_PASSWORD ausente nao e barrada aqui (mantem 'ausente -> ignora').

        O seed do admin so roda quando RUN_SEED_ON_STARTUP=true; a guarda foca em
        NAO deixar o literal publico de demo chegar a producao, nao em exigir a
        variavel sempre.
        """
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        validar_segredos_no_startup()  # nao deve levantar

    def test_segredos_fortes_nao_demo_em_producao_passa(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENVIRONMENT", "production")
        _set_segredos_validos(monkeypatch)
        validar_segredos_no_startup()  # nao deve levantar

    def test_encryption_key_ausente_em_producao_levanta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENCRYPTION_KEY ausente em producao aborta o boot.

        Sem a chave Fernet o app usaria uma chave efemera em memoria -> dados
        cifrados irrecuperaveis apos restart + documento_hash divergente entre
        replicas. O guard fecha esse caminho em producao.
        """
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            validar_segredos_no_startup()


class TestValidarSegredosNoStartupGatePorAmbiente:
    """Fora de producao a guarda e no-op (dev/compose/testes sobem com demo)."""

    @pytest.mark.parametrize("ambiente", ["development", "test"])
    def test_demo_e_curto_em_dev_ou_test_passa(
        self, monkeypatch: pytest.MonkeyPatch, ambiente: str
    ) -> None:
        monkeypatch.setenv("ENVIRONMENT", ambiente)
        monkeypatch.setenv("ENCRYPTION_KEY", _DEMO_ENC_KEY)
        monkeypatch.setenv("ADMIN_PASSWORD", _DEMO_ADMIN_PASSWORD)
        validar_segredos_no_startup()  # gated -> nao levanta

    def test_default_ausente_e_dev_passa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ENVIRONMENT ausente cai no default 'development' -> guarda no-op."""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        validar_segredos_no_startup()  # nao levanta

    def test_case_insensitive_production_levanta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deteccao de ambiente normaliza caixa: 'Production' tambem dispara."""
        monkeypatch.setenv("ENVIRONMENT", "Production")
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            validar_segredos_no_startup()
