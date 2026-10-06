"""Normalizacao unica de CPF, CNPJ e placa, e o contrato ``Documento``.

Vive em ``compartilhado.dominio`` porque dois contextos validam esses dados:
o cadastro (Cliente+Veiculo) e o acompanhamento publico da OS, que precisa
rejeitar documento e placa invalidos antes de qualquer consulta ao banco sem
importar o nucleo de outro contexto (contrato do import-linter).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from brutils.cnpj import remove_symbols

if TYPE_CHECKING:
    from collections.abc import Callable

# re.ASCII: sem a flag, \D e Unicode e deixa passar digitos de outros alfabetos
# (ex.: arabe-indicos), que o brutils aceita e que gerariam um documento_hash
# diferente do mesmo CPF em ASCII, duplicando o cadastro apesar da UK (p3
# fc06263).
_NAO_DIGITO = re.compile(r"\D", re.ASCII)


def normalizar_cpf(numero: str) -> str:
    """CPF so com digitos ASCII: mascara, espaco e qualquer outro caractere saem."""
    return _NAO_DIGITO.sub("", numero)


def normalizar_cnpj(numero: str) -> str:
    """CNPJ sem ``.``, ``/`` e ``-`` e em maiusculas.

    Letras ficam: o CNPJ alfanumerico (IN RFB 2.229/2024, emitido desde
    julho de 2026) tem letras nas 12 primeiras posicoes, e o ``brutils`` ja
    calcula o digito verificador dele.
    """
    sem_simbolos: str = remove_symbols(numero)  # brutils sem py.typed
    return sem_simbolos.upper()


def normalizar_placa(valor: str) -> str:
    """Placa em maiusculas e sem hifen (``abc-1d23`` -> ``ABC1D23``)."""
    return valor.upper().replace("-", "")


def normalizar_e_validar(
    numero: str,
    rotulo: str,
    normalizar: Callable[[str], str],
    validador: Callable[[str], bool],
) -> str:
    """Normaliza e valida com o ``brutils``; ``ValueError`` se invalido.

    O resultado precisa ser ASCII: o ``brutils`` aceita digitos Unicode no
    digito verificador do CNPJ (``int`` le o arabe-indico U+0668 como 8), e
    um documento fora do ASCII geraria outro ``documento_hash``. A mensagem
    nao ecoa o numero (o handler de ``ValueError`` devolve ``str(exc)`` no
    corpo do 422).
    """
    normalizado = normalizar(numero)
    if not (normalizado.isascii() and validador(normalizado)):
        msg = f"{rotulo} invalido"
        raise ValueError(msg)
    return normalizado


class Documento(Protocol):
    """Contrato para documentos de identificacao fiscal (CPF, CNPJ).

    Define a interface comum exigida pelo agregado Cliente e pelos repositorios.
    Implementacoes (CPF, CNPJ) satisfazem este Protocol estruturalmente: basta
    expor `numero` (read-only), `formatado()` e `mascarado()`.
    """

    # corpos `pass` (nao `...`) evitam o FP CodeQL py/ineffectual-statement
    @property
    def numero(self) -> str:
        """Numero puro do documento (sem mascara), usado para busca por hash."""
        pass

    def formatado(self) -> str:
        """Retorna o documento formatado por extenso para exibicao ao usuario."""
        pass

    def mascarado(self) -> str:
        """Retorna o documento com os digitos centrais ocultos (seguro para logs)."""
        pass
