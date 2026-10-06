"""Vocabulario de situacao da OS — rotulo de apresentacao de cada status.

Os valores persistidos de ``StatusOrdem`` sao snake_case; este modulo traduz
para o rotulo exibido na API (campo ``situacao``). Os rotulos tem acento de
proposito: sao texto para o usuario, nao identificadores.
"""

from __future__ import annotations

from src.ordem_servico.dominio.status import StatusOrdem

_SITUACAO_POR_STATUS: dict[StatusOrdem, str] = {
    StatusOrdem.RECEBIDA: "Recebida",
    StatusOrdem.EM_DIAGNOSTICO: "Em diagnóstico",
    StatusOrdem.AGUARDANDO_APROVACAO: "Aguardando aprovação",
    StatusOrdem.AGUARDANDO_PAGAMENTO: "Aguardando pagamento",
    StatusOrdem.AGUARDANDO_EXECUCAO: "Aguardando execução",
    StatusOrdem.EM_EXECUCAO: "Em execução",
    StatusOrdem.FINALIZADA: "Finalizada",
    StatusOrdem.ENTREGUE: "Entregue",
    StatusOrdem.CANCELADA: "Cancelada",
}


def situacao_de(status: StatusOrdem) -> str:
    """Rotulo de apresentacao do status (funcao total sobre ``StatusOrdem``)."""
    return _SITUACAO_POR_STATUS[status]
