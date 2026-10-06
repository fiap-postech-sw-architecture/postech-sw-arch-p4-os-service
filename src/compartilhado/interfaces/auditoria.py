"""Helper de auditoria compartilhado entre routers (LGPD, cancelamento de OS).

Uma unica logica de identificacao do ator a partir do JWT (p3 #76).
"""

from __future__ import annotations


def ator_de(usuario: dict[str, object]) -> str | None:
    """Extrai um identificador do usuario autenticado para o log de auditoria.

    Usa so o ``sub`` do JWT (id do usuario), nunca o e-mail: o token nao o
    carrega (ADR-039) e o log de auditoria nao pode virar PII se um token com
    e-mail aparecer. Retorna ``None`` quando nao ha ``sub`` -- o evento de
    auditoria ainda e emitido, so sem o ator.
    """
    sub = usuario.get("sub")
    return sub if isinstance(sub, str) and sub else None
