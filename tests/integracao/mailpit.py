"""Mailpit de teste: o SMTP de demonstracao do cluster e a caixa de entrada dele.

A mesma imagem do platform (``axllent/mailpit``). Os testes mandam e-mail pelo
``SmtpEmailAdapter`` e leem a caixa de entrada pela API HTTP do Mailpit, como o
E2E do platform fara.
"""

from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING, Any

from testcontainers.core.container import DockerContainer

from tests.integracao.broker import esperar_ate

if TYPE_CHECKING:
    from collections.abc import Callable

IMAGEM = "axllent/mailpit:v1.31.4"
_PRAZO_HTTP_S = 5


class Mailpit:
    """O container e a leitura da caixa de entrada dele."""

    def __init__(self, container: DockerContainer) -> None:
        self.container = container
        self.host = container.get_container_host_ip()
        self.porta_smtp = int(container.get_exposed_port(1025))
        self._http = f"http://{self.host}:{int(container.get_exposed_port(8025))}"

    def _pedir(self, caminho: str, metodo: str = "GET") -> Any:  # JSON da API
        pedido = urllib.request.Request(  # noqa: S310  # URL do container de teste
            f"{self._http}{caminho}", method=metodo
        )
        with urllib.request.urlopen(pedido, timeout=_PRAZO_HTTP_S) as resposta:  # noqa: S310
            corpo = resposta.read()
        # O DELETE responde `ok` em texto; o resto, JSON.
        return json.loads(corpo) if corpo.startswith((b"{", b"[")) else None

    def mensagens(self) -> list[dict[str, Any]]:
        """As mensagens da caixa de entrada, da mais nova para a mais antiga."""
        resposta = self._pedir("/api/v1/messages")
        mensagens: list[dict[str, Any]] = resposta["messages"]
        return mensagens

    def texto(self, mensagem: dict[str, Any]) -> str:
        """O corpo de texto da mensagem."""
        detalhe = self._pedir(f"/api/v1/message/{mensagem['ID']}")
        texto: str = detalhe["Text"]
        return texto

    def esperar(
        self, quantidade: int, *, prazo_s: float = 20.0
    ) -> list[dict[str, Any]]:
        """Espera ``quantidade`` mensagens na caixa e as devolve."""
        return list(
            esperar_ate(
                lambda: (m := self.mensagens()) and len(m) >= quantidade and m,
                prazo_s=prazo_s,
            )
        )

    def apagar(self) -> None:
        self._pedir("/api/v1/messages", "DELETE")


def subir_mailpit() -> tuple[DockerContainer, Mailpit]:
    container = DockerContainer(IMAGEM).with_exposed_ports(1025, 8025)
    container.start()
    mailpit = Mailpit(container)
    pronto: Callable[[], bool] = lambda: _responde(mailpit)  # noqa: E731
    esperar_ate(pronto, prazo_s=60, intervalo_s=0.3)
    return container, mailpit


def _responde(mailpit: Mailpit) -> bool:
    try:
        mailpit.mensagens()
    except OSError:
        return False
    return True
