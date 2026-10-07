"""Consumidor da fila ``os.eventos`` (ADR-036, RFC-004 secoes 5.1 e 5.4).

Para cada mensagem, na ordem:

1. Origem: o ``user_id`` (que o broker confere contra a conexao de quem
   publicou) tem de ser o produtor do ``tipo`` no catalogo. A copia que volta
   da fila de retry chega com o ``user_id`` deste consumidor, que a republicou:
   com ``x-tentativa`` de 1 em diante ele aceita o proprio usuario.
2. Trace: o span CONSUMER e filho do contexto que veio nos headers.
3. Contrato: envelope e ``dados`` validados pelo schema do ``tipo``; leitor
   tolerante (campo extra passa), ``versao`` desconhecida reprova.
4. Efeito: na mesma transacao, grava ``mensagens_processadas`` e chama o
   handler do ``tipo``; ``id`` repetido recebe ack sem efeito.

Erro transitorio (banco fora, ``FalhaTransitoriaError``, conflito de versao):
copia em ``pytstop.retry``, com confirm, e so entao o ack da original. A
routing key e a fila de retry do nivel da nova tentativa (``os.eventos.retry.1s``,
``.5s``, ``.15s``, ``.60s`` e ``.300s``): o atraso e o TTL da propria fila, que
devolve a copia a ``os.eventos`` pelo dead letter. Uma fila por atraso, e nao um
``expiration`` por mensagem numa fila so, porque a mensagem so expira na cabeca
da fila: uma copia de 300 s seguraria as de 1 s. Esgotadas as cinco, ou erro
permanente (tipo, origem, JSON ou contrato invalidos, ou qualquer outra
excecao): ``reject`` sem requeue, e a fila manda a mensagem para a
``os.eventos.dlq``. Uma vez por hora apaga, em lotes, as linhas de
``mensagens_processadas`` com mais de 30 dias.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import pika
import structlog
from opentelemetry.trace import SpanKind, StatusCode
from pika.exceptions import ChannelClosedByBroker, NackError, UnroutableError
from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.aplicacao.mensageria import (
    ContratoInvalidoError,
    Desfecho,
    FalhaTransitoriaError,
    MensagemRecebida,
)
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.contratos import FILA, catalogo
from src.compartilhado.infraestrutura.mensageria.processo import (
    DIRETORIO_DE_SAUDE,
    INTERVALO_DE_LIMPEZA_S,
    Agenda,
    Sinalizador,
    inteiro_do_ambiente,
    onde,
)
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
    contexto_dos_cabecalhos,
)
from src.compartilhado.infraestrutura.outbox_mapping import (
    apagar_em_lotes,
    registrar_processada,
)

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    from opentelemetry.trace import Tracer
    from sqlalchemy.orm import Session, sessionmaker

_log = structlog.get_logger(__name__)

# Metrica do consumidor (ADR-043), no registro padrao que o /metrics do processo
# serve. So o consumidor importa este modulo.
MENSAGENS_CONSUMIDAS: Final = Counter(
    "pytstop_mensagens_consumidas_total",
    "Mensagens tratadas pelo consumidor, por tipo e resultado "
    "(processada, duplicada, ignorada, retry, dlq).",
    ["tipo", "resultado"],
)

type Handler = Callable[[MensagemRecebida, Session], Desfecho]

# Fila de retry de cada tentativa (`os.eventos.retry.<nivel>`, TTL no
# definitions.json do platform); depois da quinta, DLQ.
NIVEIS_DE_RETRY: Final = ("1s", "5s", "15s", "60s", "300s")
_EXCHANGE_DE_RETRY: Final = "pytstop.retry"
# Retencao de 30 dias (RFC-004 secao 5.4), em lotes.
_SQL_LIMPEZA: Final = text(
    "DELETE FROM mensagens_processadas WHERE mensagem_id IN ("
    "SELECT mensagem_id FROM mensagens_processadas "
    "WHERE processada_em < now() - interval '30 days' LIMIT :lote)"
)
_TIPO_DESCONHECIDO: Final = "desconhecido"
# Banco (fora do ar, deadlock, timeout, conflito de escrita) e dependencia
# fora: a mesma mensagem tende a passar numa nova tentativa.
_TRANSITORIOS: Final[tuple[type[Exception], ...]] = (
    FalhaTransitoriaError,
    ConflitoDeConcorrenciaException,
    SQLAlchemyError,
    TimeoutError,
    ConnectionError,
)


class _MensagemRejeitadaError(Exception):
    """Erro permanente: a mensagem vai para a DLQ sem nova tentativa."""

    def __init__(self, motivo: str, **contexto: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
        self.contexto = contexto


@dataclass(frozen=True, slots=True)
class ConfigConsumidor:
    # Prefetch pequeno: o que esta no buffer de um consumidor espera por ele.
    prefetch: int = 5
    # Intervalo maximo sem mensagem antes de bater o heartbeat e olhar o sinal
    # de parada.
    inatividade_s: float = 1.0
    diretorio_de_saude: Path = DIRETORIO_DE_SAUDE

    @classmethod
    def do_ambiente(cls) -> ConfigConsumidor:
        """Le ``CONSUMIDOR_PREFETCH``."""
        return cls(prefetch=inteiro_do_ambiente("CONSUMIDOR_PREFETCH", 5, minimo=1))


class Consumidor:
    """Consome ``os.eventos`` ate o ``parar`` (SIGTERM) ser sinalizado."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        parametros: pika.ConnectionParameters,
        despachante: Mapping[str, Handler],
        tracer: Tracer,
        config: ConfigConsumidor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._usuario = amqp.usuario(parametros)
        self._despachante = despachante
        self._tracer = tracer
        self._config = config or ConfigConsumidor()
        self._sinal = Sinalizador("consumidor", self._config.diretorio_de_saude)
        self._broker = amqp.ConexaoDoProcesso(
            parametros,
            processo="consumidor",
            sinal=self._sinal,
            declarar=self._declarar,
        )
        self._catalogo = catalogo()
        self._limpeza = Agenda(INTERVALO_DE_LIMPEZA_S)

    def executar(self, parar: threading.Event) -> None:
        """Laco principal; queda do broker nao derruba o processo.

        Encerramento gracioso: com o ``parar`` sinalizado, conclui a mensagem em
        curso e fecha a conexao; o broker devolve a fila as pre-buscadas sem
        ack. Conexao perdida e consumo cancelado pelo broker reconectam com o
        backoff da ``ConexaoDoProcesso``.
        """
        _log.info("consumer started", fila=FILA, prefetch=self._config.prefetch)
        try:
            while not parar.is_set():
                self._sinal.bater()
                if not self._broker.conectar():
                    self._broker.esperar(parar)
                    continue
                try:
                    self._consumir(parar)
                    if not parar.is_set():
                        # O gerador do pika so termina quando o broker cancela o
                        # consumo (fila apagada, por exemplo).
                        _log.warning(
                            "consumption cancelled by the broker; reconnecting"
                        )
                except (*amqp.ERROS_DE_CONEXAO, ChannelClosedByBroker) as exc:
                    # A mensagem sem ack volta para a fila quando a conexao fecha.
                    _log.warning(
                        "broker connection lost; reconnecting",
                        erro=type(exc).__name__,
                        codigo=getattr(exc, "reply_code", None),
                    )
                finally:
                    self._broker.desconectar()
                if not parar.is_set():
                    self._broker.esperar(parar)
        finally:
            self._broker.desconectar()
            _log.info("consumer stopped")

    def _declarar(self, canal: Any) -> None:  # noqa: ANN401  # BlockingChannel (pika sem tipos)
        # Declaracao passiva so do que o usuario `os` alcanca: a fila que ele le
        # e o exchange de retry em que escreve.
        canal.queue_declare(FILA, passive=True)
        canal.exchange_declare(_EXCHANGE_DE_RETRY, passive=True)
        canal.basic_qos(prefetch_count=self._config.prefetch)

    def _consumir(self, parar: threading.Event) -> None:
        for metodo, propriedades, corpo in self._broker.canal.consume(
            FILA, inactivity_timeout=self._config.inatividade_s
        ):
            self._sinal.bater()
            if metodo is not None:
                self._tratar(metodo, propriedades, corpo)
            self._limpar_se_devido()
            if parar.is_set():
                # Conclui a mensagem em curso e para; ao fechar a conexao o
                # broker devolve a fila as pre-buscadas sem ack.
                return

    def _tratar(self, metodo: Any, propriedades: Any, corpo: bytes) -> None:  # noqa: ANN401  # tipos do pika
        tipo = propriedades.type if isinstance(propriedades.type, str) else ""
        rotulo = tipo if tipo in self._catalogo.consumidos else _TIPO_DESCONHECIDO
        cabecalhos = propriedades.headers or {}
        with (
            structlog.contextvars.bound_contextvars(
                message_id=propriedades.message_id,
                correlation_id=propriedades.correlation_id,
                tipo=rotulo,
            ),
            self._tracer.start_as_current_span(
                f"process {rotulo}",
                context=contexto_dos_cabecalhos(cabecalhos),
                kind=SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "rabbitmq",
                    "messaging.operation.type": "process",
                    "messaging.destination.name": FILA,
                    "messaging.message.id": str(propriedades.message_id),
                    "messaging.message.conversation_id": str(
                        propriedades.correlation_id
                    ),
                    "correlation_id": str(propriedades.correlation_id),
                },
                record_exception=False,
            ) as span,
        ):
            try:
                tentativa = self._tentativa(cabecalhos)
                resultado = self._processar(propriedades, corpo, tipo, tentativa)
            except _MensagemRejeitadaError as exc:
                _log.warning(
                    "message rejected to dlq", motivo=exc.motivo, **exc.contexto
                )
                span.set_status(StatusCode.ERROR, exc.motivo)
                self._broker.canal.basic_reject(metodo.delivery_tag, requeue=False)
                resultado = "dlq"
            except _TRANSITORIOS as exc:
                span.set_status(StatusCode.ERROR, type(exc).__name__)
                resultado = self._repetir(metodo, propriedades, corpo, tentativa, exc)
            except Exception as exc:  # noqa: BLE001  # mensagem venenosa vai para a DLQ
                # Repetir nao muda o resultado, e derrubar o processo traria a
                # mesma mensagem de volta primeiro, a cada reinicio.
                _log.error(
                    "message processing crashed; rejected to dlq",
                    erro=type(exc).__name__,
                    onde=onde(exc),
                )
                span.set_status(StatusCode.ERROR, type(exc).__name__)
                self._broker.canal.basic_reject(metodo.delivery_tag, requeue=False)
                resultado = "dlq"
            else:
                self._broker.canal.basic_ack(metodo.delivery_tag)
            MENSAGENS_CONSUMIDAS.labels(tipo=rotulo, resultado=resultado).inc()
            # A mensagem foi resolvida: a proxima queda recomeca o backoff do
            # minimo.
            self._broker.sucesso()

    def _tentativa(self, cabecalhos: Mapping[str, Any]) -> int:
        """``x-tentativa`` da mensagem: 0 na primeira entrega, ate 5 na copia."""
        tentativa = cabecalhos.get("x-tentativa", 0)
        if (
            not isinstance(tentativa, int)
            or isinstance(tentativa, bool)
            or not 0 <= tentativa <= len(NIVEIS_DE_RETRY)
        ):
            raise _MensagemRejeitadaError("tentativa_invalida")
        return tentativa

    def _processar(
        self,
        propriedades: Any,  # noqa: ANN401  # BasicProperties (pika sem tipos)
        corpo: bytes,
        tipo: str,
        tentativa: int,
    ) -> str:
        """Confere e aplica a mensagem; devolve o ``resultado`` da metrica."""
        produtor = (
            self._catalogo.produtor(tipo) if tipo in self._catalogo.consumidos else None
        )
        if produtor is None:
            raise _MensagemRejeitadaError("tipo_desconhecido")
        usuario = propriedades.user_id
        copia_de_retry = usuario == self._usuario and tentativa > 0
        if usuario != produtor and not copia_de_retry:
            raise _MensagemRejeitadaError(
                "produtor_divergente", user_id=str(usuario), esperado=produtor
            )
        try:
            envelope = json.loads(corpo)
            self._catalogo.validar(envelope)
        except ValueError as exc:  # JSON invalido (UnicodeDecodeError incluso)
            raise _MensagemRejeitadaError("json_invalido") from exc
        except ContratoInvalidoError as exc:
            raise _MensagemRejeitadaError(
                "contrato_invalido", caminho=exc.caminho, regra=exc.regra
            ) from exc
        if envelope["tipo"] != tipo or envelope["id"] != propriedades.message_id:
            raise _MensagemRejeitadaError("propriedades_divergentes")
        mensagem = MensagemRecebida.do_envelope(envelope)
        handler = self._despachante.get(tipo)
        if handler is None:
            raise _MensagemRejeitadaError("sem_handler")
        with self._session_factory() as sessao:
            if not registrar_processada(sessao, mensagem.id):
                _log.info("duplicate message acknowledged without effect")
                return "duplicada"
            try:
                desfecho = handler(mensagem, sessao)
            except _TRANSITORIOS:
                raise
            except Exception as exc:
                # Bug ou regra violada: repetir nao muda o resultado. So o tipo
                # e o lugar da excecao vao para o log: a mensagem dela pode
                # trazer dado da mensagem (placa, texto livre).
                raise _MensagemRejeitadaError(
                    "erro_no_handler", erro=type(exc).__name__, onde=onde(exc)
                ) from exc
            sessao.commit()
        return desfecho.value

    def _repetir(
        self,
        metodo: Any,  # noqa: ANN401
        propriedades: Any,  # noqa: ANN401
        corpo: bytes,
        tentativa: int,
        erro: Exception,
    ) -> str:
        """Copia na fila de retry do nivel e ack da original; esgotada, DLQ."""
        if tentativa >= len(NIVEIS_DE_RETRY):
            _log.warning(
                "message retries exhausted; rejected to dlq",
                tentativas=tentativa,
                erro=type(erro).__name__,
            )
            self._broker.canal.basic_reject(metodo.delivery_tag, requeue=False)
            return "dlq"
        copia = pika.BasicProperties(
            message_id=propriedades.message_id,
            correlation_id=propriedades.correlation_id,
            type=propriedades.type,
            content_type=propriedades.content_type,
            delivery_mode=pika.DeliveryMode.Persistent,
            # O broker so aceita o usuario da propria conexao (406 se outro).
            user_id=self._usuario,
            # Contexto do span deste consumo: a proxima passada vira filha dele
            # no mesmo trace.
            headers={**cabecalhos_do_contexto_atual(), "x-tentativa": tentativa + 1},
        )
        fila_de_retry = f"{FILA}.retry.{NIVEIS_DE_RETRY[tentativa]}"
        try:
            self._broker.canal.basic_publish(
                _EXCHANGE_DE_RETRY, fila_de_retry, corpo, copia, mandatory=True
            )
        except (UnroutableError, NackError) as exc:
            # Sem como reagendar: a DLQ guarda a mensagem para o redrive.
            _log.error(
                "retry copy refused by the broker; rejected to dlq",
                fila=fila_de_retry,
                erro=type(exc).__name__,
            )
            self._broker.canal.basic_reject(metodo.delivery_tag, requeue=False)
            return "dlq"
        except ChannelClosedByBroker as exc:
            # Permissao de topico ou fila de retry ausente na topologia: o
            # broker fecha o canal, e a original volta para a fila quando a
            # conexao fecha (a reconexao segue com backoff).
            _log.error(
                "retry copy refused by the broker; check the retry topology",
                fila=fila_de_retry,
                codigo=exc.reply_code,
            )
            raise
        self._broker.canal.basic_ack(metodo.delivery_tag)
        _log.warning(
            "message processing failed; retry scheduled",
            tentativa=tentativa + 1,
            fila=fila_de_retry,
            erro=type(erro).__name__,
        )
        return "retry"

    def _limpar_se_devido(self) -> None:
        if not self._limpeza.devida():
            return
        try:
            apagadas = apagar_em_lotes(
                self._session_factory.begin,
                _SQL_LIMPEZA,
                entre_lotes=self._broker.atender,
            )
        except SQLAlchemyError as exc:
            _log.warning("processed messages cleanup failed", erro=type(exc).__name__)
            return
        if apagadas:
            _log.info("old processed messages deleted", linhas=apagadas)
