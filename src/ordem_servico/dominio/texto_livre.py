"""Texto livre do atendimento: a validacao da descricao do problema e do motivo.

O texto chega ao atendente na tela (consulta, historico) e a LGPD o alcanca
pela anonimizacao do cliente; por isso entra aparado, dentro do limite e sem
caractere que esconda ou reordene o que se le.
"""

from __future__ import annotations

import unicodedata
from typing import Final

from src.compartilhado.dominio.exceptions import ValorInvalidoException

# Limites dos textos livres (espelhados nas colunas e nos schemas HTTP).
TAMANHO_MAXIMO_DESCRICAO: Final = 1000
TAMANHO_MAXIMO_MOTIVO: Final = 500

# Categorias Unicode que nao se lem: controle (Cc, NUL inclusive, que o Postgres
# recusa no flush), formatacao (Cf: invisiveis e override bidirecional, que
# inverte a leitura) e separadores de linha e de paragrafo (Zl, Zp). So a quebra
# de linha e a tabulacao passam.
_CATEGORIAS_RECUSADAS: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})


def texto_obrigatorio(valor: str, rotulo: str, maximo: int) -> str:
    """Texto livre aparado, nao vazio, dentro do limite e sem caractere oculto.

    CRLF vira LF (quebra de linha de formulario). Violacao levanta
    ``ValorInvalidoException`` (422).
    """
    texto = (valor or "").replace("\r\n", "\n").strip()
    if not texto:
        msg = f"{rotulo} e obrigatorio"
        raise ValorInvalidoException(msg)
    if len(texto) > maximo:
        msg = f"{rotulo} excede {maximo} caracteres"
        raise ValorInvalidoException(msg)
    if any(
        c not in "\n\t" and unicodedata.category(c) in _CATEGORIAS_RECUSADAS
        for c in texto
    ):
        msg = f"{rotulo} tem caractere de controle ou de formatacao"
        raise ValorInvalidoException(msg)
    return texto
