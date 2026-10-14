"""Partes comuns aos processos relay, consumidor, prazos e notificador.

Boot, saude e fim de cada um. Prontidao (RFC-004 secao 6, ADR-042): sem HTTP de
negocio, cada processo toca um arquivo de heartbeat a cada volta do laco
(liveness; o processo vivo mas preso para de toca-lo) e mantem um arquivo de
pronto enquanto a conexao com o broker esta estabelecida (readiness). Broker fora
tira o processo de pronto sem reinicia-lo: reiniciar nao traz o broker de volta.
"""

from __future__ import annotations

import os
import signal
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog
from prometheus_client import start_http_server

from src.compartilhado.infraestrutura.ambiente import inteiro_do_ambiente
from src.compartilhado.infraestrutura.database import (
    AMBIENTES_DEV,
    SENHA_DO_BANCO_DEMO,
    criar_engine,
    resolver_database_url,
    url_com_senha,
)
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria import amqp

if TYPE_CHECKING:
    import threading

    import pika
    from sqlalchemy import Engine

_log = structlog.get_logger(__name__)

# Temporario do container (/tmp): as probes do Kubernetes leem daqui.
DIRETORIO_DE_SAUDE: Final = Path(tempfile.gettempdir())
_PORTA_DE_METRICAS: Final = 9100
# Idade maxima do heartbeat que a sonda de liveness do Kubernetes aceita
# (k8s/base/relay.yaml e consumidor.yaml; um teste confere os manifestos): acima
# dos cerca de 70 s de um publish preso no broker mudo (mensageria/amqp.py).
IDADE_MAXIMA_DO_HEARTBEAT_S: Final = 90
# A do prazos (k8s/base/prazos.yaml): o ciclo trata uma saga por transacao, com
# o banco sob o statement_timeout de 15 s, e toca o heartbeat a cada uma.
IDADE_MAXIMA_DO_HEARTBEAT_DOS_PRAZOS_S: Final = 120
# Limpezas da outbox e de `mensagens_processadas`: uma vez por hora.
INTERVALO_DE_LIMPEZA_S: Final = 3600.0
# Relay, consumidor, prazos e notificador: no maximo 4 conexoes do pool cada, e
# nenhum comando nem espera por lock passa de segundos. O laco e unico (um comando
# preso para o processo inteiro) e o handler roda na thread da conexao AMQP: o
# banco devolve o erro antes de o heartbeat do broker vencer, e a mensagem segue a
# escada de retry.
_POOL_DOS_PROCESSOS: Final = 2
_OPCOES_DO_BANCO: Final = "-c statement_timeout=15000 -c lock_timeout=10000"
# Senha de demonstracao do usuario `os` (compose e .env.example): proibida fora
# de development/test, como os demais segredos de demonstracao.
_SENHA_DO_BROKER_DEMO: Final = "pytstop-os-demo-2026"  # gitleaks:allow


class Sinalizador:
    """Arquivos de saude de um processo (heartbeat e pronto)."""

    def __init__(self, processo: str, diretorio: Path = DIRETORIO_DE_SAUDE) -> None:
        self.heartbeat = diretorio / f"{processo}-heartbeat"
        self.pronto = diretorio / f"{processo}-pronto"

    def bater(self) -> None:
        """Laco vivo: a sonda de liveness olha a idade deste arquivo."""
        self.heartbeat.touch()

    def marcar_pronto(self) -> None:
        """Conectado ao broker, com a topologia conferida: a readiness passa."""
        self.pronto.touch()

    def marcar_nao_pronto(self) -> None:
        """Sem broker: a readiness falha, sem reiniciar o processo."""
        self.pronto.unlink(missing_ok=True)


class Agenda:
    """Libera uma tarefa no maximo uma vez por ``intervalo_s`` (relogio monotonico).

    A primeira chamada ja libera; o proximo horario avanca antes da tarefa
    rodar: com o banco fora, ela tenta de novo no proximo intervalo, nao a cada
    volta do laco.
    """

    def __init__(self, intervalo_s: float) -> None:
        self._intervalo_s = intervalo_s
        self._proxima = time.monotonic()

    def devida(self) -> bool:
        agora = time.monotonic()
        if agora < self._proxima:
            return False
        self._proxima = agora + self._intervalo_s
        return True


