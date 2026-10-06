"""Emissao e validacao dos JWT do PytStop: RS256 com JWKS publico (ADR-039).

O OS Service e o unico emissor. A chave privada RSA so existe aqui; a parte
publica sai em ``GET /.well-known/jwks.json``, de onde Billing e Execucao
validam os tokens sem segredo compartilhado. O ``kid`` de cada chave e o
thumbprint da RFC 7638. Na rotacao, a chave anterior so e publicada e aceita na
validacao, para os tokens em voo continuarem validos; ela nunca assina.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
)
from jwt.utils import to_base64url_uint

from src.autenticacao.dominio.exceptions import (
    TokenExpiradoException,
    TokenInvalidoException,
)

if TYPE_CHECKING:
    from uuid import UUID

EMISSOR: Final = "pytstop-os-service"
AUDIENCIA: Final = "pytstop"
# Folga para a diferenca de relogio entre pods, em `exp` e `iat`.
LEEWAY_SEGUNDOS: Final = 10
BITS_MINIMOS: Final = 2048
_ALGORITMO: Final = "RS256"
_CLAIMS_OBRIGATORIAS: Final = ["iss", "aud", "sub", "type", "jti", "iat", "exp"]
# Motivo da recusa que vai para o log: a resposta ao cliente e sempre a mesma
# (ADR-039), mas numa rotacao de chave ou com o relogio de um pod fora de hora
# quem investiga precisa ver a causa. Mais especifico primeiro: a assinatura
# invalida e um caso de DecodeError.
_MOTIVOS_DA_RECUSA: Final = (
    (jwt.InvalidAlgorithmError, "invalid_algorithm"),
    (jwt.InvalidSignatureError, "invalid_signature"),
    (jwt.InvalidAudienceError, "invalid_audience"),
    (jwt.InvalidIssuerError, "invalid_issuer"),
    (jwt.MissingRequiredClaimError, "missing_claim"),
    # So o iat chega aqui: o servico nao emite nbf.
    (jwt.ImmatureSignatureError, "iat_in_future"),
    (jwt.DecodeError, "malformed"),
)


def carregar_chave_privada(pem: str) -> rsa.RSAPrivateKey:
    """Chave de assinatura a partir do PEM (PKCS#1 ou PKCS#8, sem senha).

    Raises:
        ValueError: PEM ilegivel, chave que nao e RSA ou com menos de 2048 bits.
    """
    try:
        chave = load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        msg = "nao e uma chave privada em PEM sem senha"
        raise ValueError(msg) from exc
    if not isinstance(chave, rsa.RSAPrivateKey):
        msg = "a chave precisa ser RSA"
        raise ValueError(msg)
    _exigir_tamanho_minimo(chave.key_size)
    return chave


def carregar_chave_publica(pem: str) -> rsa.RSAPublicKey:
    """Chave publica RSA a partir do PEM (a anterior, durante a rotacao).

    Raises:
        ValueError: PEM ilegivel, chave que nao e RSA ou com menos de 2048 bits.
    """
    try:
        chave = load_pem_public_key(pem.encode())
    except (ValueError, UnsupportedAlgorithm) as exc:
        msg = "nao e uma chave publica em PEM"
        raise ValueError(msg) from exc
    if not isinstance(chave, rsa.RSAPublicKey):
        msg = "a chave precisa ser RSA"
        raise ValueError(msg)
    _exigir_tamanho_minimo(chave.key_size)
    return chave


def _exigir_tamanho_minimo(bits: int) -> None:
    if bits < BITS_MINIMOS:
        msg = f"a chave tem {bits} bits; o minimo e {BITS_MINIMOS}"
        raise ValueError(msg)


def _motivo_da_recusa(erro: jwt.InvalidTokenError) -> str:
    for tipo, motivo in _MOTIVOS_DA_RECUSA:
        if isinstance(erro, tipo):
            return motivo
    return "invalid_token"


def _b64url(valor: int) -> str:
    return to_base64url_uint(valor).decode()


def kid_da_chave(chave: rsa.RSAPublicKey) -> str:
    """Thumbprint SHA-256 da chave publica (RFC 7638), usado como ``kid``."""
    numeros = chave.public_numbers()
    # Membros obrigatorios do kty RSA em ordem lexicografica, sem espacos.
    canonico = json.dumps(
        {"e": _b64url(numeros.e), "kty": "RSA", "n": _b64url(numeros.n)},
        separators=(",", ":"),
        sort_keys=True,
    )
    resumo = hashlib.sha256(canonico.encode()).digest()
    return base64.urlsafe_b64encode(resumo).rstrip(b"=").decode()


class JWTService:
    """Emite e valida os tokens do servico e publica as chaves no JWKS."""

    # As expiracoes chegam pelo construtor, lidas do ambiente na factory
    # `obter_jwt_service` (defaults 15 e 10080): fonte unica dos defaults.
    def __init__(
        self,
        chave_privada: rsa.RSAPrivateKey,
        expiracao_minutos: int,
        refresh_expiracao_minutos: int,
        chave_anterior: rsa.RSAPublicKey | None = None,
    ) -> None:
        self._chave_privada = chave_privada
        self._kid = kid_da_chave(chave_privada.public_key())
        # A atual primeiro; a anterior so verifica (tokens emitidos antes da troca).
        self._chaves_publicas: dict[str, rsa.RSAPublicKey] = {
            self._kid: chave_privada.public_key()
        }
        if chave_anterior is not None:
            self._chaves_publicas.setdefault(
                kid_da_chave(chave_anterior), chave_anterior
            )
        self._expiracao_minutos = expiracao_minutos
        self._refresh_expiracao_minutos = refresh_expiracao_minutos

    def gerar_access_token(self, usuario_id: UUID, papel: str) -> str:
        """Access token do usuario, com o ``papel`` e a validade curta do access.

        Sem e-mail: o token circula entre os servicos (ADR-039).
        """
        return self._assinar(
            {"sub": str(usuario_id), "papel": papel, "type": "access"},
            self._expiracao_minutos,
        )

    def gerar_refresh_token(self, usuario_id: UUID) -> str:
        """Refresh token do usuario: so ``sub``, sem ``papel``, com a validade longa."""
        return self._assinar(
            {"sub": str(usuario_id), "type": "refresh"}, self._refresh_expiracao_minutos
        )

    def _assinar(self, claims: dict[str, str], minutos: int) -> str:
        agora = datetime.now(UTC)
        payload = {
            "iss": EMISSOR,
            "aud": AUDIENCIA,
            **claims,
            "jti": str(uuid.uuid4()),
            "iat": agora,
            "exp": agora + timedelta(minutes=minutos),
        }
        return jwt.encode(
            payload,
            self._chave_privada,
            algorithm=_ALGORITMO,
            headers={"kid": self._kid},
        )

    def validar_token(self, token: str) -> dict[str, object]:
        """Claims de um token deste emissor, de qualquer ``type``.

        Do cabecalho nao verificado so sai o ``kid``, para escolher entre as
        chaves deste servico; o algoritmo e fixo (RS256), o que barra ``none``
        e HS256 assinado com a chave publica.

        Raises:
            TokenExpiradoException: ``exp`` vencido alem do leeway.
            TokenInvalidoException: formato, ``kid``, algoritmo, assinatura,
                ``iss``, ``aud``, ``iat`` ou claim obrigatoria; o ``motivo``
                diz qual (``malformed``, ``unknown_kid``, ``invalid_signature``
                e os demais de ``_MOTIVOS_DA_RECUSA``).
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            chave = self._chaves_publicas.get(kid) if isinstance(kid, str) else None
            if chave is None:
                raise TokenInvalidoException(motivo="unknown_kid")
            claims: dict[str, object] = jwt.decode(
                token,
                chave,
                algorithms=[_ALGORITMO],
                audience=AUDIENCIA,
                issuer=EMISSOR,
                leeway=LEEWAY_SEGUNDOS,
                options={"require": _CLAIMS_OBRIGATORIAS},
            )
        except jwt.ExpiredSignatureError:
            raise TokenExpiradoException() from None
        except jwt.InvalidTokenError as exc:
            raise TokenInvalidoException(motivo=_motivo_da_recusa(exc)) from None
        return claims

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """JWK Set (RFC 7517) so com a parte publica de cada chave."""
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": _ALGORITMO,
                    "kid": kid,
                    "n": _b64url(chave.public_numbers().n),
                    "e": _b64url(chave.public_numbers().e),
                }
                for kid, chave in self._chaves_publicas.items()
            ]
        }
