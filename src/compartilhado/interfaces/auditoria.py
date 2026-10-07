"""Helper de auditoria compartilhado entre routers (LGPD, cancelamento de OS).

Uma unica logica de identificacao do ator a partir do JWT (p3 #76).
"""

from __future__ import annotations

from src.compartilhado.dominio.exceptions import FalhaAutenticacaoException


def ator_de(usuario: dict[str, object]) -> str:
    """O ator da operacao auditada: o ``sub`` do JWT (id do usuario).

    Nunca o e-mail: o token nao o carrega (ADR-039) e o log de auditoria nao
    pode virar PII se um token com e-mail aparecer. O gate ja exige o ``sub``;
    sem ele, a credencial e invalida (401), e nenhuma escrita fica sem ator.

    Raises:
        FalhaAutenticacaoException: claims sem ``sub`` em texto.
    """
    sub = usuario.get("sub")
    if not isinstance(sub, str) or not sub:
        raise FalhaAutenticacaoException("missing_sub")
    return sub
