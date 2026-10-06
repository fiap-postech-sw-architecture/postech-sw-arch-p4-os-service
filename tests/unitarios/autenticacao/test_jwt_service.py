from __future__ import annotations

from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.utils import base64url_decode

from src.autenticacao.dominio.exceptions import (
    TokenExpiradoException,
    TokenInvalidoException,
)
from src.autenticacao.infraestrutura.jwt_service import (
    carregar_chave_privada,
    carregar_chave_publica,
    kid_da_chave,
)
from tests.chaves_jwt import (
    CHAVE,
    CHAVE_PEM,
    KID,
    OUTRA_CHAVE,
    OUTRO_KID,
    adulterar,
    assinar,
    claims,
    forjar_hmac_com_a_chave_publica,
    forjar_sem_assinatura,
    instante,
    jwt_service,
    pem_privado,
    pem_publico,
)

_CLAIMS_DO_ACCESS = {"iss", "aud", "sub", "papel", "type", "jti", "iat", "exp"}


class TestEmissao:
    def test_access_token_rs256_com_kid_e_as_claims_do_adr_039(self) -> None:
        uid = uuid4()
        token = jwt_service().gerar_access_token(uid, "admin")

        assert jwt.get_unverified_header(token) == {
            "alg": "RS256",
            "kid": KID,
            "typ": "JWT",
        }
        payload = jwt_service().validar_token(token)
        assert set(payload) == _CLAIMS_DO_ACCESS
        assert (payload["iss"], payload["aud"]) == ("pytstop-os-service", "pytstop")
        assert (payload["sub"], payload["papel"], payload["type"]) == (
            str(uid),
            "admin",
            "access",
        )

    def test_refresh_token_sem_papel_e_sem_email(self) -> None:
        uid = uuid4()
        payload = jwt_service().validar_token(jwt_service().gerar_refresh_token(uid))
        assert set(payload) == _CLAIMS_DO_ACCESS - {"papel"}
        assert (payload["sub"], payload["type"]) == (str(uid), "refresh")

    def test_validade_vem_do_construtor(self) -> None:
        payload = jwt_service(expiracao_minutos=15).validar_token(
            jwt_service(expiracao_minutos=15).gerar_access_token(uuid4(), "admin")
        )
        assert payload["exp"] - payload["iat"] == 15 * 60  # type: ignore[operator]

    def test_jti_unico_por_token(self) -> None:
        svc = jwt_service()
        uid = uuid4()
        p1 = svc.validar_token(svc.gerar_access_token(uid, "admin"))
        p2 = svc.validar_token(svc.gerar_access_token(uid, "admin"))
        assert p1["jti"] != p2["jti"]


class TestValidacao:
    @pytest.mark.parametrize("atraso_s", [0, 1, 9])
    @pytest.mark.usefixtures("relogio_congelado")
    def test_expirado_ate_9s_atras_ainda_vale_pelo_leeway(self, atraso_s: int) -> None:
        token = assinar(claims(iat=instante(-3600), exp=instante(-atraso_s)))

        assert jwt_service().validar_token(token)["type"] == "access"

    @pytest.mark.parametrize("atraso_s", [10, 11, 60])
    @pytest.mark.usefixtures("relogio_congelado")
    def test_expirado_10s_atras_ou_mais_e_recusado(self, atraso_s: int) -> None:
        # O leeway e de 10 s exatos: com exp 10 s atras o token ja expirou.
        token = assinar(claims(iat=instante(-3600), exp=instante(-atraso_s)))

        with pytest.raises(TokenExpiradoException) as exc:
            jwt_service().validar_token(token)
        assert exc.value.motivo == "expired_token"

    @pytest.mark.parametrize("adianto_s", [0, 1, 10])
    @pytest.mark.usefixtures("relogio_congelado")
    def test_iat_ate_10s_a_frente_ainda_vale_pelo_leeway(self, adianto_s: int) -> None:
        token = assinar(claims(iat=instante(adianto_s), exp=instante(3600)))

        assert jwt_service().validar_token(token)["type"] == "access"

    @pytest.mark.parametrize("adianto_s", [11, 12, 60])
    @pytest.mark.usefixtures("relogio_congelado")
    def test_iat_11s_a_frente_ou_mais_e_recusado(self, adianto_s: int) -> None:
        # Relogio de um pod mais de 10 s adiantado: o token ainda nao "nasceu".
        token = assinar(claims(iat=instante(adianto_s), exp=instante(3600)))

        with pytest.raises(TokenInvalidoException) as exc:
            jwt_service().validar_token(token)
        assert exc.value.motivo == "iat_in_future"

    @pytest.mark.parametrize(
        ("token", "motivo"),
        [
            pytest.param("lixo.token.invalido", "malformed", id="malformado"),
            pytest.param("so-um-segmento", "malformed", id="sem-segmentos"),
            pytest.param(forjar_sem_assinatura(), "invalid_algorithm", id="alg-none"),
            pytest.param(
                forjar_hmac_com_a_chave_publica(),
                "invalid_algorithm",
                id="hs256-com-a-chave-publica",
            ),
            pytest.param(
                assinar(claims(aud="outro-servico")),
                "invalid_audience",
                id="aud-errada",
            ),
            pytest.param(
                assinar(claims(iss="outro-emissor")), "invalid_issuer", id="iss-errado"
            ),
            pytest.param(
                assinar(chave=OUTRA_CHAVE), "unknown_kid", id="kid-desconhecido"
            ),
            pytest.param(
                assinar(chave=OUTRA_CHAVE, kid=KID),
                "invalid_signature",
                id="outra-chave-com-o-kid-certo",
            ),
            pytest.param(
                assinar().rpartition(".")[0] + ".!!!",
                "malformed",
                id="assinatura-que-nao-e-base64",
            ),
            pytest.param(
                jwt.encode(claims(), CHAVE, algorithm="RS256"),
                "unknown_kid",
                id="sem-kid",
            ),
        ],
    )
    def test_token_forjado_ou_de_outro_emissor_e_recusado(
        self, token: str, motivo: str
    ) -> None:
        with pytest.raises(TokenInvalidoException) as exc:
            jwt_service().validar_token(token)
        assert exc.value.motivo == motivo
        assert exc.value.mensagem == "Credencial ausente, invalida ou expirada"

    @pytest.mark.parametrize(
        "ausente", ["iss", "aud", "sub", "type", "jti", "iat", "exp"]
    )
    def test_claim_obrigatoria_ausente(self, ausente: str) -> None:
        corpo = claims()
        del corpo[ausente]
        with pytest.raises(TokenInvalidoException) as exc:
            jwt_service().validar_token(assinar(corpo))
        assert exc.value.motivo == "missing_claim"

    def test_corpo_adulterado_com_a_assinatura_original(self) -> None:
        token = jwt_service().gerar_access_token(uuid4(), "atendente")
        with pytest.raises(TokenInvalidoException) as exc:
            jwt_service().validar_token(adulterar(token, papel="admin"))
        assert exc.value.motivo == "invalid_signature"


