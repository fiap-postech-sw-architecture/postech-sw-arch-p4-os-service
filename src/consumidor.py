"""Processo consumidor da fila ``os.eventos``: ``python -m src.consumidor``.

Roda na mesma imagem da API, com outro comando. O despachante liga cada evento
que o OS consome (operacao de recebimento da fila ``os.eventos`` no AsyncAPI) ao
orquestrador da saga, montado sobre a transacao da mensagem: o consumidor comita
efeito, comandos e ``mensagens_processadas`` juntos (ADR-036).
"""

from __future__ import annotations

import threading
from datetime import timedelta
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from opentelemetry import trace

from src.compartilhado.infraestrutura.database import criar_session_factory
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    Consumidor,
    Handler,
)
from src.compartilhado.infraestrutura.mensageria.processo import (
    instalar_sinais,
    inteiro_do_ambiente,
    preparar,
    subir_metricas,
)
from src.compartilhado.infraestrutura.observability import criar_tracer
from src.ordem_servico.aplicacao.saga.orquestrador import (
    EventoAdiantadoError,
    OrquestradorDaSaga,
)
from src.ordem_servico.aplicacao.saga.saga import ETAPA_ESPERADA
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
    SagaSQLAlchemyRepository,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.compartilhado.aplicacao.mensageria import Desfecho, MensagemRecebida
    from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem

# Espera por resposta a comando, gravada pela saga no envio (RFC-004 secao 10.3).
_PRAZO_RESPOSTA_PADRAO_S: Final = 120


def tratar_evento_da_saga(
    mensagem: MensagemRecebida,
    transacao: TransacaoDaMensagem,
    *,
    prazo_resposta: timedelta,
) -> Desfecho:
    """Orquestrador da saga sobre a transacao da mensagem (sem commit aqui).

    O span ``process <tipo>`` do consumo ganha a etapa antes e depois do evento
    e o desfecho (processada, ignorada ou adiantada), ADR-043.
    """
    span = trace.get_current_span()
    orquestrador = OrquestradorDaSaga(
        ordens=OrdemDeServicoSQLAlchemyRepository(session=transacao.session),
        sagas=SagaSQLAlchemyRepository(session=transacao.session),
        publicador=transacao,
        prazo_resposta=prazo_resposta,
    )
    try:
        tratamento = orquestrador.tratar(mensagem)
    except EventoAdiantadoError as exc:
        span.set_attributes(
            {
                "pytstop.saga.etapa": exc.etapa.value,
                "pytstop.saga.desfecho": "adiantada",
            }
        )
        raise
    span.set_attributes(
        {
            "pytstop.saga.etapa": tratamento.etapa.value,
            "pytstop.saga.etapa_nova": tratamento.etapa_nova.value,
            "pytstop.saga.desfecho": tratamento.desfecho.value,
        }
    )
    return tratamento.desfecho


def montar_despachante(prazo_resposta: timedelta) -> Mapping[str, Handler]:
    """Os 23 eventos que o OS consome, todos para o orquestrador da saga."""
    tratar = partial(tratar_evento_da_saga, prazo_resposta=prazo_resposta)
    return MappingProxyType(dict.fromkeys(ETAPA_ESPERADA, tratar))


def main() -> None:
    """Sobe o consumidor e trata ``os.eventos`` ate o SIGTERM."""
    parar = threading.Event()
    instalar_sinais(parar)
    engine, parametros = preparar("consumidor")
    try:
        prazo_resposta = timedelta(
            seconds=inteiro_do_ambiente(
                "SAGA_PRAZO_RESPOSTA_SEGUNDOS", _PRAZO_RESPOSTA_PADRAO_S, minimo=1
            )
        )
        consumidor = Consumidor(
            session_factory=criar_session_factory(engine),
            parametros=parametros,
            despachante=montar_despachante(prazo_resposta),
            tracer=criar_tracer("consumidor"),
        )
        subir_metricas()
        consumidor.executar(parar)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
