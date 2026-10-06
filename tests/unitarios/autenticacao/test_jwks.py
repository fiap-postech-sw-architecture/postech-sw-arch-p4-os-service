"""JWKS publico e validacao dos tokens por um validador independente (ADR-039).

O validador independente e o ``scripts/validar_token.py`` (so PyJWT, como
Billing e Execucao): busca o JWKS servido pela app num uvicorn de verdade.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from jwt.utils import base64url_decode

from scripts.validar_token import validar_access_token
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
    jwt_service,
    pem_publico,
)
from tests.servidor_http import servir

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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


class TestValidadorIndependente:
    def test_aceita_o_access_token_emitido_pelo_servico(self, url_base: str) -> None:
        uid = uuid4()
        token = jwt_service().gerar_access_token(uid, "mecanico")

        resultado = validar_access_token(url_base, token)

        assert (resultado["sub"], resultado["papel"]) == (str(uid), "mecanico")
        assert "email" not in resultado

    def test_aceita_expirado_dentro_do_leeway(self, url_base: str) -> None:
        token = assinar(claims(exp=datetime.now(UTC) - timedelta(seconds=5)))
        assert validar_access_token(url_base, token)["type"] == "access"

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
            pytest.param(
                lambda: assinar(claims(exp=datetime.now(UTC) - timedelta(seconds=15))),
                id="expirado-alem-do-leeway",
            ),
            pytest.param(lambda: assinar(chave=OUTRA_CHAVE), id="kid-desconhecido"),
            pytest.param(
                lambda: assinar(chave=OUTRA_CHAVE, kid=KID),
                id="outra-chave-com-o-kid-certo",
            ),
        ],
    )
    def test_recusa_token_forjado_ou_de_outro_tipo(
        self, url_base: str, token: Callable[[], str]
    ) -> None:
        with pytest.raises(jwt.PyJWTError):
            validar_access_token(url_base, token())

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