def instalar_sinais(parar: threading.Event) -> None:
    """SIGTERM e SIGINT pedem o encerramento gracioso.

    Como PID 1 do container, o processo ignoraria o SIGTERM sem handler e
    morreria no SIGKILL do fim do grace period.
    """
    signal.signal(signal.SIGTERM, lambda *_: parar.set())
    signal.signal(signal.SIGINT, lambda *_: parar.set())


def _iniciar(processo: str) -> None:
    """Log JSON e a linha de versao no stdout (o commit que roda no pod)."""
    configurar_logging()
    git_sha = os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12]
    git_date = os.environ.get("PYTSTOP_GIT_DATE", "unknown")
    print(
        f">>> pytstop-os-service {processo} | commit {git_sha} | {git_date}", flush=True
    )


def _recusar_senha_de_demonstracao(*conexoes: tuple[str, str, str]) -> None:
    """Fora de development/test, a URL com a senha de demonstracao aborta o boot.

    Cada item e (nome da variavel, URL, senha de demonstracao dela).
    """
    if os.environ.get("ENVIRONMENT", "development").lower() in AMBIENTES_DEV:
        return
    for nome, url_de_conexao, senha in conexoes:
        if url_com_senha(url_de_conexao, senha):
            msg = (
                f"{nome} usa a senha de demonstracao -- proibido em producao. "
                "Injete a credencial real via Secret."
            )
            raise RuntimeError(msg)


def _abrir_banco(url_do_banco: str) -> Engine:
    """Mapeamentos, engine de pool pequeno e timeouts abaixo dos do broker."""
    from src.compartilhado.infraestrutura.bootstrap import iniciar_todos_mapeamentos

    iniciar_todos_mapeamentos()
    return criar_engine(
        url_do_banco,
        pool_size=_POOL_DOS_PROCESSOS,
        max_overflow=_POOL_DOS_PROCESSOS,
        opcoes=_OPCOES_DO_BANCO,
    )


def preparar(processo: str) -> tuple[Engine, pika.URLParameters]:
    """Boot comum: log JSON, guarda de segredo, mapeamentos, banco e broker.

    Raises:
        RuntimeError: ``RABBITMQ_URL`` ausente, ``RABBITMQ_URL`` ou
            ``DATABASE_URL`` com a senha de demonstracao fora de
            development/test, ou banco sem configuracao.
    """
    _iniciar(processo)
    url = os.environ.get("RABBITMQ_URL", "")
    if not url:
        msg = "RABBITMQ_URL obrigatoria (amqp://os:<senha>@<host>:5672/%2F)."
        raise RuntimeError(msg)
    url_do_banco = resolver_database_url()
    _recusar_senha_de_demonstracao(
        ("RABBITMQ_URL", url, _SENHA_DO_BROKER_DEMO),
        ("DATABASE_URL", url_do_banco, SENHA_DO_BANCO_DEMO),
    )
    return _abrir_banco(url_do_banco), amqp.parametros(url, processo)


def preparar_sem_broker(processo: str) -> Engine:
    """Boot de um processo que so fala com o banco (o ``notificador``).

    Como ``preparar``, sem o broker: ``RABBITMQ_URL`` nao e lida nem exigida.

    Raises:
        RuntimeError: ``DATABASE_URL`` com a senha de demonstracao fora de
            development/test, ou banco sem configuracao.
    """
    _iniciar(processo)
    url_do_banco = resolver_database_url()
    _recusar_senha_de_demonstracao(("DATABASE_URL", url_do_banco, SENHA_DO_BANCO_DEMO))
    return _abrir_banco(url_do_banco)


def subir_metricas() -> int:
    """Sobe o ``/metrics`` do processo na porta ``METRICS_PORT`` (ADR-043)."""
    porta = inteiro_do_ambiente("METRICS_PORT", _PORTA_DE_METRICAS, minimo=1)
    start_http_server(porta)
    _log.info("metrics endpoint started", porta=porta)
    return porta
