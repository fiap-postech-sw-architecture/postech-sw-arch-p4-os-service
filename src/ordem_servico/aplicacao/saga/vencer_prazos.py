"""Caso de uso do processo ``prazos``: vence as sagas sem resposta (RFC-004 secao 4.6).

A cada ciclo, uma transacao por saga: reivindica a de prazo vencido mais antiga
(``SagaRepository.reivindicar_vencida``, que tambem confere que o envio mais
recente ja saiu da outbox), le a OS, decide, grava conferindo a ``versao`` e
comita com o comando na outbox. Com o prazo vencido e reenvios restantes, o
comando sai de novo com id novo (``Saga.reenviar``); esgotados, a etapa linear
inicia a compensacao por ``prazo_tecnico`` e a compensacao vai a
``falha_na_compensacao`` (``Saga.esgotar_prazo``).

Antes de tudo o ciclo confere o relogio do processo contra o do banco (acima de
5 s de desvio, nao age) e a presenca do consumidor do OS
(``PresencaDoConsumidor``): sem consumidor em ``os.eventos`` ou com o broker
fora a resposta pode estar esperando na fila, e o ciclo nao reenvia nem esgota
prazo, so avisa. A saga trabalhada por outra replica (``SKIP LOCKED``) fica para
ela; o consumidor que leu a saga antes perde na conferencia da ``versao`` e
volta pela fila de retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from src.compartilhado.aplicacao.mensageria import ContratoInvalidoError
from src.compartilhado.aplicacao.unit_of_work import PublicadorDeComandos
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.ordem_servico.aplicacao.saga.contexto_de_envio import RelogioDefasadoError
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    GatilhoDeProcesso,
    InvarianteDaSagaError,
    TransicaoDaSagaInvalidaError,
)
from src.ordem_servico.aplicacao.saga.prazos import (
    ATOR_PRAZOS,
    comando_a_reenviar,
    compensacao_do_esgotamento,
)
from src.ordem_servico.dominio.marcos import MarcosDaOrdem

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from datetime import datetime
    from uuid import UUID

    from src.ordem_servico.aplicacao.ports import PresencaDoConsumidor, SagaRepository
    from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository

_log = structlog.get_logger(__name__)

# O quanto o relogio do processo pode destoar do relogio do banco, que grava o
# ``entregue_em`` da outbox. Acima disso o ciclo nao age: o relogio adiantado
# reenvia e esgota antes da hora, sem volta, e o atrasado deixa de achar as
# vencidas. Cobre a latencia da consulta e fica longe dos 30 s do ciclo.
DESVIO_MAXIMO_DO_RELOGIO: Final = timedelta(seconds=5)

# O que recusa a saga de vez: o estado dela, o comando guardado ou o relogio desta
# replica. Repetir no mesmo ciclo nao muda nada: a saga fica de fora dele, o
# erro vai para o log e o gauge de prazo vencido mantem o alerta de saga parada.
_RECUSAS: Final = (
    TransicaoDaSagaInvalidaError,
    InvarianteDaSagaError,
    RelogioDefasadoError,
    ContratoInvalidoError,
    ConflitoDeConcorrenciaException,
)


class TransacaoDosPrazos(PublicadorDeComandos, Protocol):
    """Uma transacao do processo ``prazos``: os repositorios, a outbox e o commit.

    Aberta e fechada pelo ``with``: o fim sem ``commit`` desfaz a saga, o
    comando e o que mais a transacao gravou.
    """

    @property
    def ordens(self) -> OrdemDeServicoRepository:
        """Os repositorios da OS sobre a transacao."""
        pass

    @property
    def sagas(self) -> SagaRepository:
        """Os repositorios da saga sobre a transacao."""
        pass

    def agora_do_banco(self) -> datetime:
        """O relogio do banco (``clock_timestamp()``), o que grava o ``entregue_em``."""
        pass

    def commit(self) -> None:
        """Comita a saga e os comandos gravados na outbox, juntos."""
        pass


type AbrirTransacao = Callable[[], AbstractContextManager[TransacaoDosPrazos]]


class Vencimento(StrEnum):
    """O que o ciclo fez com uma saga de prazo vencido."""

    REENVIADA = "reenviada"
    ESGOTADA = "esgotada"
    RECUSADA = "recusada"


@dataclass(frozen=True, slots=True)
class ResultadoDoCiclo:
    """O que um ciclo fez: sagas reenviadas, esgotadas e recusadas, ou a pausa."""

    pausado: bool = False
    reenviadas: int = 0
    esgotadas: int = 0
    recusadas: int = 0


class VencerPrazos:
    """Um ciclo do ``prazos``: confere o consumidor e vence cada saga sem resposta."""

    def __init__(
        self,
        *,
        abrir: AbrirTransacao,
        presenca: PresencaDoConsumidor,
        contexto: ContextoDeEnvio,
        max_reenvios: int,
    ) -> None:
        self._abrir = abrir
        self._presenca = presenca
        self._contexto = contexto
        self._max_reenvios = max_reenvios

    def executar(
        self, ao_tratar: Callable[[], None] = lambda: None
    ) -> ResultadoDoCiclo:
        """Vence todas as sagas de prazo vencido; ``ao_tratar`` roda a cada uma.

        ``ao_tratar`` e o heartbeat do processo, tocado a cada saga (a liveness
        tolera 120 s). Erro de banco sobe para o laco do processo; a saga que o
        dominio recusa fica de fora do resto do ciclo.
        """
        if self._relogio_fora_do_banco():
            return ResultadoDoCiclo(pausado=True)
        if not self._presenca.ha_consumidor():
            _log.warning("saga deadlines paused: the os.eventos consumer is away")
            return ResultadoDoCiclo(pausado=True)
        recusadas: set[UUID] = set()
        vencimentos: list[Vencimento] = []
        while (vencimento := self._vencer_uma(recusadas)) is not None:
            vencimentos.append(vencimento)
            ao_tratar()
        return ResultadoDoCiclo(
            reenviadas=vencimentos.count(Vencimento.REENVIADA),
            esgotadas=vencimentos.count(Vencimento.ESGOTADA),
            recusadas=len(recusadas),
        )

    def _relogio_fora_do_banco(self) -> bool:
        """Confere o relogio do processo contra o do banco, uma vez por ciclo.

        Adiantado ou atrasado alem de ``DESVIO_MAXIMO_DO_RELOGIO``, o ciclo nao
        age: o prazo que o ``prazos`` mede e o do relogio do banco.
        """
        with self._abrir() as transacao:
            banco = transacao.agora_do_banco()
        desvio = abs(self._contexto.relogio() - banco)
        if desvio <= DESVIO_MAXIMO_DO_RELOGIO:
            return False
        _log.error(
            "clock skew too large: saga deadlines skipped",
            desvio_s=round(desvio.total_seconds(), 1),
            limite_s=DESVIO_MAXIMO_DO_RELOGIO.total_seconds(),
        )
        return True

    def _vencer_uma(self, recusadas: set[UUID]) -> Vencimento | None:
        """Uma saga numa transacao; ``None`` quando nenhuma venceu."""
        ordem_id: UUID | None = None
        try:
            with self._abrir() as transacao:
                sagas = transacao.sagas
                saga = sagas.reivindicar_vencida(
                    self._contexto.relogio(),
                    self._contexto.prazo_resposta,
                    ignorar=recusadas,
                )
                if saga is None:
                    return None
                ordem_id = saga.ordem_id
                # O comando e o contexto que a saga passa a guardar nascem no span
                # do prazo, filho do trace dela (ADR-043).
                with self._contexto.span("saga prazo", saga.traceparent):
                    vencimento = self._tratar(transacao, saga)
                    sagas.salvar(saga)
                    transacao.commit()
                return vencimento
        except _RECUSAS as exc:
            if ordem_id is None:
                raise
            _log.error(
                "saga deadline refused",
                correlation_id=str(ordem_id),
                erro=type(exc).__name__,
            )
            recusadas.add(ordem_id)
            return Vencimento.RECUSADA

    def _tratar(self, transacao: TransacaoDosPrazos, saga: Saga) -> Vencimento:
        """Reenvia o comando em voo, ou esgota o prazo dele; a transacao comita."""
        etapa, comando = saga.etapa, saga.tipo_do_comando_em_voo
        contexto = {"correlation_id": str(saga.ordem_id), "etapa": etapa.value}
        if saga.reenvios < self._max_reenvios:
            agora = self._contexto.instante_do_passo(
                saga, tipo=GatilhoDeProcesso.REENVIO
            )
            envio = self._contexto.enviar(
                transacao,
                comando_a_reenviar(saga),
                correlation_id=saga.ordem_id,
                agora=agora,
            )
            saga.reenviar(envio, agora=agora, ator=ATOR_PRAZOS)
            _log.info(
                "saga command resent",
                comando=comando,
                reenvio=saga.reenvios,
                **contexto,
            )
            return Vencimento.REENVIADA
        agora = self._contexto.instante_do_passo(
            saga, tipo=GatilhoDeProcesso.PRAZO_ESGOTADO
        )
        ordem = transacao.ordens.obter_por_id(saga.ordem_id)
        if ordem is None:
            msg = "saga sem a OS dela"
            raise InvarianteDaSagaError(msg)
        marcos = MarcosDaOrdem.da_ordem(ordem)
        primeira = compensacao_do_esgotamento(saga, marcos, agora)
        compensacao = (
            self._contexto.enviar(
                transacao, primeira, correlation_id=saga.ordem_id, agora=agora
            )
            if primeira is not None
            else None
        )
        saga.esgotar_prazo(marcos, agora=agora, ator=ATOR_PRAZOS, envio=compensacao)
        parada = saga.etapa is EtapaSaga.FALHA_NA_COMPENSACAO
        (_log.warning if parada else _log.info)(
            "saga deadline exhausted",
            comando=comando,
            etapa_nova=saga.etapa.value,
            motivo=saga.motivo,
            **({"falha": saga.falha} if parada else {}),
            **contexto,
        )
        return Vencimento.ESGOTADA
