"""Envio de comandos: o caminho unico que grava o comando na outbox.

``enviar`` publica o comando e devolve o ``Envio`` que a ``Saga`` confere e
registra (RFC-004 secoes 4.6 e 5.2). O ``ContextoDeEnvio`` e o que o envio pede
do ambiente: o prazo tecnico da resposta, o relogio (e a sua politica de
defasagem) e o span da acao no trace da saga (ADR-043). Tudo entra de fora, para
os testes controlarem o tempo e a camada de aplicacao nao importar telemetria.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from src.ordem_servico.aplicacao.saga.modelo import Envio

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import Comando
    from src.compartilhado.aplicacao.unit_of_work import PublicadorDeComandos
    from src.ordem_servico.aplicacao.saga.modelo import DadosDoContrato
    from src.ordem_servico.aplicacao.saga.saga import Saga

_log = structlog.get_logger(__name__)

# Quanto o relogio desta replica pode estar atras do ultimo registro da saga
# (de outra replica ou da API): ate 1 s e jitter e passa sem aviso; acima, o
# aviso mostra a defasagem; acima de 60 s nao e jitter, e o passo e recusado.
_DEFASAGEM_SEM_AVISO: Final = timedelta(seconds=1)
_DEFASAGEM_MAXIMA: Final = timedelta(seconds=60)

# Abre o span ``nome`` como filho do ``traceparent`` da saga (``None``: do span
# corrente), com link para o span corrente.
type SpanDaSaga = Callable[[str, str | None], AbstractContextManager[None]]
# O comando a enviar: o tipo e os ``dados`` do envelope.
type ComandoParaEnviar = tuple[Comando, DadosDoContrato]


class RelogioDefasadoError(Exception):
    """O relogio desta replica esta mais de 60 s atras do ultimo registro da saga."""


def agora() -> datetime:
    """O instante de agora em UTC: o relogio de quem nao injeta outro."""
    return datetime.now(UTC)


def sem_span(_nome: str, _traceparent: str | None) -> AbstractContextManager[None]:
    """Nenhum span: o padrao de quem roda sem tracer."""
    return nullcontext()


def enviar(
    publicador: PublicadorDeComandos,
    comando: ComandoParaEnviar,
    *,
    correlation_id: UUID,
    causation_id: UUID | None = None,
    prazo_resposta_em: datetime | None = None,
) -> Envio:
    """Grava o comando na outbox e devolve o ``Envio`` dele.

    ``causation_id`` e o evento que o causou (``None`` quando a causa e a
    requisicao HTTP, RFC-004 secao 5.2); ``prazo_resposta_em``, o prazo tecnico
    do comando com resposta automatica (secao 4.6).
    """
    tipo, dados = comando
    comando_id = publicador.publicar_comando(
        tipo, dados, correlation_id=correlation_id, causation_id=causation_id
    )
    return Envio(
        tipo=tipo, id=comando_id, dados=dados, prazo_resposta_em=prazo_resposta_em
    )


@dataclass(frozen=True, slots=True)
class ContextoDeEnvio:
    """Prazo de resposta do comando enviado, relogio e span da acao."""

    prazo_resposta: timedelta
    relogio: Callable[[], datetime] = agora
    span: SpanDaSaga = sem_span

    def instante_do_passo(self, saga: Saga, *, tipo: str) -> datetime:
        """O relogio, sem ficar antes do ultimo registro da saga.

        O registro anterior pode vir de outra replica (ou da API) com o relogio
        uns milissegundos a frente, e a saga recusaria o novo como instante que
        volta: ele fica no instante do anterior. Passando de 1 s o aviso mostra a
        defasagem (``tipo`` e o do evento ou do pedido), e passando de 60 s o
        passo e recusado: relogio fora de hora gravaria ``em`` e prazos no futuro.

        Raises:
            RelogioDefasadoError: mais de 60 s atras do ultimo registro.
        """
        instante = self.relogio()
        defasagem = saga.atualizada_em - instante
        if defasagem > _DEFASAGEM_SEM_AVISO:
            _log.warning(
                "saga clock behind last record",
                etapa=saga.etapa.value,
                defasagem_segundos=defasagem.total_seconds(),
                correlation_id=str(saga.ordem_id),
                tipo=tipo,
            )
        if defasagem > _DEFASAGEM_MAXIMA:
            msg = f"relogio {defasagem.total_seconds():.0f} s atras do ultimo registro"
            raise RelogioDefasadoError(msg)
        return max(instante, saga.atualizada_em)

    def enviar(
        self,
        publicador: PublicadorDeComandos,
        comando: ComandoParaEnviar,
        *,
        correlation_id: UUID,
        causation_id: UUID | None = None,
        agora: datetime,
    ) -> Envio:
        """``enviar`` de um comando com resposta automatica: o prazo e o tecnico.

        O prazo conta de ``agora`` (o instante do passo), que a saga confere
        contra o comando.
        """
        return enviar(
            publicador,
            comando,
            correlation_id=correlation_id,
            causation_id=causation_id,
            prazo_resposta_em=agora + self.prazo_resposta,
        )
