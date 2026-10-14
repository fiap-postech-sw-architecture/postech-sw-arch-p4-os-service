"""Caso de uso do processo ``prazos``: vence as sagas sem resposta (RFC-004 secao 4.6).

A cada ciclo, uma transacao por saga: reivindica a de prazo vencido mais antiga
(``SagaRepository.reivindicar_vencida``, que tambem confere que o envio mais
recente ja saiu da outbox), decide, grava conferindo a ``versao`` e comita com
o comando na outbox. Com o prazo vencido e reenvios restantes, o
comando sai de novo com id novo (``Saga.reenviar``); esgotados, a etapa linear
inicia a compensacao por ``prazo_tecnico`` e a compensacao vai a
``falha_na_compensacao`` (``Saga.esgotar_prazo``).

Antes de tudo o ciclo confere o relogio do processo contra o do banco (acima de
5 s de desvio, nao age). Antes de cada saga confere a fila do consumidor do OS
(``PresencaDoConsumidor``): sem consumidor em ``os.eventos``, com mensagem
pronta nela (o consumidor voltou e ainda nao drenou as respostas) ou com o
broker fora, a resposta pode estar esperando, e o ciclo nao reenvia nem esgota
prazo, so avisa; a fila que muda no meio do ciclo interrompe o resto. O ciclo
trata no maximo ``SAGAS_POR_CICLO`` sagas, e o ciclo seguinte segue dali; o
SIGTERM e visto entre duas sagas. A saga
trabalhada por outra replica (``SKIP LOCKED``) fica para ela; o consumidor que
leu a saga antes perde na conferencia da ``versao`` e volta pela fila de retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import structlog

from src.compartilhado.aplicacao.diagnostico import onde
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

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from src.ordem_servico.aplicacao.ports import (
        AbrirTransacao,
        PresencaDoConsumidor,
        TransacaoDosPrazos,
    )
    from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
    from src.ordem_servico.aplicacao.saga.saga import Saga

_log = structlog.get_logger(__name__)

# O quanto o relogio do processo pode destoar do relogio do banco, que grava o
# ``entregue_em`` da outbox. Acima disso o ciclo nao age: o relogio adiantado
# reenvia e esgota antes da hora, sem volta, e o atrasado deixa de achar as
# vencidas. Cobre a latencia da consulta e fica longe dos 30 s do ciclo.
DESVIO_MAXIMO_DO_RELOGIO: Final = timedelta(seconds=5)

# O teto de sagas por ciclo: depois de uma parada longa, a rajada de reenvios e
# repartida em ciclos, com a fila do consumidor conferida a cada saga.
SAGAS_POR_CICLO: Final = 100

# As recusas do dominio dizem so a etapa e o tipo do comando, sem dado de cliente:
# o log leva a mensagem delas. De qualquer outro erro da saga so o tipo e o
# ``arquivo:linha`` vao ao log.
_RECUSAS_DO_DOMINIO: Final = (TransicaoDaSagaInvalidaError, InvarianteDaSagaError)


class DesfechoDoPrazo(StrEnum):
    """O que o ciclo fez com uma saga de prazo vencido."""

    REENVIADA = "reenviada"
    ESGOTADA = "esgotada"
    RECUSADA = "recusada"


@dataclass(frozen=True, slots=True)
class ResultadoDoCiclo:
    """O que um ciclo fez: sagas reenviadas, esgotadas e recusadas, ou a pausa.

    ``pausado`` e o ciclo que parou sem agir mais (relogio fora do banco, fila do
    consumidor sem estar em dia); o que tratou antes da pausa fica nos contadores.
    """

    pausado: bool = False
    reenviadas: int = 0
    esgotadas: int = 0
    recusadas: int = 0


class VencerPrazos:
    """Um ciclo do ``prazos``: confere o relogio e o consumidor e vence as sagas."""

    def __init__(
        self,
        *,
        abrir: AbrirTransacao,
        presenca: PresencaDoConsumidor,
        contexto: ContextoDeEnvio,
        max_reenvios: int,
        banco_fora: tuple[type[Exception], ...],
    ) -> None:
        self._abrir = abrir
        self._presenca = presenca
        self._contexto = contexto
        self._max_reenvios = max_reenvios
        self._banco_fora = banco_fora

    def executar(
        self,
        ao_tratar: Callable[[], None] = lambda: None,
        *,
        parar: Callable[[], bool] = lambda: False,
    ) -> ResultadoDoCiclo:
        """Vence as sagas de prazo vencido, ate ``SAGAS_POR_CICLO``.

        ``ao_tratar`` roda a cada saga: e o heartbeat do processo (a liveness
        tolera 120 s). ``parar`` e o SIGTERM do processo: visto entre duas sagas,
        o ciclo termina a que esta em curso e sai. O banco fora sobe para o laco
        do processo; a saga que falha por outro motivo (o dominio a recusa, o
        comando guardado esta fora do contrato, um erro do banco que se repetiria)
        fica de fora do resto do ciclo, e as outras seguem.
        """
        if self._relogio_fora_do_banco():
            return ResultadoDoCiclo(pausado=True)
        recusadas: set[UUID] = set()
        desfechos: list[DesfechoDoPrazo] = []
        pausado = False
        while len(desfechos) < SAGAS_POR_CICLO and not parar():
            if not self._fila_em_dia():
                pausado = True
                break
            desfecho = self._vencer_uma(recusadas)
            if desfecho is None:
                break
            desfechos.append(desfecho)
            ao_tratar()
        return ResultadoDoCiclo(
            pausado=pausado,
            reenviadas=desfechos.count(DesfechoDoPrazo.REENVIADA),
            esgotadas=desfechos.count(DesfechoDoPrazo.ESGOTADA),
            recusadas=len(recusadas),
        )

    def _fila_em_dia(self) -> bool:
        """Consumidor de pe e sem resposta esperando em ``os.eventos``."""
        fila = self._presenca.situacao_da_fila()
        if fila is not None and fila.em_dia:
            return True
        if fila is None:
            motivo = "broker_fora"
        elif fila.consumidores == 0:
            motivo = "sem_consumidor"
        else:
            motivo = "respostas_na_fila"
        _log.warning(
            "saga deadlines paused",
            motivo=motivo,
            prontas=None if fila is None else fila.prontas,
        )
        return False

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

    def _vencer_uma(self, recusadas: set[UUID]) -> DesfechoDoPrazo | None:
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
                    desfecho = self._tratar(transacao, saga)
                    sagas.salvar(saga)
                    transacao.commit()
                return desfecho
        except self._banco_fora:
            # Banco fora nao e defeito da saga: o ciclo falha e o laco tenta de novo.
            raise
        except Exception as exc:  # a saga com defeito sai do ciclo, as outras seguem
            if ordem_id is None:
                raise
            # Repetir no mesmo ciclo nao muda nada, e a saga de prazo mais antigo
            # seria sempre a primeira: ela fica de fora dele, o erro vai ao log e o
            # gauge de prazo vencido mantem o alerta de saga parada.
            detalhe = (
                {"motivo": str(exc)}
                if isinstance(exc, _RECUSAS_DO_DOMINIO)
                else {"onde": onde(exc)}
            )
            _log.error(
                "saga deadline refused",
                correlation_id=str(ordem_id),
                erro=type(exc).__name__,
                **detalhe,
            )
            recusadas.add(ordem_id)
            return DesfechoDoPrazo.RECUSADA

    def _tratar(self, transacao: TransacaoDosPrazos, saga: Saga) -> DesfechoDoPrazo:
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
            saga.reenviar(
                envio, agora=agora, ator=ATOR_PRAZOS, maximo=self._max_reenvios
            )
            _log.info(
                "saga command resent",
                comando=comando,
                reenvio=saga.reenvios,
                **contexto,
            )
            return DesfechoDoPrazo.REENVIADA
        agora = self._contexto.instante_do_passo(
            saga, tipo=GatilhoDeProcesso.PRAZO_ESGOTADO
        )
        primeira = compensacao_do_esgotamento(saga, agora, self._max_reenvios)
        compensacao = (
            self._contexto.enviar(
                transacao, primeira, correlation_id=saga.ordem_id, agora=agora
            )
            if primeira is not None
            else None
        )
        saga.esgotar_prazo(
            agora=agora, ator=ATOR_PRAZOS, maximo=self._max_reenvios, envio=compensacao
        )
        parada = saga.etapa is EtapaSaga.FALHA_NA_COMPENSACAO
        (_log.warning if parada else _log.info)(
            "saga deadline exhausted",
            comando=comando,
            etapa_nova=saga.etapa.value,
            motivo=saga.motivo,
            **({"falha": saga.falha} if parada else {}),
            **contexto,
        )
        return DesfechoDoPrazo.ESGOTADA
