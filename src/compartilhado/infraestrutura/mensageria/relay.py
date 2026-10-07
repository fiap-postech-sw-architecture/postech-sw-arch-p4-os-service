"""Relay da outbox para o RabbitMQ (ADR-036; o relay do p3 com destino AMQP).

Sem conexao com o broker o relay nao reivindica linhas e reconecta com backoff
de ate 30 s: a queda do broker nao conta tentativa de nenhuma linha. Conectado,
drena a outbox em lotes. O claim (transacao curta) pega as linhas ``pendente``
vencidas com ``FOR UPDATE SKIP LOCKED``, em ordem por OS (head-of-line por
``correlation_id``; ``dead`` nao bloqueia), e estende ``proxima_tentativa_em``
por um lease. Cada linha e entregue na propria transacao, que comeca pelo
fencing (o relock ``SKIP LOCKED`` com status ``pendente``): duas replicas nunca
publicam a mesma linha ao mesmo tempo.

A publicacao usa publisher confirms e ``mandatory``: a linha so vira
``entregue`` depois do confirm. Devolvida (sem fila para a routing key),
recusada (nack) ou com o canal fechado pelo broker, a linha conta tentativa,
com os atrasos do relay do p3, ate ``dead`` na quinta falha. Entre lotes o
relay espera o ``NOTIFY outbox_novo`` com poll de seguranca e, uma vez por hora,
apaga as linhas entregues ha mais de 7 dias (RFC-004 secao 5.4).

Trace (ADR-043): cada publicacao abre um span PRODUCER filho do contexto
gravado na linha e leva o contexto desse span nos headers; o laco ocioso nao
abre span.
"""

from __future__ import annotations

import contextlib
import json
import select
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog
from opentelemetry.trace import SpanKind, StatusCode
from pika.exceptions import ChannelClosedByBroker, NackError, UnroutableError
from prometheus_client import Counter, Gauge
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.infraestrutura.database import tempo_de_conexao
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.mensageria.processo import (
    DIRETORIO_DE_SAUDE,
    Sinalizador,
    inteiro_do_ambiente,
    numero_do_ambiente,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
    contexto_dos_cabecalhos,
)
from src.compartilhado.infraestrutura.outbox_mapping import CANAL_NOTIFY

if TYPE_CHECKING:
    import threading
    from collections.abc import Sequence
    from pathlib import Path
    from uuid import UUID

    import pika
    from opentelemetry.trace import Tracer
    from sqlalchemy import Connection, Engine

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

# Politica do relay do p3: atraso depois de cada falha da propria mensagem, e a
# quinta falha leva a linha a `dead`.
ATRASOS_S: Final = (1, 4, 16, 64)
MAX_TENTATIVAS: Final = 5
_RETENCAO: Final = timedelta(days=7)
_INTERVALO_DE_LIMPEZA: Final = timedelta(hours=1)
# Keepalives TCP da conexao dedicada de LISTEN: um peer que sumiu em silencio
# e detectado em cerca de 60 s, em vez de deixar o relay surdo ao NOTIFY.
_KEEPALIVES: Final = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

_SQL_CLAIM: Final = text(
    "SELECT o.id, o.mensagem_id, o.correlation_id, o.exchange, o.routing_key, "
    "o.envelope, o.traceparent, o.tracestate, o.tentativas "
    "FROM outbox o "
    "WHERE o.status = 'pendente' AND o.proxima_tentativa_em <= :agora "
    "AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.correlation_id = o.correlation_id "
    "AND p.id < o.id AND p.status = 'pendente') "
    "ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT :limite"
)
_SQL_LEASE: Final = text(
    "UPDATE outbox SET proxima_tentativa_em = :ate WHERE id = ANY(:ids)"
)
_SQL_FENCING: Final = text(
    "SELECT 1 FROM outbox WHERE id = :id AND status = 'pendente' FOR UPDATE SKIP LOCKED"
)
_SQL_ENTREGUE: Final = text(
    "UPDATE outbox SET status = 'entregue', entregue_em = :agora, "
    "ultimo_erro = NULL WHERE id = :id"
)
_SQL_NOVA_TENTATIVA: Final = text(
    "UPDATE outbox SET tentativas = :tentativas, proxima_tentativa_em = :proxima, "
    "ultimo_erro = :erro WHERE id = :id"
)
_SQL_DEAD: Final = text(
    "UPDATE outbox SET status = 'dead', tentativas = :tentativas, "
    "ultimo_erro = :erro WHERE id = :id"
)
_SQL_LIBERAR: Final = text(
    "UPDATE outbox SET proxima_tentativa_em = :agora "
    "WHERE id = ANY(:ids) AND status = 'pendente'"
)
_SQL_LIMPEZA: Final = text(
    "DELETE FROM outbox WHERE status = 'entregue' AND entregue_em < :limite"
)
_SQL_CONTAGEM: Final = text("SELECT count(*) FROM outbox WHERE status = :status")


