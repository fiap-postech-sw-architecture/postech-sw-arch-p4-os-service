"""Valida um access token do OS Service como Billing e Execucao validam (ADR-039).

So usa o PyJWT, nada do codigo do servico: busca o JWKS publico com o
``PyJWKClient`` (cache de 10 min, timeout de 2 s), escolhe a chave pelo ``kid``
e confere assinatura RS256, ``iss``, ``aud``, ``exp`` (leeway de 10 s) e
``type=access``. O ``make smoke`` roda este script dentro do container com o
token do login do admin semeado; os testes o usam como validador independente.

Uso: ``python scripts/validar_token.py <url-base> < arquivo-com-o-token``
"""

from __future__ import annotations

import sys

import jwt


def validar_access_token(url_base: str, token: str) -> dict[str, object]:
    """Claims do access token; levanta ``jwt.PyJWTError`` se for recusado."""
    cliente = jwt.PyJWKClient(
        f"{url_base}/.well-known/jwks.json", lifespan=600, timeout=2
    )
    chave = cliente.get_signing_key_from_jwt(token)
    claims: dict[str, object] = jwt.decode(
        token,
        chave,
        algorithms=["RS256"],
        issuer="pytstop-os-service",
        audience="pytstop",
        leeway=10,
        options={"require": ["iss", "aud", "sub", "type", "jti", "iat", "exp"]},
    )
    if claims["type"] != "access":
        msg = "o token nao e um access token"
        raise jwt.InvalidTokenError(msg)
    return claims


if __name__ == "__main__":
    claims = validar_access_token(sys.argv[1], sys.stdin.read().strip())
    print(f"access token valido pelo JWKS: papel {claims['papel']}")
