"""Relay da outbox para o RabbitMQ (ADR-036; o relay do p3 com destino AMQP).

Sem conexao com o broker o relay nao reivindica linhas e reconecta com o
backoff da ``ConexaoDoProcesso``: a queda do broker nao conta tentativa de
nenhuma linha, e com a conexao bloqueada por alarme de recursos (o
``Connection.Blocked``) ele para de reivindicar ate o desbloqueio; o timeout do
bloqueio derruba a conexao, como uma queda. Conectado, drena a outbox em lotes
de transacoes curtas, sem transacao aberta durante o publish (``outbox.py``):
claim com lease, renovacao do lease antes de publicar e o desfecho gravado so
se a linha ainda for desta replica.

A publicacao usa publisher confirms e ``mandatory``: a linha so vira
``entregue`` depois do confirm. Devolvida (sem fila para a routing key),
recusada (nack) ou com o canal fechado pelo broker, a linha conta tentativa,
com os atrasos do relay do p3, ate ``dead`` na quinta falha. Entre lotes o
relay espera o ``NOTIFY outbox_novo`` com poll de seguranca e, uma vez por hora,
apaga em lotes as linhas entregues ha mais de 7 dias (RFC-004 secao 5.4) e as
``dead`` ha mais de 30.

Trace (ADR-043): cada publicacao abre um span PRODUCER filho do contexto
gravado na linha e leva o contexto desse span nos headers; o laco ocioso nao
abre span.
"""

from __future__ import annotations

import contextlib
import json
import select
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from opentelemetry.trace import SpanKind, StatusCode
from pika.exceptions import ChannelClosedByBroker, NackError, UnroutableError
from prometheus_client import Counter, Gauge
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.aplicacao.mensageria import ContratoInvalidoError
from src.compartilhado.infraestrutura.database import tempo_de_conexao
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.mensageria.outbox import (
    ATRASOS_S,
    LinhaDaOutbox,
    Outbox,
)
from src.compartilhado.infraestrutura.mensageria.processo import (
    DIRETORIO_DE_SAUDE,
    INTERVALO_DE_LIMPEZA_S,
    Agenda,
    Sinalizador,
    inteiro_do_ambiente,
    numero_do_ambiente,
    onde,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
    contexto_dos_cabecalhos,
    span_de_mensagem,
)
from src.compartilhado.infraestrutura.outbox_mapping import CANAL_NOTIFY

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    import pika
    from opentelemetry.trace import Tracer
    from sqlalchemy import Engine

_log = structlog.get_logger(__name__)

# Metricas do relay (ADR-043), no registro padrao do prometheus_client que o
# /metrics do processo serve. So o relay importa este modulo; os nomes herdados
# do p3 (`outbox_*`) ficam sem o prefixo `pytstop_`.
MENSAGENS_PUBLICADAS: Final = Counter(
    "pytstop_mensagens_publicadas_total",
    "Mensagens publicadas pelo relay e confirmadas pelo broker, por tipo.",
    ["tipo"],
)
OUTBOX_PENDENTES: Final = Gauge(
    "outbox_pendentes", "Linhas da outbox esperando publicacao (status pendente)."
)
OUTBOX_DEAD: Final = Gauge(
    "outbox_dead", "Linhas da outbox que esgotaram as tentativas (status dead)."
)

_POLL_PADRAO_S: Final = 5.0
_LOTE_PADRAO: Final = 10
_LEASE_PADRAO_S: Final = 60
# O lease cobre o publish bloqueado por alarme do broker (30 s) e a marcacao
# da linha (statement_timeout de 15 s): abaixo disso, outra replica publicaria
# de novo cada linha presa num alarme.
_LEASE_MINIMO_S: Final = 45
# Keepalives TCP da conexao dedicada de LISTEN: um peer que sumiu em silencio
# e detectado em cerca de 60 s, em vez de deixar o relay surdo ao NOTIFY.
_KEEPALIVES: Final = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


