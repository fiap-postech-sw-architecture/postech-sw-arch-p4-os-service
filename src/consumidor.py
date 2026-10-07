"""Processo consumidor da fila ``os.eventos``: ``python -m src.consumidor``.

Roda na mesma imagem da API, com outro comando. O ``DESPACHANTE`` liga cada
evento que o OS consome (operacao de recebimento da fila ``os.eventos`` no
AsyncAPI) ao seu handler. Os handlers da saga entram com o orquestrador; ate
la, cada tipo so registra o recebimento.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Final

import structlog

from src.compartilhado.aplicacao.mensageria import Desfecho, MensagemRecebida
from src.compartilhado.infraestrutura.database import criar_session_factory
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
    Handler,
)
from src.compartilhado.infraestrutura.mensageria.processo import (
    instalar_sinais,
    preparar,
    subir_metricas,
)
from src.compartilhado.infraestrutura.observability import criar_tracer

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem

_log = structlog.get_logger(__name__)


def registrar_recebimento(
    mensagem: MensagemRecebida, _transacao: TransacaoDaMensagem
) -> Desfecho:
    """Handler provisorio: so registra o recebimento (sem texto livre nem PII)."""
    _log.info(
        "event received",
        tipo=mensagem.tipo,
        message_id=str(mensagem.id),
        correlation_id=str(mensagem.correlation_id),
        causation_id=str(mensagem.causation_id) if mensagem.causation_id else None,
    )
    return Desfecho.PROCESSADA


DESPACHANTE: Final[Mapping[str, Handler]] = {
    "DiagnosticoIniciado": registrar_recebimento,
    "DiagnosticoConcluido": registrar_recebimento,
    "DiagnosticoDescartado": registrar_recebimento,
    "OrcamentoGerado": registrar_recebimento,
    "GeracaoDeOrcamentoFalhou": registrar_recebimento,
    "OrcamentoAprovado": registrar_recebimento,
    "OrcamentoRecusado": registrar_recebimento,
    "OrcamentoExpirado": registrar_recebimento,
    "OrcamentoCancelado": registrar_recebimento,
    "PecasReservadas": registrar_recebimento,
    "ReservaDePecasFalhou": registrar_recebimento,
    "ReservaLiberada": registrar_recebimento,
    "PagamentoSolicitado": registrar_recebimento,
    "PagamentoConfirmado": registrar_recebimento,
    "PagamentoRecusado": registrar_recebimento,
    "PagamentoExpirado": registrar_recebimento,
    "PagamentoEstornado": registrar_recebimento,
    "EstornoDePagamentoFalhou": registrar_recebimento,
    "PagamentoCancelado": registrar_recebimento,
    "ExecucaoAgendada": registrar_recebimento,
    "ExecucaoCancelada": registrar_recebimento,
    "ExecucaoIniciada": registrar_recebimento,
    "ExecucaoFinalizada": registrar_recebimento,
}


def main() -> None:
    """Sobe o consumidor e trata ``os.eventos`` ate o SIGTERM."""
    parar = threading.Event()
    instalar_sinais(parar)
    engine, parametros = preparar("consumidor")
    try:
        consumidor = Consumidor(
            session_factory=criar_session_factory(engine),
            parametros=parametros,
            despachante=DESPACHANTE,
            tracer=criar_tracer("consumidor"),
            config=ConfigConsumidor.do_ambiente(),
        )
        subir_metricas()
        consumidor.executar(parar)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
