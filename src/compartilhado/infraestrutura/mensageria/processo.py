"""Partes comuns aos processos relay e consumidor: boot, saude e encerramento.

Prontidao (RFC-004 secao 6, ADR-042): sem HTTP de negocio, cada processo toca um
arquivo de heartbeat a cada volta do laco (liveness; o processo vivo mas preso
para de toca-lo) e mantem um arquivo de pronto enquanto a conexao com o broker
esta estabelecida (readiness). Broker fora tira o processo de pronto sem
reinicia-lo: reiniciar nao traz o broker de volta.
"""

from __future__ import annotations

import math
import os
import signal
import tempfile
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import structlog
from prometheus_client import start_http_server

from src.compartilhado.infraestrutura.database import (
    AMBIENTES_DEV,
    criar_engine,
    resolver_database_url,
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
# Limpezas da outbox e de `mensagens_processadas`: uma vez por hora.
INTERVALO_DE_LIMPEZA_S: Final = 3600.0
# Senha de demonstracao do usuario `os` (compose e .env.example): proibida fora
# de development/test, como os demais segredos de demonstracao.
_SENHA_DO_BROKER_DEMO: Final = "pytstop-os-demo-2026"  # gitleaks:allow


class Sinalizador:
    """Arquivos de saude de um processo (heartbeat e pronto)."""

    def __init__(self, processo: str, diretorio: Path = DIRETORIO_DE_SAUDE) -> None:
        self.heartbeat = diretorio / f"{processo}-heartbeat"
        self.pronto = diretorio / f"{processo}-pronto"

    def bater(self) -> None:
        self.heartbeat.touch()

    def marcar_pronto(self) -> None:
        self.pronto.touch()

    def marcar_nao_pronto(self) -> None:
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


def inteiro_do_ambiente(nome: str, padrao: int, *, minimo: int) -> int:
    """Le um inteiro do ambiente; valor invalido aborta o boot com mensagem clara."""
    bruto = os.environ.get(nome, str(padrao))
    try:
        valor = int(bruto)
    except ValueError:
        valor = minimo - 1
    if valor < minimo:
        msg = f"config invalida no boot: {nome}={bruto!r} (inteiro >= {minimo})"
        raise RuntimeError(msg)
    return valor


def numero_do_ambiente(
    nome: str, padrao: float, *, minimo: float, maximo: float = math.inf
) -> float:
    """Le um numero do ambiente; valor invalido aborta o boot com mensagem clara."""
    bruto = os.environ.get(nome, str(padrao))
    try:
        valor = float(bruto)
    except ValueError:
        valor = minimo - 1
    if math.isnan(valor) or valor < minimo or valor > maximo:
        msg = (
            f"config invalida no boot: {nome}={bruto!r} (numero de {minimo} a {maximo})"
        )
        raise RuntimeError(msg)
    return valor


def instalar_sinais(parar: threading.Event) -> None:
    """SIGTERM e SIGINT pedem o encerramento gracioso.

    Como PID 1 do container, o processo ignoraria o SIGTERM sem handler e
    morreria no SIGKILL do fim do grace period.
    """
    signal.signal(signal.SIGTERM, lambda *_: parar.set())
    signal.signal(signal.SIGINT, lambda *_: parar.set())


def preparar(processo: str) -> tuple[Engine, pika.URLParameters]:
    """Boot comum: log JSON, guarda de segredo, mapeamentos, banco e broker.

    Raises:
        RuntimeError: ``RABBITMQ_URL`` ausente ou com a senha de demonstracao
            fora de development/test, ou banco sem configuracao.
    """
    configurar_logging()
    git_sha = os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12]
    git_date = os.environ.get("PYTSTOP_GIT_DATE", "unknown")
    print(
        f">>> pytstop-os-service {processo} | commit {git_sha} | {git_date}", flush=True
    )

    url = os.environ.get("RABBITMQ_URL", "")
    if not url:
        msg = "RABBITMQ_URL obrigatoria (amqp://os:<senha>@<host>:5672/%2F)."
        raise RuntimeError(msg)
    ambiente = os.environ.get("ENVIRONMENT", "development").lower()
    if (
        ambiente not in AMBIENTES_DEV
        and urlsplit(url).password == _SENHA_DO_BROKER_DEMO
    ):
        msg = (
            "RABBITMQ_URL usa a senha de demonstracao do RabbitMQ -- proibido em "
            "producao. Injete a credencial real via Secret."
        )
        raise RuntimeError(msg)

    from src.compartilhado.infraestrutura.bootstrap import iniciar_todos_mapeamentos

    iniciar_todos_mapeamentos()
    return criar_engine(resolver_database_url()), amqp.parametros(url, processo)


def onde(exc: BaseException) -> str:
    """Arquivo e linha em que a excecao nasceu, para o log (sem a mensagem dela).

    A mensagem de uma excecao pode trazer dado da mensagem (placa, texto livre):
    os logs de falha levam so o tipo e este ``arquivo:linha``.
    """
    quadros = traceback.extract_tb(exc.__traceback__)
    if not quadros:
        return "desconhecido"
    return f"{quadros[-1].filename.rsplit('/', 1)[-1]}:{quadros[-1].lineno}"


def subir_metricas() -> int:
    """Sobe o ``/metrics`` do processo na porta ``METRICS_PORT`` (ADR-043)."""
    porta = inteiro_do_ambiente("METRICS_PORT", _PORTA_DE_METRICAS, minimo=1)
    start_http_server(porta)
    _log.info("metrics endpoint started", porta=porta)
    return porta
