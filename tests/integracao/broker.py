"""RabbitMQ 4.3.6 de teste com a topologia e os usuarios copiados do platform.

Sobe a imagem com o ``definitions.json``, o ``rabbitmq.conf`` e o admin de
``contratos/rabbitmq/`` e roda o ``criar-usuarios.sh`` copiado, que cria os
usuarios ``os``, ``billing`` e ``execucao`` com as permissoes de
``permissoes.json``. A unica mudanca na topologia e o TTL das filas de retry,
de 100 ms em vez de 1 a 300 s, para o ciclo inteiro caber num teste. Os testes
publicam eventos como ``billing`` ou ``execucao`` (os participantes) e leem as
filas como ``admin``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Self

import pika
from pika.exceptions import AMQPError
from testcontainers.core.container import DockerContainer

from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

IMAGEM = "rabbitmq:4.3.6-management"
_RABBITMQ = CONTRATOS / "rabbitmq"
_ADMIN = json.loads((_RABBITMQ / "rabbitmq-admin.json").read_text())["users"][0]
# Senhas de demonstracao, as mesmas do compose deste servico e do platform.
SENHAS = {
    "admin": _ADMIN["password"],
    "os": "pytstop-os-demo-2026",  # gitleaks:allow
    "billing": "pytstop-billing-demo-2026",  # gitleaks:allow
    "execucao": "pytstop-execucao-demo-2026",  # gitleaks:allow
}
_DEFINITIONS = json.loads((_RABBITMQ / "definitions.json").read_text())
FILAS = tuple(fila["name"] for fila in _DEFINITIONS["queues"])
TTL_DE_RETRY_MS = 100
# O Docker do colima so enxerga a home do usuario (o temporario do sistema fica
# fora): o definitions de teste mora no cache do pytest, ignorado pelo git.
_DEFINITIONS_DE_TESTE = CONTRATOS.parent / ".pytest_cache/rabbitmq/definitions.json"
_USUARIO_DA_ORIGEM = {
    "os-service": "os",
    "billing-service": "billing",
    "execution-service": "execucao",
}
_MONTAGENS = {
    "rabbitmq.conf": "/etc/rabbitmq/conf.d/20-pytstop.conf",
    "enabled_plugins": "/etc/rabbitmq/enabled_plugins",
    "definitions.json": "/etc/rabbitmq/definitions/definitions.json",
    "rabbitmq-admin.json": "/etc/rabbitmq/definitions/admin.json",
    "criar-usuarios.sh": "/scripts/criar-usuarios.sh",
    "permissoes.json": "/scripts/permissoes.json",
}


def esperar_ate(
    condicao: Callable[[], Any], prazo_s: float = 20.0, intervalo_s: float = 0.05
) -> Any:
    """Repete ``condicao`` ate ela devolver algo verdadeiro (ou estoura o prazo)."""
    fim = time.monotonic() + prazo_s
    while True:
        resultado = condicao()
        if resultado:
            return resultado
        if time.monotonic() > fim:
            msg = f"condicao nao satisfeita em {prazo_s} s"
            raise AssertionError(msg)
        time.sleep(intervalo_s)


def snake_case(nome: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", nome).lower()


class Broker:
    """O container e atalhos para publicar, ler e operar o broker nos testes."""

    def __init__(self, container: DockerContainer) -> None:
        self.container = container
        self.host = container.get_container_host_ip()
        self.porta = int(container.get_exposed_port(5672))

    def url(self, usuario: str) -> str:
        return f"amqp://{usuario}:{SENHAS[usuario]}@{self.host}:{self.porta}/%2F"

    def parametros(self, usuario: str = "os") -> pika.URLParameters:
        from src.compartilhado.infraestrutura.mensageria.amqp import parametros

        return parametros(self.url(usuario), "teste")

    @contextmanager
    def canal(self, usuario: str = "admin") -> Iterator[Any]:
        conexao = pika.BlockingConnection(pika.URLParameters(self.url(usuario)))
        try:
            canal = conexao.channel()
            canal.confirm_delivery()
            yield canal
        finally:
            if conexao.is_open:
                conexao.close()

    def publicar_evento(
        self,
        envelope: Mapping[str, Any],
        *,
        usuario: str | None = None,
        routing_key: str | None = None,
        exchange: str = "pytstop.eventos",
        tipo: str | None = None,
        message_id: str | None = None,
        correlation_id: str | None = None,
        sem_user_id: bool = False,
        cabecalhos: Mapping[str, Any] | None = None,
        corpo: bytes | None = None,
        expiracao_ms: int | None = None,
        propriedades: type[pika.BasicProperties] = pika.BasicProperties,
    ) -> None:
        """Publica como o participante publicaria (``user_id`` = usuario).

        O produtor sai da ``origem`` do envelope; os demais argumentos montam as
        mensagens fora do contrato que os testes negativos precisam
        (``propriedades``: outra classe de propriedades, com outro encode).
        """
        produtor = usuario or _USUARIO_DA_ORIGEM[envelope["origem"]]
        tipo = tipo or envelope["tipo"]
        chave = routing_key or f"evento.{produtor}.{snake_case(envelope['tipo'])}"
        with self.canal(produtor) as canal:
            canal.basic_publish(
                exchange=exchange,
                routing_key=chave,
                body=corpo if corpo is not None else json.dumps(envelope).encode(),
                properties=propriedades(
                    message_id=message_id or envelope["id"],
                    correlation_id=correlation_id or envelope["correlation_id"],
                    type=tipo,
                    user_id=None if sem_user_id else produtor,
                    content_type="application/json",
                    delivery_mode=pika.DeliveryMode.Persistent,
                    headers=dict(cabecalhos or {}),
                    expiration=str(expiracao_ms) if expiracao_ms is not None else None,
                ),
                mandatory=True,
            )

    def pegar(self, fila: str) -> tuple[Any, bytes] | None:
        """Tira uma mensagem da fila (como admin): propriedades e corpo."""
        with self.canal() as canal:
            metodo, props, corpo = canal.basic_get(fila, auto_ack=True)
        return None if metodo is None else (props, corpo)

    def pegar_todas(self, fila: str) -> list[tuple[Any, bytes]]:
        """Esvazia a fila (como admin) numa conexao so, na ordem da fila."""
        mensagens: list[tuple[Any, bytes]] = []
        with self.canal() as canal:
            while True:
                metodo, props, corpo = canal.basic_get(fila, auto_ack=True)
                if metodo is None:
                    return mensagens
                mensagens.append((props, corpo))

    def contar(self, fila: str) -> int:
        with self.canal() as canal:
            total: int = canal.queue_declare(fila, passive=True).method.message_count
        return total

    def esvaziar(self) -> None:
        with self.canal() as canal:
            for fila in FILAS:
                canal.queue_purge(fila)

    def rabbitmqctl(self, *argumentos: str) -> str:
        codigo, saida = self.container.get_wrapped_container().exec_run(
            ["rabbitmqctl", *argumentos], user="999:999"
        )
        texto: str = saida.decode()
        assert codigo == 0, texto
        return texto

    def esperar_topologia(self, prazo_s: float = 90.0) -> None:
        # pytstop.retry so existe se o definitions.json foi importado no boot.
        def pronto() -> bool:
            try:
                with self.canal() as canal:
                    canal.exchange_declare("pytstop.retry", passive=True)
            except (AMQPError, OSError):
                return False
            return True

        esperar_ate(pronto, prazo_s=prazo_s, intervalo_s=0.5)


def _definitions_de_teste() -> str:
    """Copia do definitions.json com o TTL das filas de retry encurtado."""
    definitions = json.loads(json.dumps(_DEFINITIONS))
    for fila in definitions["queues"]:
        if "x-message-ttl" in fila["arguments"]:
            fila["arguments"]["x-message-ttl"] = TTL_DE_RETRY_MS
    _DEFINITIONS_DE_TESTE.parent.mkdir(parents=True, exist_ok=True)
    _DEFINITIONS_DE_TESTE.write_text(json.dumps(definitions))
    return str(_DEFINITIONS_DE_TESTE)


def subir_broker() -> tuple[DockerContainer, Broker]:
    container = DockerContainer(IMAGEM).with_exposed_ports(5672)
    # Mesmo usuario do compose da plataforma (o .erlang.cookie e do 999).
    container.with_kwargs(user="999:999", hostname="rabbitmq")
    for arquivo, destino in _MONTAGENS.items():
        origem = (
            _definitions_de_teste()
            if arquivo == "definitions.json"
            else str(_RABBITMQ / arquivo)
        )
        container.with_volume_mapping(origem, destino, "ro")
    container.start()
    broker = Broker(container)
    broker.esperar_topologia()
    ambiente = {
        "RABBITMQADMIN_TARGET_HOST": "localhost",
        "RABBITMQADMIN_TARGET_PORT": "15672",
        "RABBITMQADMIN_NON_INTERACTIVE_MODE": "true",
        "RABBITMQADMIN_USERNAME": "admin",
        "RABBITMQADMIN_PASSWORD": SENHAS["admin"],
        "RABBITMQ_OS_PASSWORD": SENHAS["os"],
        "RABBITMQ_BILLING_PASSWORD": SENHAS["billing"],
        "RABBITMQ_EXECUCAO_PASSWORD": SENHAS["execucao"],
    }
    codigo, saida = container.get_wrapped_container().exec_run(
        ["sh", "/scripts/criar-usuarios.sh"], environment=ambiente, user="999:999"
    )
    assert codigo == 0, saida.decode()
    return container, broker


class EsperasRegistradas(threading.Event):
    """``parar`` que anota cada espera (o timeout pedido) e quase nao dorme."""

    def __init__(self) -> None:
        super().__init__()
        self.esperas: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.esperas.append(timeout)
        return super().wait(0.001)


class EmSegundoPlano:
    """Roda ``processo.executar(parar)`` (relay ou consumidor) numa thread."""

    def __init__(self, processo: Any, parar: threading.Event | None = None) -> None:
        self._processo = processo
        self.parar = parar or threading.Event()
        self._erro: BaseException | None = None
        self._thread = threading.Thread(target=self._rodar, daemon=True)

    def _rodar(self) -> None:
        try:
            self._processo.executar(self.parar)
        except BaseException as exc:
            self._erro = exc

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.parar.set()
        self._thread.join(timeout=20)
        assert not self._thread.is_alive(), "o processo nao parou com o sinal"
        if self._erro is not None:
            raise self._erro
