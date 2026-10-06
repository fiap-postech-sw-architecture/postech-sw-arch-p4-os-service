from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.autenticacao.infraestrutura.jwt_service import (
    carregar_chave_privada,
    kid_da_chave,
)
from src.autenticacao.interfaces.dependencies import (
    KID_DA_CHAVE_DEMO,
    obter_jwt_service,
    obter_login,
    obter_logout,
    obter_refresh_token,
    obter_registrar,
    validar_chave_jwt_no_startup,
)
from tests.chaves_jwt import (
    CHAVE_PEM,
    KID,
    OUTRA_CHAVE,
    OUTRO_KID,
    pem_demo_do_compose,
    pem_demo_do_env_example,
    pem_privado,
    pem_publico,
)


@pytest.fixture
def ambiente(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Chave valida, sem anterior e validades padrao; cada teste muda o que testa."""
    monkeypatch.setenv("JWT_PRIVATE_KEY", CHAVE_PEM)
    for nome in (
        "JWT_PREVIOUS_PUBLIC_KEY",
        "JWT_EXPIRATION_MINUTES",
        "JWT_REFRESH_EXPIRATION_MINUTES",
    ):
        monkeypatch.delenv(nome, raising=False)
    return monkeypatch


class TestObterJwtService:
    def test_sem_chave_e_erro_do_servidor(self, ambiente: pytest.MonkeyPatch) -> None:
        ambiente.delenv("JWT_PRIVATE_KEY")
        with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY nao configurada"):
            obter_jwt_service()

    @pytest.mark.parametrize(
        ("variavel", "valor"),
        [
            pytest.param("JWT_PRIVATE_KEY", "nao e pem", id="privada"),
            pytest.param("JWT_PREVIOUS_PUBLIC_KEY", "nao e pem", id="anterior"),
        ],
    )
    def test_chave_invalida_e_erro_do_servidor(
        self, ambiente: pytest.MonkeyPatch, variavel: str, valor: str
    ) -> None:
        # RuntimeError, nunca ValueError: o handler de ValueError responderia 422.
        ambiente.setenv(variavel, valor)
        with pytest.raises(RuntimeError, match=f"{variavel} invalida"):
            obter_jwt_service()

    def test_access_de_15_minutos_e_refresh_de_7_dias_por_padrao(
        self, ambiente: pytest.MonkeyPatch
    ) -> None:
        svc = obter_jwt_service()
        access = svc.validar_token(svc.gerar_access_token(uuid4(), "admin"))
        refresh = svc.validar_token(svc.gerar_refresh_token(uuid4()))
        assert access["exp"] - access["iat"] == 15 * 60  # type: ignore[operator]
        assert refresh["exp"] - refresh["iat"] == 10080 * 60  # type: ignore[operator]

    def test_uma_instancia_por_configuracao(self, ambiente: pytest.MonkeyPatch) -> None:
        # O parse da chave RSA nao se repete a cada requisicao.
        assert obter_jwt_service() is obter_jwt_service()
        ambiente.setenv("JWT_PREVIOUS_PUBLIC_KEY", pem_publico(OUTRA_CHAVE))
        com_anterior = obter_jwt_service()
        assert [c["kid"] for c in com_anterior.jwks()["keys"]] == [KID, OUTRO_KID]

    def test_assina_com_a_chave_do_ambiente(self, ambiente: pytest.MonkeyPatch) -> None:
        token = obter_jwt_service().gerar_access_token(uuid4(), "admin")
        assert jwt.get_unverified_header(token)["kid"] == KID


class TestFactoriesDosCasosDeUso:
    @pytest.mark.usefixtures("ambiente")
    @pytest.mark.parametrize(
        "factory", [obter_registrar, obter_login, obter_logout, obter_refresh_token]
    )
    def test_monta_o_caso_de_uso(self, factory: object) -> None:
        assert factory(MagicMock()) is not None  # type: ignore[operator]


class TestGuardaDeBootDaChaveJwt:
    @pytest.mark.parametrize("ambiente_app", ["development", "test", "Test"])
    def test_dev_e_test_aceitam_ate_chave_ausente(
        self, ambiente: pytest.MonkeyPatch, ambiente_app: str
    ) -> None:
        ambiente.setenv("ENVIRONMENT", ambiente_app)
        ambiente.delenv("JWT_PRIVATE_KEY")
        validar_chave_jwt_no_startup()

    def test_producao_com_chave_propria_passa(
        self, ambiente: pytest.MonkeyPatch
    ) -> None:
        ambiente.setenv("ENVIRONMENT", "production")
        ambiente.setenv("JWT_PREVIOUS_PUBLIC_KEY", pem_publico(OUTRA_CHAVE))
        validar_chave_jwt_no_startup()

    @pytest.mark.parametrize(
        ("variavel", "valor", "erro"),
        [
            pytest.param("JWT_PRIVATE_KEY", "", "nao configurada", id="ausente"),
            pytest.param("JWT_PRIVATE_KEY", "lixo", "PEM sem senha", id="invalida"),
            pytest.param(
                "JWT_PREVIOUS_PUBLIC_KEY", "lixo", "PREVIOUS", id="anterior-invalida"
            ),
        ],
    )
    def test_producao_sem_chave_utilizavel_aborta(
        self, ambiente: pytest.MonkeyPatch, variavel: str, valor: str, erro: str
    ) -> None:
        ambiente.setenv("ENVIRONMENT", "production")
        ambiente.setenv(variavel, valor)
        with pytest.raises(RuntimeError, match=erro):
            validar_chave_jwt_no_startup()

    def test_producao_com_chave_de_menos_de_2048_bits_aborta(
        self, ambiente: pytest.MonkeyPatch
    ) -> None:
        # Fraca de proposito: o que se testa e a recusa.
        fraca = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505
        ambiente.setenv("ENVIRONMENT", "production")
        ambiente.setenv("JWT_PRIVATE_KEY", pem_privado(fraca))
        with pytest.raises(RuntimeError, match="1024 bits; o minimo e 2048"):
            validar_chave_jwt_no_startup()

    @pytest.mark.parametrize("como", ["atual", "anterior"])
    def test_producao_com_a_chave_de_demonstracao_aborta(
        self, ambiente: pytest.MonkeyPatch, como: str
    ) -> None:
        demo = pem_demo_do_compose()
        ambiente.setenv("ENVIRONMENT", "production")
        if como == "atual":
            ambiente.setenv("JWT_PRIVATE_KEY", demo)
        else:
            ambiente.setenv(
                "JWT_PREVIOUS_PUBLIC_KEY", pem_publico(carregar_chave_privada(demo))
            )
        with pytest.raises(RuntimeError, match="demonstracao"):
            validar_chave_jwt_no_startup()

    def test_chave_de_demo_do_compose_e_do_env_example_e_a_barrada(self) -> None:
        # Drift guard: a guarda compara o kid; os dois arquivos trazem a mesma
        # chave de demonstracao, e e ela que a guarda recusa.
        for pem in (pem_demo_do_compose(), pem_demo_do_env_example()):
            chave = carregar_chave_privada(pem)
            assert kid_da_chave(chave.public_key()) == KID_DA_CHAVE_DEMO
