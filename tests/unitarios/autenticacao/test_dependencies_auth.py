from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.autenticacao.aplicacao.use_cases import (
    Login,
    Logout,
    RefreshToken,
    Registrar,
)
from src.autenticacao.infraestrutura.jwt_service import (
    carregar_chave_privada,
    kid_da_chave,
)
from src.autenticacao.interfaces.dependencies import (
    ACCESS_MAXIMO_MINUTOS,
    KIDS_DAS_CHAVES_DEMO,
    REFRESH_MAXIMO_MINUTOS,
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
    validade_em_segundos,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session


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
        assert validade_em_segundos(access) == 15 * 60
        assert validade_em_segundos(refresh) == 10080 * 60

    def test_uma_instancia_por_configuracao(self, ambiente: pytest.MonkeyPatch) -> None:
        # O parse da chave RSA nao se repete a cada requisicao.
        assert obter_jwt_service() is obter_jwt_service()
        ambiente.setenv("JWT_PREVIOUS_PUBLIC_KEY", pem_publico(OUTRA_CHAVE))
        com_anterior = obter_jwt_service()
        assert [c["kid"] for c in com_anterior.jwks()["keys"]] == [KID, OUTRO_KID]

    def test_assina_com_a_chave_do_ambiente(self, ambiente: pytest.MonkeyPatch) -> None:
        token = obter_jwt_service().gerar_access_token(uuid4(), "admin")
        assert jwt.get_unverified_header(token)["kid"] == KID


class TestConfiguracaoDoJwt:
    """Chave e validade do ambiente: valor ruim e erro do servidor, nunca 4xx."""

    @pytest.mark.parametrize(
        ("nome", "valor"),
        [
            pytest.param("JWT_EXPIRATION_MINUTES", "abc", id="access-texto"),
            pytest.param("JWT_EXPIRATION_MINUTES", "", id="access-vazio"),
            pytest.param("JWT_EXPIRATION_MINUTES", "0", id="access-zero"),
            pytest.param("JWT_EXPIRATION_MINUTES", "-5", id="access-negativo"),
            pytest.param("JWT_EXPIRATION_MINUTES", "61", id="access-acima-do-teto"),
            pytest.param("JWT_EXPIRATION_MINUTES", "525600", id="access-de-1-ano"),
            pytest.param(
                "JWT_EXPIRATION_MINUTES",
                "99999999999999",
                id="access-estoura-timedelta",
            ),
            pytest.param("JWT_REFRESH_EXPIRATION_MINUTES", "xx", id="refresh-texto"),
            pytest.param("JWT_REFRESH_EXPIRATION_MINUTES", "0", id="refresh-zero"),
            pytest.param(
                "JWT_REFRESH_EXPIRATION_MINUTES", "43201", id="refresh-acima-do-teto"
            ),
        ],
    )
    def test_validade_fora_dos_limites_e_erro_do_servidor(
        self, ambiente: pytest.MonkeyPatch, nome: str, valor: str
    ) -> None:
        # RuntimeError, nunca ValueError: o handler de ValueError responderia 422
        # em todo login e a validade de 1 ano passaria pela guarda de boot.
        ambiente.setenv(nome, valor)
        with pytest.raises(RuntimeError, match=nome):
            obter_jwt_service()

    def test_validade_nos_tetos_vale(self, ambiente: pytest.MonkeyPatch) -> None:
        ambiente.setenv("JWT_EXPIRATION_MINUTES", str(ACCESS_MAXIMO_MINUTOS))
        ambiente.setenv("JWT_REFRESH_EXPIRATION_MINUTES", str(REFRESH_MAXIMO_MINUTOS))
        svc = obter_jwt_service()

        access = svc.validar_token(svc.gerar_access_token(uuid4(), "admin"))
        refresh = svc.validar_token(svc.gerar_refresh_token(uuid4()))

        assert validade_em_segundos(access) == ACCESS_MAXIMO_MINUTOS * 60
        assert validade_em_segundos(refresh) == REFRESH_MAXIMO_MINUTOS * 60

    @pytest.mark.parametrize(
        "borda",
        [
            pytest.param("\n", id="so-quebra-de-linha"),
            pytest.param(" ", id="so-espaco"),
            pytest.param("\r\n", id="crlf"),
        ],
    )
    def test_anterior_so_com_espaco_e_como_sem_anterior(
        self, ambiente: pytest.MonkeyPatch, borda: str
    ) -> None:
        # Um Secret criado com `echo` guarda uma quebra de linha: nao aborta o boot.
        ambiente.setenv("JWT_PREVIOUS_PUBLIC_KEY", borda)
        assert [c["kid"] for c in obter_jwt_service().jwks()["keys"]] == [KID]


class TestFactoriesDosCasosDeUso:
    @pytest.mark.usefixtures("ambiente")
    @pytest.mark.parametrize(
        ("factory", "caso_de_uso"),
        [
            pytest.param(obter_registrar, Registrar, id="registrar"),
            pytest.param(obter_login, Login, id="login"),
            pytest.param(obter_logout, Logout, id="logout"),
            pytest.param(obter_refresh_token, RefreshToken, id="refresh"),
        ],
    )
    def test_monta_o_caso_de_uso(
        self, factory: Callable[[Session], object], caso_de_uso: type
    ) -> None:
        assert isinstance(factory(MagicMock()), caso_de_uso)


class TestGuardaDeBootDaChaveJwt:
    @pytest.mark.parametrize(
        "ambiente_app",
        [
            pytest.param("development", id="development"),
            pytest.param("test", id="test"),
            pytest.param("Test", id="test-em-caixa-mista"),
        ],
    )
    def test_dev_e_test_aceitam_ate_chave_ausente(
        self, ambiente: pytest.MonkeyPatch, ambiente_app: str
    ) -> None:
        ambiente.setenv("ENVIRONMENT", ambiente_app)
        ambiente.delenv("JWT_PRIVATE_KEY")
        validar_chave_jwt_no_startup()

    def test_environment_ausente_vale_development(
        self, ambiente: pytest.MonkeyPatch
    ) -> None:
        # A imagem fixa ENVIRONMENT=production; sem a variavel (processo fora da
        # imagem) o servico se comporta como development, e a guarda nao age.
        ambiente.delenv("ENVIRONMENT", raising=False)
        ambiente.delenv("JWT_PRIVATE_KEY")
        validar_chave_jwt_no_startup()

    @pytest.mark.parametrize(
        "ambiente_app",
        [
            pytest.param("production", id="production"),
            pytest.param("Production", id="production-em-caixa-mista"),
            pytest.param("staging", id="staging"),
            pytest.param("prod", id="prod"),
            pytest.param("homolog", id="homolog"),
            pytest.param("dev", id="dev"),
            pytest.param("local", id="local"),
            pytest.param("", id="vazio"),
        ],
    )
    def test_ambiente_fora_de_development_e_test_barra_a_chave_de_demo(
        self, ambiente: pytest.MonkeyPatch, ambiente_app: str
    ) -> None:
        # A chave de demonstracao e publica: um staging com ela aceitaria token
        # forjado por qualquer um. So development e test a dispensam.
        ambiente.setenv("JWT_PRIVATE_KEY", pem_demo_do_compose())
        ambiente.setenv("ENVIRONMENT", ambiente_app)
        with pytest.raises(RuntimeError, match="demonstracao"):
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

    @pytest.mark.parametrize(
        ("nome", "valor"),
        [
            pytest.param("JWT_EXPIRATION_MINUTES", "525600", id="access-de-1-ano"),
            pytest.param("JWT_EXPIRATION_MINUTES", "trinta", id="access-texto"),
            pytest.param("JWT_REFRESH_EXPIRATION_MINUTES", "-5", id="refresh-negativo"),
        ],
    )
    def test_producao_com_validade_fora_dos_limites_aborta(
        self, ambiente: pytest.MonkeyPatch, nome: str, valor: str
    ) -> None:
        ambiente.setenv("ENVIRONMENT", "production")
        ambiente.setenv(nome, valor)
        with pytest.raises(RuntimeError, match=nome):
            validar_chave_jwt_no_startup()

    def test_producao_com_validade_padrao_passa(
        self, ambiente: pytest.MonkeyPatch
    ) -> None:
        ambiente.setenv("ENVIRONMENT", "production")
        ambiente.setenv("JWT_EXPIRATION_MINUTES", "15")
        ambiente.setenv("JWT_REFRESH_EXPIRATION_MINUTES", "10080")
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

    @pytest.mark.parametrize(
        "como",
        [
            pytest.param("atual", id="como-atual"),
            pytest.param("anterior", id="como-anterior"),
        ],
    )
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
            assert kid_da_chave(chave.public_key()) in KIDS_DAS_CHAVES_DEMO