class _BrokerIndisponivelError(Exception):
    """A conexao com o broker caiu no meio do trabalho (nao e falha da linha)."""


@dataclass(frozen=True, slots=True)
class ConfigRelay:
    """Ajustes do relay; ``do_ambiente`` le os de producao das variaveis."""

    poll_s: float = _POLL_PADRAO_S
    lote: int = _LOTE_PADRAO
    # Renovado antes de cada publicacao: cobre o publish bloqueado por alarme
    # do broker (30 s). Passou dele (broker mudo, ate cerca de 70 s), outra
    # replica pode publicar a linha de novo, e o consumidor descarta a copia
    # pelo id; o fencing impede que as duas gravem o desfecho.
    lease: timedelta = timedelta(seconds=_LEASE_PADRAO_S)
    # Um atraso para cada falha antes da que leva a `dead`.
    atrasos_s: tuple[float, ...] = ATRASOS_S
    diretorio_de_saude: Path = DIRETORIO_DE_SAUDE

    @classmethod
    def do_ambiente(cls) -> ConfigRelay:
        """Le ``OUTBOX_POLL_SEGUNDOS``, ``OUTBOX_LOTE`` e ``OUTBOX_LEASE_SEGUNDOS``.

        O poll fica em ate 15 s: o laco ocioso e quem atende o heartbeat AMQP
        (30 s) e toca o heartbeat do processo.
        """
        return cls(
            poll_s=numero_do_ambiente(
                "OUTBOX_POLL_SEGUNDOS", _POLL_PADRAO_S, minimo=0.1, maximo=15.0
            ),
            lote=inteiro_do_ambiente("OUTBOX_LOTE", _LOTE_PADRAO, minimo=1),
            lease=timedelta(
                seconds=inteiro_do_ambiente(
                    "OUTBOX_LEASE_SEGUNDOS", _LEASE_PADRAO_S, minimo=_LEASE_MINIMO_S
                )
            ),
        )


