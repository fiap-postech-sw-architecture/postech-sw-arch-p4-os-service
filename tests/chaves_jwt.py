"""Chaves RSA e tokens dos testes: geradas a cada sessao, nenhuma fica no repo.

``CHAVE`` e a chave de assinatura que os testes poem em ``JWT_PRIVATE_KEY``;
``OUTRA_CHAVE`` faz o papel de chave alheia ou da chave anterior na rotacao.
Os ``forjar_*`` montam os ataques classicos a mao, sem passar pelo
``JWTService``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.autenticacao.infraestrutura.jwt_service import (
    AUDIENCIA,
    EMISSOR,
    JWTService,
    kid_da_chave,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import tzinfo

    import pytest


def _nova_chave() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem_privado(chave: rsa.RSAPrivateKey) -> str:
    return chave.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def pem_publico(chave: rsa.RSAPrivateKey) -> str:
    return (
        chave.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


CHAVE = _nova_chave()
CHAVE_PEM = pem_privado(CHAVE)
KID = kid_da_chave(CHAVE.public_key())
OUTRA_CHAVE = _nova_chave()
OUTRA_CHAVE_PEM = pem_privado(OUTRA_CHAVE)
OUTRO_KID = kid_da_chave(OUTRA_CHAVE.public_key())


def jwt_service(
    expiracao_minutos: int = 30,
    *,
    chave: rsa.RSAPrivateKey = CHAVE,
    anterior: rsa.RSAPrivateKey | None = None,
) -> JWTService:
    return JWTService(
        chave_privada=chave,
        expiracao_minutos=expiracao_minutos,
        refresh_expiracao_minutos=10080,
        chave_anterior=None if anterior is None else anterior.public_key(),
    )


def claims(**extras: object) -> dict[str, object]:
    """Claims de um access token valido por 1 h; ``extras`` sobrescreve."""
    agora = datetime.now(UTC)
    return {
        "iss": EMISSOR,
        "aud": AUDIENCIA,
        "sub": str(uuid4()),
        "papel": "admin",
        "type": "access",
        "jti": str(uuid4()),
        "iat": agora,
        "exp": agora + timedelta(hours=1),
        **extras,
    }


# Instante em que o relogio do PyJWT para, nos testes de leeway.
AGORA_CONGELADA: Final = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)


def congelar_o_relogio_do_pyjwt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixa em ``AGORA_CONGELADA`` o ``now`` com que o PyJWT confere ``exp`` e ``iat``.

    So o ``decode`` enxerga o relogio congelado, entao os tokens do teste levam
    ``iat`` e ``exp`` como NumericDate inteiro (``instante``): o ``encode`` so
    converte ``datetime`` de verdade.
    """

    class _Relogio(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is None:
                return AGORA_CONGELADA.replace(tzinfo=None)
            return AGORA_CONGELADA

    monkeypatch.setattr("jwt.api_jwt.datetime", _Relogio)


def instante(deslocamento_segundos: int) -> int:
    """NumericDate de ``AGORA_CONGELADA`` mais ``deslocamento_segundos``."""
    return int(AGORA_CONGELADA.timestamp()) + deslocamento_segundos


def validade_em_segundos(payload: Mapping[str, object]) -> int:
    """``exp - iat`` das claims de um token validado (NumericDate, inteiros)."""
    return cast("int", payload["exp"]) - cast("int", payload["iat"])


def assinar(
    corpo: Mapping[str, object] | None = None,
    *,
    chave: rsa.RSAPrivateKey = CHAVE,
    kid: str | None = None,
) -> str:
    """RS256 com ``chave``; o ``kid`` padrao e o da propria chave."""
    return jwt.encode(
        dict(corpo if corpo is not None else claims()),
        chave,
        algorithm="RS256",
        headers={"kid": kid if kid is not None else kid_da_chave(chave.public_key())},
    )


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


def _segmentos(cabecalho: dict[str, object], corpo: Mapping[str, object]) -> str:
    return ".".join(
        _b64(json.dumps(parte, default=str).encode())
        for parte in (cabecalho, _numerico(corpo))
    )


def _numerico(corpo: Mapping[str, object]) -> dict[str, object]:
    # iat/exp como NumericDate, como o PyJWT serializaria.
    return {
        nome: int(valor.timestamp()) if isinstance(valor, datetime) else valor
        for nome, valor in corpo.items()
    }


def forjar_sem_assinatura() -> str:
    """``alg=none`` com o ``kid`` certo e assinatura vazia."""
    return _segmentos({"alg": "none", "typ": "JWT", "kid": KID}, claims()) + "."


_HASH_DO_HMAC = {
    "HS256": hashlib.sha256,
    "HS384": hashlib.sha384,
    "HS512": hashlib.sha512,
}


def forjar_hmac_com_a_chave_publica(algoritmo: str = "HS256") -> str:
    """Troca de algoritmo: HMAC com o PEM publico (que esta no JWKS) como segredo."""
    segmentos = _segmentos({"alg": algoritmo, "typ": "JWT", "kid": KID}, claims())
    assinatura = hmac.new(
        pem_publico(CHAVE).encode(), segmentos.encode(), _HASH_DO_HMAC[algoritmo]
    ).digest()
    return f"{segmentos}.{_b64(assinatura)}"


def adulterar(token: str, **extras: object) -> str:
    """Troca claims do corpo e mantem o cabecalho e a assinatura originais."""
    cabecalho, _, assinatura = token.split(".")
    corpo = {**jwt.decode(token, options={"verify_signature": False}), **extras}
    return f"{cabecalho}.{_b64(json.dumps(corpo).encode())}.{assinatura}"


_RAIZ = Path(__file__).resolve().parents[1]


def pem_demo_do_compose() -> str:
    """Chave RSA de demonstracao do docker-compose.yml (string YAML com ``\\n``)."""
    compose = (_RAIZ / "docker-compose.yml").read_text()
    escapado = re.search(r'JWT_PRIVATE_KEY: "([^"]+)"', compose)
    assert escapado is not None
    return escapado.group(1).replace("\\n", "\n")


def pem_demo_do_env_example() -> str:
    """Chave RSA de demonstracao do .env.example (valor entre aspas, multilinha)."""
    exemplo = (_RAIZ / ".env.example").read_text()
    valor = re.search(r'^JWT_PRIVATE_KEY="([^"]+)"', exemplo, re.MULTILINE)
    assert valor is not None
    return valor.group(1)
