"""Maquina de transicoes do ``StatusOrdem`` para a Ordem de Servico."""

from __future__ import annotations

from typing import ClassVar

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.ordem_servico.dominio.status import StatusOrdem


class MaquinaDeStatus:
    """Allow-list das transicoes legais do ``StatusOrdem`` (RFC-004 secao 4.2).

    Fluxo feliz: RECEBIDA -> EM_DIAGNOSTICO -> AGUARDANDO_APROVACAO ->
    AGUARDANDO_PAGAMENTO -> AGUARDANDO_EXECUCAO -> EM_EXECUCAO -> FINALIZADA
    -> ENTREGUE. CANCELADA a partir de qualquer estado anterior a EM_EXECUCAO:
    o inicio da execucao fisica e o pivot da saga (RFC-004 secao 4.4), depois dele
    nao ha compensacao. ENTREGUE e CANCELADA sao terminais.
    """

    _TRANSICOES: ClassVar[dict[StatusOrdem, frozenset[StatusOrdem]]] = {
        StatusOrdem.RECEBIDA: frozenset(
            {StatusOrdem.EM_DIAGNOSTICO, StatusOrdem.CANCELADA}
        ),
        StatusOrdem.EM_DIAGNOSTICO: frozenset(
            {StatusOrdem.AGUARDANDO_APROVACAO, StatusOrdem.CANCELADA}
        ),
        StatusOrdem.AGUARDANDO_APROVACAO: frozenset(
            {StatusOrdem.AGUARDANDO_PAGAMENTO, StatusOrdem.CANCELADA}
        ),
        StatusOrdem.AGUARDANDO_PAGAMENTO: frozenset(
            {StatusOrdem.AGUARDANDO_EXECUCAO, StatusOrdem.CANCELADA}
        ),
        StatusOrdem.AGUARDANDO_EXECUCAO: frozenset(
            {StatusOrdem.EM_EXECUCAO, StatusOrdem.CANCELADA}
        ),
        StatusOrdem.EM_EXECUCAO: frozenset({StatusOrdem.FINALIZADA}),
        StatusOrdem.FINALIZADA: frozenset({StatusOrdem.ENTREGUE}),
        StatusOrdem.ENTREGUE: frozenset(),
        StatusOrdem.CANCELADA: frozenset(),
    }

    def transicoes_validas(self, status: StatusOrdem) -> frozenset[StatusOrdem]:
        """Transicoes legais a partir de ``status`` (frozenset: nao muta a tabela)."""
        return self._TRANSICOES[status]

    def validar_transicao(self, de: StatusOrdem, para: StatusOrdem) -> None:
        """Levanta ``TransicaoStatusInvalidaException`` se a transicao nao for legal.

        A mensagem lista as transicoes validas a partir de ``de``.
        """
        validas = self.transicoes_validas(de)
        if para not in validas:
            raise TransicaoStatusInvalidaException(
                mensagem=(
                    f"Transicao invalida de {de.value} para {para.value}; "
                    f"transicoes validas a partir de {de.value}: "
                    f"{sorted(s.value for s in validas)}"
                )
            )
