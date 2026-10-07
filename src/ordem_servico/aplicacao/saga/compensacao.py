"""Compensacoes da saga (RFC-004 secoes 4.4 e 4.7; ADR-035 "Compensacoes").

O plano e funcao pura do que a saga ja fez quando a compensacao comeca: desfaz
cada passo concluido ou em voo (o comando saiu e a resposta ainda nao chegou,
entao o efeito dele tambem se desfaz), na ordem fixa ``ORDEM_DAS_COMPENSACOES``,
menos o passo que a falha de negocio ja encerrou no participante. O
``DescartarDiagnostico`` entra sempre: o ``SolicitarDiagnostico`` nao tem
resposta automatica (RFC-004 secao 4.3). As compensacoes saem uma por vez,
cada uma esperando a resposta (``RESPOSTA_DA_COMPENSACAO``).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.modelo import MotivoCompensacao

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_M = MotivoCompensacao

# Pessimistic view (RFC-004 secao 4.5): a OS sai da fila antes de o pagamento
# ser devolvido.
ORDEM_DAS_COMPENSACOES: Final = (
    Comando.CANCELAR_EXECUCAO,
    Comando.ESTORNAR_PAGAMENTO,
    Comando.LIBERAR_RESERVA,
    Comando.CANCELAR_ORCAMENTO,
    Comando.DESCARTAR_DIAGNOSTICO,
)
# O passo T que cada compensacao desfaz (RFC-004 secao 4.3).
_PASSO_DESFEITO: Final[Mapping[Comando, str]] = MappingProxyType(
    {
        Comando.CANCELAR_EXECUCAO: "T7",
        Comando.ESTORNAR_PAGAMENTO: "T6",
        Comando.LIBERAR_RESERVA: "T5",
        Comando.CANCELAR_ORCAMENTO: "T3",
        Comando.DESCARTAR_DIAGNOSTICO: "T2",
    }
)
# O passo de cada comando que pode estar em voo quando a compensacao comeca.
_PASSO_DO_COMANDO: Final[Mapping[str, str]] = MappingProxyType(
    {
        Comando.GERAR_ORCAMENTO.value: "T3",
        Comando.RESERVAR_PECAS.value: "T5",
        Comando.SOLICITAR_PAGAMENTO.value: "T6",
        Comando.AGENDAR_EXECUCAO.value: "T7",
    }
)
# Falha de negocio: o motivo da compensacao e o passo que o participante ja
# encerrou ao publica-la (orcamento recusado nao recebe CancelarOrcamento, nem
# pagamento recusado EstornarPagamento: RN-024 e RN-025).
MOTIVO_DA_FALHA: Final[Mapping[str, MotivoCompensacao]] = MappingProxyType(
    {
        "GeracaoDeOrcamentoFalhou": _M.GERACAO_FALHOU,
        "OrcamentoRecusado": _M.ORCAMENTO_RECUSADO,
        "OrcamentoExpirado": _M.ORCAMENTO_EXPIRADO,
        "ReservaDePecasFalhou": _M.RESERVA_FALHOU,
        "PagamentoRecusado": _M.PAGAMENTO_RECUSADO,
        "PagamentoExpirado": _M.PAGAMENTO_EXPIRADO,
    }
)
_ENCERRADO_PELA_FALHA: Final[Mapping[MotivoCompensacao, str]] = MappingProxyType(
    {
        _M.GERACAO_FALHOU: "T3",
        _M.ORCAMENTO_RECUSADO: "T3",
        _M.ORCAMENTO_EXPIRADO: "T3",
        _M.RESERVA_FALHOU: "T5",
        _M.PAGAMENTO_RECUSADO: "T6",
        _M.PAGAMENTO_EXPIRADO: "T6",
    }
)
# A compensacao a que cada evento responde. As tres respostas do Billing ao
# EstornarPagamento so valem casadas pelo causation_id: o PagamentoEstornado do
# estorno automatico tambem chega com a compensacao pendente (RFC-004 secao 4.4).
RESPOSTA_DA_COMPENSACAO: Final[Mapping[str, Comando]] = MappingProxyType(
    {
        "ExecucaoCancelada": Comando.CANCELAR_EXECUCAO,
        "PagamentoCancelado": Comando.ESTORNAR_PAGAMENTO,
        "PagamentoEstornado": Comando.ESTORNAR_PAGAMENTO,
        "EstornoDePagamentoFalhou": Comando.ESTORNAR_PAGAMENTO,
        "ReservaLiberada": Comando.LIBERAR_RESERVA,
        "OrcamentoCancelado": Comando.CANCELAR_ORCAMENTO,
        "DiagnosticoDescartado": Comando.DESCARTAR_DIAGNOSTICO,
    }
)
# Motivo da OS cancelada quando o atendente nao escreveu um (RFC-004 secao
# 4.4): texto para o usuario, com acento, como os rotulos de situacao.
ROTULO_DO_MOTIVO: Final[Mapping[MotivoCompensacao, str]] = MappingProxyType(
    {
        _M.ORCAMENTO_RECUSADO: "Orçamento recusado pelo cliente",
        _M.ORCAMENTO_EXPIRADO: "Orçamento expirado sem decisão do cliente",
        _M.GERACAO_FALHOU: "Falha na geração do orçamento",
        _M.RESERVA_FALHOU: "Peças indisponíveis no estoque",
        _M.PAGAMENTO_RECUSADO: "Pagamento recusado",
        _M.PAGAMENTO_EXPIRADO: "Pagamento expirado",
        _M.CANCELAMENTO: "Cancelada pelo atendimento",
        _M.PRAZO_TECNICO: "Sem resposta de um serviço no prazo",
    }
)


def plano_de_compensacao(
    concluidos: Iterable[str], em_voo: str | None, motivo: MotivoCompensacao
) -> tuple[Comando, ...]:
    """As compensacoes da saga, na ordem em que saem (RFC-004 secao 4.4).

    ``concluidos`` sao os passos T concluidos (T3, T5, T6, T7) e ``em_voo`` o
    tipo do comando com prazo que espera resposta. A falha de negocio tira o
    passo que encerrou: ``geracao_falhou`` e as do orcamento tiram o T3,
    ``reserva_falhou`` o T5 e as do pagamento o T6.
    """
    a_desfazer = {"T2", *concluidos, _PASSO_DO_COMANDO.get(em_voo or "")}
    a_desfazer.discard(_ENCERRADO_PELA_FALHA.get(motivo))
    return tuple(
        comando
        for comando in ORDEM_DAS_COMPENSACOES
        if _PASSO_DESFEITO[comando] in a_desfazer
    )