class Relay:
    """Publica a outbox no RabbitMQ ate o ``parar`` (SIGTERM) ser sinalizado."""

    def __init__(
        self,
        *,
        engine: Engine,
        parametros: pika.ConnectionParameters,
        tracer: Tracer,
        config: ConfigRelay | None = None,
    ) -> None:
        self._engine = engine
        self._usuario = amqp.usuario(parametros)
        self._tracer = tracer
        self._config = config or ConfigRelay()
        self._outbox = Outbox(engine, self._config.atrasos_s)
        self._sinal = Sinalizador("relay", self._config.diretorio_de_saude)
        self._broker = amqp.ConexaoDoProcesso(
            parametros, processo="relay", sinal=self._sinal, declarar=self._declarar
        )
        self._catalogo = catalogo()
        self._exchanges = sorted(
            {
                self._catalogo.destino(tipo).exchange
                for tipo in self._catalogo.publicados
            }
        )
        self._limpeza = Agenda(INTERVALO_DE_LIMPEZA_S)
        OUTBOX_PENDENTES.set_function(lambda: self._outbox.contar("pendente"))
        OUTBOX_DEAD.set_function(lambda: self._outbox.contar("dead"))

    def executar(self, parar: threading.Event) -> None:
        """Laco principal; queda do broker ou do banco nao derruba o processo."""
        _log.info(
            "relay started",
            poll_s=self._config.poll_s,
            lote=self._config.lote,
            lease_s=self._config.lease.total_seconds(),
        )
        escuta: Any = None  # conexao psycopg2 do LISTEN (sem tipos)
        try:
            while not parar.is_set():
                self._sinal.bater()
                if self._broker.canal is None and not self._broker.conectar():
                    self._broker.esperar(parar)
                    continue
                if self._broker.bloqueada:
                    # Alarme de recursos no broker: nada de reivindicar linhas
                    # ate o Connection.Unblocked, que o _esperar recebe.
                    escuta = self._esperar(escuta, parar)
                    continue
                try:
                    self._drenar(parar)
                    self._limpar_se_devido()
                except _BrokerIndisponivelError:
                    _log.warning("broker connection lost; reconnecting")
                    self._broker.desconectar()
                    self._broker.esperar(parar)
                    continue
                except SQLAlchemyError:
                    # Banco fora (failover, blip do pool): as linhas voltam no
                    # proximo ciclo; derrubar o processo so reiniciaria o pod.
                    _log.exception("outbox cycle failed")
                escuta = self._esperar(escuta, parar)
        finally:
            _fechar_escuta(escuta)
            self._broker.desconectar()
            _log.info("relay stopped")

    def _declarar(self, canal: Any) -> None:  # noqa: ANN401  # BlockingChannel (pika sem tipos)
        # Declaracao passiva so do que o usuario `os` alcanca: confere a
        # topologia do platform sem redeclarar nada.
        for exchange in self._exchanges:
            canal.exchange_declare(exchange, passive=True)

    def _drenar(self, parar: threading.Event) -> None:
        """Reivindica e entrega lotes ate nao sobrar linha elegivel."""
        while not parar.is_set():
            self._sinal.bater()
            linhas = self._outbox.reivindicar(self._config.lote, self._config.lease)
            if not linhas:
                return
            for indice, linha in enumerate(linhas):
                # Cada linha pode levar ate um lease (renovar, publish bloqueado
                # e marcar): o heartbeat por linha, e nao por lote, mantem a
                # sonda de liveness longe dos 90 s com lote grande ou banco lento.
                self._sinal.bater()
                if self._broker.bloqueada:
                    self._liberar(linhas[indice:])
                    return
                try:
                    self._entregar(linha)
                except _BrokerIndisponivelError:
                    # As linhas do lote voltam ja, sem esperar o lease: o broker
                    # caiu, nao foram elas que falharam.
                    self._liberar(linhas[indice:])
                    raise
                except Exception as exc:  # noqa: BLE001  # a linha falha, o relay segue
                    self._contar_falha(linha, exc)

    def _entregar(self, reivindicada: LinhaDaOutbox) -> None:
        if not self._broker.canal.is_open:
            self._reabrir_canal()
        linha = self._outbox.renovar(reivindicada, self._config.lease)
        if linha is None:
            _log.info("outbox row taken by another replica", outbox_id=reivindicada.id)
            return
        # Daqui em diante o token do fencing e o lease renovado: a falha e
        # contada nesta linha, e nao na reivindicada.
        try:
            self._validar_e_publicar(linha)
        except _BrokerIndisponivelError:
            # O lease renovado e o desta linha: o _drenar so libera as outras.
            self._liberar([linha])
            raise
        except Exception as exc:  # noqa: BLE001  # a linha falha, o relay segue
            self._contar_falha(linha, exc)

    def _validar_e_publicar(self, linha: LinhaDaOutbox) -> None:
        try:
            self._catalogo.validar(linha.envelope)
        except ContratoInvalidoError:
            # Nenhuma nova tentativa muda o envelope: dead direto.
            if self._outbox.marcar_dead(linha, "envelope fora do contrato"):
                _log.error(
                    "outbox row with an envelope outside the contract; dead",
                    outbox_id=linha.id,
                    message_id=str(linha.mensagem_id),
                    correlation_id=str(linha.correlation_id),
                )
            return
        self._publicar_no_span(linha)

    def _contar_falha(self, linha: LinhaDaOutbox, exc: Exception) -> None:
        """Falha inesperada no caminho da linha: conta tentativa, como uma recusa.

        Se nem a contagem gravar (banco fora), a linha volta quando o lease
        vencer.
        """
        _log.error(
            "outbox row failed",
            outbox_id=linha.id,
            erro=type(exc).__name__,
            onde=onde(exc),
        )
        try:
            desfecho = self._outbox.registrar_falha(
                linha, f"falha ao publicar ({type(exc).__name__})"
            )
        except SQLAlchemyError:
            _log.warning(
                "outbox row failure not recorded; it returns after the lease",
                outbox_id=linha.id,
            )
            return
        if desfecho == "perdida":
            _log.warning(
                "outbox row failure not recorded; the lease was lost",
                outbox_id=linha.id,
            )
        elif desfecho == "dead":
            _log.error(
                "outbox row dead after unexpected failures",
                outbox_id=linha.id,
                tentativas=linha.tentativas + 1,
            )

    def _publicar_no_span(self, linha: LinhaDaOutbox) -> None:
        tipo = linha.envelope["tipo"]
        # O span cobre publicacao, marcacao da linha e logs: as linhas de log
        # saem com o trace_id e o span_id da publicacao.
        with span_de_mensagem(
            self._tracer,
            f"publish {tipo}",
            contexto=contexto_dos_cabecalhos(
                {"traceparent": linha.traceparent, "tracestate": linha.tracestate}
            ),
            tipo=SpanKind.PRODUCER,
            atributos={
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "send",
                "messaging.destination.name": linha.exchange,
                "messaging.rabbitmq.destination.routing_key": linha.routing_key,
                "messaging.message.id": str(linha.mensagem_id),
                "messaging.message.conversation_id": str(linha.correlation_id),
                "correlation_id": str(linha.correlation_id),
            },
        ) as span:
            falha = self._publicar(linha)
            if falha is not None:
                span.set_status(StatusCode.ERROR, falha)
            self._registrar_desfecho(linha, falha)

    def _publicar(self, linha: LinhaDaOutbox) -> str | None:
        """Publica com confirm; devolve a falha da mensagem, ou None se confirmada.

        Raises:
            _BrokerIndisponivelError: a conexao caiu (nao conta tentativa).
        """
        propriedades = amqp.propriedades(
            linha.envelope,
            usuario=self._usuario,
            cabecalhos=cabecalhos_do_contexto_atual(),
        )
        corpo = json.dumps(linha.envelope, ensure_ascii=False).encode()
        try:
            self._broker.canal.basic_publish(
                linha.exchange, linha.routing_key, corpo, propriedades, mandatory=True
            )
        except UnroutableError:
            return "devolvida pelo broker: nenhuma fila para a routing key"
        except NackError:
            return "recusada pelo broker (nack)"
        except ChannelClosedByBroker as exc:
            return f"canal fechado pelo broker ({exc.reply_code})"
        except amqp.ERROS_DE_CONEXAO as exc:
            raise _BrokerIndisponivelError from exc
        return None

    def _registrar_desfecho(self, linha: LinhaDaOutbox, falha: str | None) -> None:
        """Marca a linha: entregue, nova tentativa com atraso ou `dead`."""
        tipo = linha.envelope["tipo"]
        contexto_de_log = {
            "outbox_id": linha.id,
            "tipo": tipo,
            "message_id": str(linha.mensagem_id),
            "correlation_id": str(linha.correlation_id),
        }
        if falha is None:
            MENSAGENS_PUBLICADAS.labels(tipo=tipo).inc()
            # O broker confirmou: a proxima queda recomeca o backoff do minimo.
            self._broker.sucesso()
            try:
                entregue = self._outbox.marcar_entregue(linha)
            except SQLAlchemyError:
                # Publicada e sem a marca (banco fora): nao e falha da mensagem
                # e nao gasta tentativa. A linha volta quando o lease vencer e
                # sai de novo; o consumidor descarta a copia pelo id.
                _log.warning(
                    "message published but not marked; it returns after the lease",
                    **contexto_de_log,
                )
                return
            if entregue:
                _log.info("message published", **contexto_de_log)
            else:
                # O publish passou do lease e outra replica pegou a linha: a
                # mensagem pode sair de novo, e o consumidor descarta pelo id.
                _log.warning(
                    "message published after losing the lease", **contexto_de_log
                )
            return
        desfecho = self._outbox.registrar_falha(linha, falha)
        if desfecho == "perdida":
            _log.warning(
                "message publish failed after losing the lease", **contexto_de_log
            )
            return
        tentativas = linha.tentativas + 1
        if desfecho == "dead":
            _log.error(
                "message publish failed; outbox row dead",
                tentativas=tentativas,
                motivo=falha,
                **contexto_de_log,
            )
        else:
            _log.warning(
                "message publish failed; retry scheduled",
                tentativas=tentativas,
                motivo=falha,
                **contexto_de_log,
            )

    def _reabrir_canal(self) -> None:
        """Canal novo na mesma conexao, depois de o broker fechar o anterior."""
        try:
            self._broker.reabrir_canal()
        except (*amqp.ERROS_DE_CONEXAO, ChannelClosedByBroker) as exc:
            raise _BrokerIndisponivelError from exc

    def _liberar(self, linhas: list[LinhaDaOutbox]) -> None:
        try:
            self._outbox.liberar(linhas)
        except SQLAlchemyError:
            _log.exception("outbox rows not released; they return after the lease")

    def _limpar_se_devido(self) -> None:
        if not self._limpeza.devida():
            return
        apagadas = self._outbox.limpar(entre_lotes=self._atender_o_broker)
        if apagadas:
            _log.info("old outbox rows deleted", linhas=apagadas)

    def _atender_o_broker(self) -> None:
        """Heartbeat AMQP entre lotes da limpeza; a conexao caida e queda do broker."""
        try:
            self._broker.atender()
        except amqp.ERROS_DE_CONEXAO as exc:
            raise _BrokerIndisponivelError from exc

    def _esperar(self, escuta: Any, parar: threading.Event) -> Any:  # noqa: ANN401  # conexao psycopg2
        """Espera um NOTIFY ou o poll de seguranca e atende o heartbeat AMQP."""
        if escuta is None:
            escuta = self._abrir_escuta()
        if escuta is None:
            parar.wait(self._config.poll_s)
        else:
            try:
                prontos, _, _ = select.select([escuta], [], [], self._config.poll_s)
                if prontos:
                    escuta.poll()
                    # Um drain cobre todas as notificacoes acumuladas.
                    escuta.notifies.clear()
            except (self._engine.dialect.loaded_dbapi.Error, OSError):
                _log.warning("listen connection lost; polling until it is back")
                _fechar_escuta(escuta)
                escuta = None
        if self._broker.conexao is not None:
            try:
                # Sem isso o broker derruba a conexao ociosa por heartbeat.
                self._broker.atender()
            except amqp.ERROS_DE_CONEXAO:
                # Inclui o timeout do bloqueio por alarme de recursos.
                _log.warning("broker connection lost while idle; reconnecting")
                self._broker.desconectar()
                self._broker.esperar(parar)
        return escuta

    def _abrir_escuta(self) -> Any:  # noqa: ANN401  # conexao psycopg2
        """Conexao dedicada de ``LISTEN`` (fora do pool), ou None se o banco falhar."""
        try:
            argumentos, parametros = self._engine.dialect.create_connect_args(
                self._engine.url
            )
            parametros.update(_KEEPALIVES)
            # Sem o connect_timeout do engine, um banco inalcancavel prenderia o
            # laco no timeout de TCP do sistema e o heartbeat venceria.
            parametros.setdefault("connect_timeout", tempo_de_conexao())
            escuta = self._engine.dialect.loaded_dbapi.connect(
                *argumentos, **parametros
            )
            escuta.autocommit = True
            with escuta.cursor() as cursor:
                cursor.execute(f"LISTEN {CANAL_NOTIFY}")
        except (self._engine.dialect.loaded_dbapi.Error, OSError):
            _log.warning("listen connection unavailable; polling")
            return None
        return escuta


def _fechar_escuta(escuta: Any) -> None:  # noqa: ANN401  # conexao psycopg2
    if escuta is not None:
        # Best-effort: a conexao de LISTEN pode ja estar morta.
        with contextlib.suppress(Exception):
            escuta.close()
