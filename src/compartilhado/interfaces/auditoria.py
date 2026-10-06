"""Helper de auditoria compartilhado entre routers (LGPD, cancelamento de OS).

Uma unica logica de identificacao do ator a partir do JWT (p3 #76).
"""

from __future__ import annotations


def ator_de(usuario: dict[str, object]) -> str | None:
    """Extrai um identificador do usuario autenticado para o log de auditoria.

    Usa o ``sub`` do JWT (id do usuario); cai para ``email`` se presente.
    Retorna ``None`` quando nenhum identificador esta disponivel -- o evento de
    auditoria ainda e emitido, so sem o ator.
    """
    for chave in ("sub", "email"):
        valor = usuario.get(chave)
        if isinstance(valor, str) and valor:
            return valor
    return None
