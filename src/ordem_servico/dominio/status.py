"""Enumeracao ``StatusOrdem``: estados do ciclo de vida da OrdemDeServico."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class StatusOrdem(StrEnum):
    """Estados da OS na fase 4 (RFC-004 secao 4.2), na ordem do fluxo feliz.

    As transicoes legais vivem na ``MaquinaDeStatus``. Valores snake_case
    (contrato da API e coluna ``status``).
    """

    RECEBIDA = "recebida"
    EM_DIAGNOSTICO = "em_diagnostico"
    AGUARDANDO_APROVACAO = "aguardando_aprovacao"
    AGUARDANDO_PAGAMENTO = "aguardando_pagamento"
    AGUARDANDO_EXECUCAO = "aguardando_execucao"
    EM_EXECUCAO = "em_execucao"
    FINALIZADA = "finalizada"
    ENTREGUE = "entregue"
    CANCELADA = "cancelada"


# Estados sem transicao de saida: a OS esta encerrada e nao conta como "ativa"
# para o contexto Cliente+Veiculo (desativacao, erasure LGPD). Fonte unica; um
# teste valida o conjunto contra a maquina.
ESTADOS_TERMINAIS: Final[frozenset[StatusOrdem]] = frozenset(
    {StatusOrdem.ENTREGUE, StatusOrdem.CANCELADA}
)