class TestRotacao:
    def test_token_da_chave_anterior_vale_enquanto_ela_esta_configurada(self) -> None:
        antigo = jwt_service(chave=OUTRA_CHAVE).gerar_refresh_token(uuid4())

        com_anterior = jwt_service(anterior=OUTRA_CHAVE)
        assert com_anterior.validar_token(antigo)["type"] == "refresh"
        # Token novo sai sempre com a chave atual.
        novo = com_anterior.gerar_access_token(uuid4(), "admin")
        assert jwt.get_unverified_header(novo)["kid"] == KID

        with pytest.raises(TokenInvalidoException) as exc:
            jwt_service().validar_token(antigo)
        assert exc.value.motivo == "unknown_kid"

    def test_jwks_publica_a_atual_e_depois_a_anterior(self) -> None:
        kids = [c["kid"] for c in jwt_service(anterior=OUTRA_CHAVE).jwks()["keys"]]
        assert kids == [KID, OUTRO_KID]

    def test_anterior_igual_a_atual_nao_duplica(self) -> None:
        assert [c["kid"] for c in jwt_service(anterior=CHAVE).jwks()["keys"]] == [KID]


class TestJwks:
    def test_so_membros_publicos_e_n_e_da_chave(self) -> None:
        (chave,) = jwt_service().jwks()["keys"]

        assert set(chave) == {"kty", "use", "alg", "kid", "n", "e"}
        assert (chave["kty"], chave["use"], chave["alg"], chave["kid"]) == (
            "RSA",
            "sig",
            "RS256",
            KID,
        )
        numeros = CHAVE.public_key().public_numbers()
        assert int.from_bytes(base64url_decode(chave["n"])) == numeros.n
        assert int.from_bytes(base64url_decode(chave["e"])) == numeros.e

    def test_kid_e_o_thumbprint_da_rfc_7638(self) -> None:
        # Exemplo da secao 3.1 da RFC 7638.
        n = (
            "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1R"
            "K7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9"
            "yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZg"
            "nYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6We"
            "Zu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
        )
        chave = rsa.RSAPublicNumbers(
            65537, int.from_bytes(base64url_decode(n))
        ).public_key()
        assert kid_da_chave(chave) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def _pem_pkcs1(chave: rsa.RSAPrivateKey) -> str:
    return chave.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


# Fraca de proposito: o que se testa e a recusa.
_RSA_1024 = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505
_EC = ec.generate_private_key(ec.SECP256R1())


class TestCarregarChaves:
    @pytest.mark.parametrize(
        "pem",
        [
            pytest.param(CHAVE_PEM, id="pkcs8"),
            pytest.param(_pem_pkcs1(CHAVE), id="pkcs1"),
        ],
    )
    def test_privada_rsa_de_2048(self, pem: str) -> None:
        assert kid_da_chave(carregar_chave_privada(pem).public_key()) == KID

    @pytest.mark.parametrize(
        ("pem", "erro"),
        [
            pytest.param("nao e pem", "PEM sem senha", id="texto"),
            pytest.param(pem_publico(CHAVE), "PEM sem senha", id="publica"),
            pytest.param(
                CHAVE.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.BestAvailableEncryption(b"senha-de-teste"),
                ).decode(),
                "PEM sem senha",
                id="cifrada",
            ),
            pytest.param(
                _EC.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
                "RSA",
                id="ec",
            ),
            pytest.param(pem_privado(_RSA_1024), "1024 bits", id="rsa-1024"),
        ],
    )
    def test_privada_recusada(self, pem: str, erro: str) -> None:
        with pytest.raises(ValueError, match=erro):
            carregar_chave_privada(pem)

    def test_publica_rsa_de_2048(self) -> None:
        assert kid_da_chave(carregar_chave_publica(pem_publico(OUTRA_CHAVE))) == (
            OUTRO_KID
        )

    @pytest.mark.parametrize(
        ("pem", "erro"),
        [
            pytest.param("nao e pem", "PEM", id="texto"),
            pytest.param(
                _EC.public_key()
                .public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                .decode(),
                "RSA",
                id="ec",
            ),
            pytest.param(pem_publico(_RSA_1024), "1024 bits", id="rsa-1024"),
        ],
    )
    def test_publica_recusada(self, pem: str, erro: str) -> None:
        with pytest.raises(ValueError, match=erro):
            carregar_chave_publica(pem)
