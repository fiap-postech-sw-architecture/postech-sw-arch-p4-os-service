"""JWKS publico e validacao dos tokens por um validador independente (ADR-039).

O validador independente e o ``scripts/validar_token.py`` (so PyJWT, como
Billing e Execucao): busca o JWKS servido pela app num uvicorn de verdade.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from jwt.utils import base64url_decode

from scripts.validar_token import validar_access_token
from src.autenticacao.infraestrutura.jwt_service import JWTService, kid_da_chave
from src.autenticacao.interfaces.dependencies import obter_jwt_service
from src.autenticacao.interfaces.router import jwks
from src.main import criar_app
from tests.chaves_jwt import (
    CHAVE,
    CHAVE_PEM,
    KID,
    OUTRA_CHAVE,
    OUTRO_KID,
    assinar,
    claims,
    forjar_hmac_com_a_chave_publica,
    forjar_sem_assinatura,
    instante,
    jwk_publico,
    jwt_service,
    pem_privado,
    pem_publico,
)
from tests.servidor_http import servir

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cryptography.hazmat.primitives.asymmetric import rsa

_JWKS = "/.well-known/jwks.json"
# Membros privados de uma JWK RSA (RFC 7518, secao 6.3.2).
_MEMBROS_PRIVADOS = {"d", "p", "q", "dp", "dq", "qi", "oth"}


@pytest.fixture(autouse=True)
def _chave_no_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_PRIVATE_KEY", CHAVE_PEM)
    monkeypatch.delenv("JWT_PREVIOUS_PUBLIC_KEY", raising=False)


@pytest.fixture(scope="module")
def url_base() -> Iterator[str]:
    """A app servida por HTTP; o JWKS reflete o ambiente de cada teste."""
    with servir(criar_app()) as url:
        yield url


class TestRotaJwks:
    def test_publica_sem_token_com_cache_de_10_minutos(self) -> None:
        resp = TestClient(criar_app()).get(_JWKS)

        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "public, max-age=600"
        assert resp.headers["Content-Type"] == "application/json"
        # Os demais headers de seguranca continuam.
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_61a_chamada_no_minuto_recebe_429_no_envelope(self) -> None:
        # O limite padrao (RATE_LIMIT) nao alcanca as rotas de include_router: o
        # JWKS, publico e sem token, leva o proprio limite de 60/min por IP.
        client = TestClient(criar_app())

        respostas = [client.get(_JWKS) for _ in range(61)]

        assert [r.status_code for r in respostas] == [200] * 60 + [429]
        assert respostas[-1].json()["erro"]["codigo"] == "RATE_LIMIT_EXCEDIDO"

    def test_rota_roda_no_event_loop(self) -> None:
        # A docstring promete responder mesmo com o threadpool cheio: so vale se a
        # rota for async (com o limite do SlowAPI por cima).
        assert inspect.iscoroutinefunction(jwks)

    @pytest.mark.parametrize(
        "metodo",
        [pytest.param(m, id=m.lower()) for m in ("POST", "PUT", "DELETE", "HEAD")],
    )
    def test_metodo_que_nao_e_get_da_405_com_allow_no_envelope(
        self, metodo: str
    ) -> None:
        resp = TestClient(criar_app()).request(metodo, _JWKS)

        assert resp.status_code == 405
        assert resp.headers["Allow"] == "GET"
        assert resp.headers["Cache-Control"] == "no-store"
        if metodo != "HEAD":  # a resposta do HEAD vai sem corpo
            assert resp.json()["erro"]["codigo"] == "METODO_NAO_PERMITIDO"

    def test_rota_inexistente_no_app_real_responde_o_id_da_requisicao(self) -> None:
        resp = TestClient(criar_app()).get(
            "/.well-known/nao-existe.json", headers={"X-Request-ID": "req-jwks-404"}
        )

        assert resp.status_code == 404
        assert resp.json() == {
            "erro": {
                "codigo": "ENTIDADE_NAO_ENCONTRADA",
                "mensagem": "Recurso nao encontrado",
                "id_requisicao": "req-jwks-404",
            }
        }
        assert resp.headers["X-Request-ID"] == "req-jwks-404"

    def test_formato_rfc_7517_so_com_a_parte_publica(self) -> None:
        (chave,) = TestClient(criar_app()).get(_JWKS).json()["keys"]

        assert chave.keys() == {"kty", "use", "alg", "kid", "n", "e"}
        assert not chave.keys() & _MEMBROS_PRIVADOS
        assert (chave["kty"], chave["use"], chave["alg"], chave["kid"]) == (
            "RSA",
            "sig",
            "RS256",
            KID,
        )
        numeros = CHAVE.public_key().public_numbers()
        assert int.from_bytes(base64url_decode(chave["n"])) == numeros.n
        assert int.from_bytes(base64url_decode(chave["e"])) == numeros.e

    def test_nada_da_chave_privada_sai_na_resposta(self) -> None:
        corpo = TestClient(criar_app()).get(_JWKS).text
        privados = CHAVE.private_numbers()
        for segredo in (privados.d, privados.p, privados.q):
            assert jwt.utils.to_base64url_uint(segredo).decode() not in corpo
        assert "PRIVATE" not in corpo

    def test_na_rotacao_publica_tambem_a_anterior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JWT_PREVIOUS_PUBLIC_KEY", pem_publico(OUTRA_CHAVE))
        chaves = TestClient(criar_app()).get(_JWKS).json()["keys"]
        assert [c["kid"] for c in chaves] == [KID, OUTRO_KID]


class TestFalhaInternaDoJwks:
    """Erro ao montar o JWKS e erro do servidor (500, sem cache), nunca 422."""

    def test_jwks_inconsistente_responde_500_sem_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug do servico, nao do cliente: a validacao do response_model falha e
        # o handler de ValueError (422) nao pode alcanca-la.
        monkeypatch.setattr(
            JWTService, "jwks", lambda _self: {"keys": [{"kty": "RSA"}]}
        )

        resp = TestClient(criar_app(), raise_server_exceptions=False).get(_JWKS)

        assert resp.status_code == 500
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.json()["erro"]["codigo"] == "ERRO_INTERNO"
        assert "RSA" not in resp.text

    def test_chave_ausente_responde_500_sem_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_PRIVATE_KEY")

        resp = TestClient(criar_app(), raise_server_exceptions=False).get(_JWKS)

        assert resp.status_code == 500
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.json()["erro"]["codigo"] == "ERRO_INTERNO"


class TestValidadorIndependente:
    def test_aceita_o_access_token_emitido_pelo_servico(self, url_base: str) -> None:
        uid = uuid4()
        token = jwt_service().gerar_access_token(uid, "mecanico")

        resultado = validar_access_token(url_base, token)

        assert (resultado["sub"], resultado["papel"]) == (str(uid), "mecanico")
        assert "email" not in resultado

    @pytest.mark.parametrize(
        "atraso_s",
        [
            pytest.param(0, id="0s-atras"),
            pytest.param(9, id="9s-atras"),
        ],
    )
    @pytest.mark.usefixtures("relogio_congelado")
    def test_aceita_expirado_ate_9s_atras(self, url_base: str, atraso_s: int) -> None:
        token = assinar(claims(iat=instante(-3600), exp=instante(-atraso_s)))
        assert validar_access_token(url_base, token)["type"] == "access"

    @pytest.mark.parametrize(
        "atraso_s",
        [
            pytest.param(10, id="10s-atras"),
            pytest.param(11, id="11s-atras"),
        ],
    )
    @pytest.mark.usefixtures("relogio_congelado")
    def test_recusa_expirado_10s_atras_ou_mais(
        self, url_base: str, atraso_s: int
    ) -> None:
        # O leeway do validador e de 10 s exatos, como o do servico e dos consumidores.
        token = assinar(claims(iat=instante(-3600), exp=instante(-atraso_s)))
        with pytest.raises(jwt.ExpiredSignatureError):
            validar_access_token(url_base, token)

    @pytest.mark.parametrize(
        "ausente",
        [
            pytest.param(nome, id=f"sem-{nome}")
            for nome in ("exp", "sub", "type", "jti", "iat")
        ],
    )
    def test_recusa_token_sem_claim_obrigatoria(
        self, url_base: str, ausente: str
    ) -> None:
        corpo = claims()
        del corpo[ausente]

        with pytest.raises(jwt.MissingRequiredClaimError):
            validar_access_token(url_base, assinar(corpo))

    @pytest.mark.parametrize(
        "adianto_s",
        [
            pytest.param(0, id="0s-a-frente"),
            pytest.param(10, id="10s-a-frente"),
        ],
    )
    @pytest.mark.usefixtures("relogio_congelado")
    def test_aceita_iat_ate_10s_a_frente(self, url_base: str, adianto_s: int) -> None:
        token = assinar(claims(iat=instante(adianto_s), exp=instante(3600)))
        assert validar_access_token(url_base, token)["type"] == "access"

    @pytest.mark.parametrize(
        "adianto_s",
        [
            pytest.param(11, id="11s-a-frente"),
            pytest.param(12, id="12s-a-frente"),
        ],
    )
    @pytest.mark.usefixtures("relogio_congelado")
    def test_recusa_iat_11s_a_frente_ou_mais(
        self, url_base: str, adianto_s: int
    ) -> None:
        token = assinar(claims(iat=instante(adianto_s), exp=instante(3600)))
        with pytest.raises(jwt.ImmatureSignatureError):
            validar_access_token(url_base, token)

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param(
                lambda: jwt_service().gerar_refresh_token(uuid4()),
                id="refresh-como-access",
            ),
            pytest.param(forjar_sem_assinatura, id="alg-none"),
            pytest.param(
                forjar_hmac_com_a_chave_publica, id="hs256-com-a-chave-publica"
            ),
            pytest.param(lambda: assinar(claims(aud="outro-servico")), id="aud-errada"),
            pytest.param(lambda: assinar(claims(iss="outro-emissor")), id="iss-errado"),
            pytest.param(lambda: assinar(chave=OUTRA_CHAVE), id="kid-desconhecido"),
            pytest.param(
                lambda: assinar(chave=OUTRA_CHAVE, kid=KID),
                id="outra-chave-com-o-kid-certo",
            ),
            pytest.param(
                lambda: assinar(chave=OUTRA_CHAVE, jwk=jwk_publico(OUTRA_CHAVE)),
                id="jwk-do-atacante-no-cabecalho",
            ),
            pytest.param(
                lambda: assinar(
                    chave=OUTRA_CHAVE, jku="https://atacante.example/jwks.json"
                ),
                id="jku-do-atacante",
            ),
            pytest.param(
                lambda: assinar(
                    chave=OUTRA_CHAVE, x5u="https://atacante.example/cert.pem"
                ),
                id="x5u-do-atacante",
            ),
            pytest.param(
                lambda: assinar(crit=["extensao"], extensao="x"),
                id="crit-com-extensao-desconhecida",
            ),
        ],
    )
    def test_recusa_token_forjado_ou_de_outro_tipo(
        self, url_base: str, token: Callable[[], str]
    ) -> None:
        with pytest.raises(jwt.PyJWTError):
            validar_access_token(url_base, token())

    @pytest.mark.parametrize(
        ("assina", "anterior"),
        [
            pytest.param(CHAVE, OUTRA_CHAVE, id="etapa-1-a-nova-so-publicada"),
            pytest.param(OUTRA_CHAVE, CHAVE, id="etapa-2-a-nova-assina"),
        ],
    )
    def test_token_emitido_pela_configuracao_de_rotacao_passa_pelo_jwks(
        self,
        url_base: str,
        monkeypatch: pytest.MonkeyPatch,
        assina: rsa.RSAPrivateKey,
        anterior: rsa.RSAPrivateKey,
    ) -> None:
        # O servico emite pela fabrica do ambiente (o mesmo caminho do login) e o
        # validador independente confere pelo JWKS que a app serve: o kid do
        # cabecalho e o da chave que assina, nunca o da anterior.
        monkeypatch.setenv("JWT_PRIVATE_KEY", pem_privado(assina))
        monkeypatch.setenv("JWT_PREVIOUS_PUBLIC_KEY", pem_publico(anterior))
        uid = uuid4()

        token = obter_jwt_service().gerar_access_token(uid, "admin")

        assert validar_access_token(url_base, token)["sub"] == str(uid)
        assert jwt.get_unverified_header(token)["kid"] == kid_da_chave(
            assina.public_key()
        )

    def test_token_da_chave_anterior_vale_enquanto_ela_esta_no_jwks(
        self, url_base: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Emitido antes da troca, com a chave que virou a anterior.
        em_voo = jwt_service(chave=OUTRA_CHAVE).gerar_access_token(uuid4(), "admin")
        monkeypatch.setenv("JWT_PREVIOUS_PUBLIC_KEY", pem_publico(OUTRA_CHAVE))
        assert validar_access_token(url_base, em_voo)["papel"] == "admin"

        monkeypatch.delenv("JWT_PREVIOUS_PUBLIC_KEY")
        with pytest.raises(jwt.PyJWKClientError, match="Unable to find a signing key"):
            validar_access_token(url_base, em_voo)