class _BrokerIndisponivelError(Exception):
    """A conexao com o broker caiu no meio do trabalho (nao e falha da linha)."""


@dataclass(frozen=True, slots=True)
class ConfigRelay:
    poll_s: float = 5.0
    lote: int = 10
    # Lease > tempo de pior caso de uma publicacao (confirm ou heartbeat
    # vencido): so assim outra replica nao reivindica a linha em voo.
    lease: timedelta = timedelta(seconds=60)
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
                "OUTBOX_POLL_SEGUNDOS", 5.0, minimo=0.1, maximo=15.0
            ),
            lote=inteiro_do_ambiente("OUTBOX_LOTE", 10, minimo=1),
            lease=timedelta(
                seconds=inteiro_do_ambiente("OUTBOX_LEASE_SEGUNDOS", 60, minimo=10)
            ),
        )


@dataclass(frozen=True, slots=True)
class _Linha:
    id: int
    mensagem_id: UUID
    correlation_id: UUID
    exchange: str
    routing_key: str
    envelope: dict[str, Any] = field(repr=False)
    traceparent: str | None
    tracestate: str | None
    tentativas: int


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
        self._sinal = Sinalizador("relay", self._config.diretorio_de_saude)
        self._broker = amqp.ConexaoDoProcesso(
            parametros, processo="relay", sinal=self._sinal, declarar=self._declarar
        )
        contratos = catalogo()
        self._exchanges = sorted(
            {contratos.destino(tipo).exchange for tipo in contratos.publicados}
        )
        self._proxima_limpeza = _agora()
        OUTBOX_PENDENTES.set_function(lambda: self._contar("pendente"))
        OUTBOX_DEAD.set_function(lambda: self._contar("dead"))

    def executar(self, parar: threading.Event) -> None:
        """Laco principal; queda do broker ou do banco nao derruba o processo."""
        _log.info(
            "relay started",
            poll_s=self._config.poll_s,
            lote=self._config.lote,
            lease_s=self._config.lease.total_seconds(),
        )
        escuta: Any = None
        try:
            while not parar.is_set():
                self._sinal.bater()
                if self._broker.canal is None and not self._broker.conectar():
                    self._broker.esperar(parar)
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
            with self._engine.begin() as conexao:
                linhas = self._reivindicar(conexao)
            if not linhas:
                return
            for indice, linha in enumerate(linhas):
                try:
                    self._entregar(linha)
                except _BrokerIndisponivelError:
                    # As linhas do lote voltam ja, sem esperar o lease: o broker
                    # caiu, nao foram elas que falharam.
                    self._liberar([restante.id for restante in linhas[indice:]])
                    raise
                except SQLAlchemyError:
                    # Erro de banco numa linha nao derruba o lote; ela volta
                    # quando o lease vencer.
                    _log.exception("outbox row failed", outbox_id=linha.id)

    def _reivindicar(self, conexao: Connection) -> list[_Linha]:
        agora = _agora()
        linhas = [
            _Linha(
                id=row.id,
                mensagem_id=row.mensagem_id,
                correlation_id=row.correlation_id,
                exchange=row.exchange,
                routing_key=row.routing_key,
                envelope=row.envelope,
                traceparent=row.traceparent,
                tracestate=row.tracestate,
                tentativas=row.tentativas,
            )
            for row in conexao.execute(
                _SQL_CLAIM, {"agora": agora, "limite": self._config.lote}
            )
        ]
        if linhas:
            conexao.execute(
                _SQL_LEASE,
                {"ate": agora + self._config.lease, "ids": [li.id for li in linhas]},
            )
        return linhas

    def _entregar(self, linha: _Linha) -> None:
        if not self._broker.canal.is_open:
            self._reabrir_canal()
        with self._engine.begin() as conexao:
            if conexao.execute(_SQL_FENCING, {"id": linha.id}).first() is None:
                _log.info("outbox row taken by another replica", outbox_id=linha.id)
                return
            tipo = linha.envelope["tipo"]
            # O span cobre publicacao, marcacao da linha e logs: as linhas de
            # log saem com o trace_id e o span_id da publicacao.
            with self._tracer.start_as_current_span(
                f"publish {tipo}",
                context=contexto_dos_cabecalhos(
                    {"traceparent": linha.traceparent, "tracestate": linha.tracestate}
                ),
                kind=SpanKind.PRODUCER,
                attributes={
                    "messaging.system": "rabbitmq",
                    "messaging.operation.type": "send",
                    "messaging.destination.name": linha.exchange,
                    "messaging.rabbitmq.destination.routing_key": linha.routing_key,
                    "messaging.message.id": str(linha.mensagem_id),
                    "messaging.message.conversation_id": str(linha.correlation_id),
                    "correlation_id": str(linha.correlation_id),
                },
                record_exception=False,
            ) as span:
                falha = self._publicar(linha)
                if falha is not None:
                    span.set_status(StatusCode.ERROR, falha)
                self._registrar_desfecho(conexao, linha, falha)

    def _publicar(self, linha: _Linha) -> str | None:
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

    def _registrar_desfecho(
        self, conexao: Connection, linha: _Linha, falha: str | None
    ) -> None:
        """Marca a linha: entregue, nova tentativa com atraso ou `dead`."""
        tipo = linha.envelope["tipo"]
        contexto_de_log = {
            "outbox_id": linha.id,
            "tipo": tipo,
            "message_id": str(linha.mensagem_id),
            "correlation_id": str(linha.correlation_id),
        }
        if falha is None:
            conexao.execute(_SQL_ENTREGUE, {"id": linha.id, "agora": _agora()})
            MENSAGENS_PUBLICADAS.labels(tipo=tipo).inc()
            _log.info("message published", **contexto_de_log)
            # O broker confirmou: a proxima queda recomeca o backoff do minimo.
            self._broker.sucesso()
            return
        tentativas = linha.tentativas + 1
        parametros = {"id": linha.id, "tentativas": tentativas, "erro": falha}
        if tentativas >= MAX_TENTATIVAS:
            conexao.execute(_SQL_DEAD, parametros)
            _log.error(
                "message publish failed; outbox row dead",
                tentativas=tentativas,
                motivo=falha,
                **contexto_de_log,
            )
            return
        atraso = timedelta(seconds=self._config.atrasos_s[tentativas - 1])
        conexao.execute(
            _SQL_NOVA_TENTATIVA, {**parametros, "proxima": _agora() + atraso}
        )
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

    def _liberar(self, ids: Sequence[int]) -> None:
        try:
            with self._engine.begin() as conexao:
                conexao.execute(_SQL_LIBERAR, {"agora": _agora(), "ids": list(ids)})
        except SQLAlchemyError:
            _log.exception("outbox rows not released; they return after the lease")

    def _limpar_se_devido(self) -> None:
        agora = _agora()
        if agora < self._proxima_limpeza:
            return
        # Avanca antes: com o banco fora, a proxima tentativa e daqui a uma
        # hora, nao a cada volta do laco.
        self._proxima_limpeza = agora + _INTERVALO_DE_LIMPEZA
        with self._engine.begin() as conexao:
            apagadas = conexao.execute(
                _SQL_LIMPEZA, {"limite": agora - _RETENCAO}
            ).rowcount
        if apagadas:
            _log.info("delivered outbox rows deleted", linhas=apagadas)

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
                self._broker.conexao.process_data_events(time_limit=0)
            except amqp.ERROS_DE_CONEXAO:
                _log.warning("broker connection lost while idle; reconnecting")
                self._broker.desconectar()
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

    def _contar(self, status: str) -> float:
        try:
            with self._engine.connect() as conexao:
                total = conexao.execute(_SQL_CONTAGEM, {"status": status}).scalar_one()
        except SQLAlchemyError:
            return float("nan")
        return float(total)


def _agora() -> datetime:
    return datetime.now(UTC)


def _fechar_escuta(escuta: Any) -> None:  # noqa: ANN401  # conexao psycopg2
    if escuta is not None:
        # Best-effort: a conexao de LISTEN pode ja estar morta.
        with contextlib.suppress(Exception):
            escuta.close()
