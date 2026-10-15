"""Proxy TCP para o PostgreSQL do teste que o teste pode calar.

Calado, ele aceita a conexao e engole o que recebe sem responder: o banco que
"morreu" sem fechar o socket (no de verdade, uma particao de rede ou um no
travado). O prazo do servidor (``statement_timeout``) nao corta isso, porque o
servidor nem chega a ver o comando.
"""

from __future__ import annotations

import socket
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Self


class ProxyTcp:
    """Encaminha para ``destino`` ate o teste chamar ``calar()``."""

    def __init__(self, destino: tuple[str, int]) -> None:
        self._destino = destino
        self._escuta = socket.create_server(("127.0.0.1", 0))
        self.porta: int = self._escuta.getsockname()[1]
        self._mudo = threading.Event()
        self._sockets: list[socket.socket] = []
        threading.Thread(target=self._aceitar, daemon=True).start()

    def calar(self) -> None:
        """Daqui em diante nada passa em nenhum sentido, nem nas conexoes abertas."""
        self._mudo.set()

    def fechar(self) -> None:
        """Fecha tudo: quem estava preso num ``recv`` volta com erro."""
        self._escuta.close()
        for aberto in list(self._sockets):
            try:
                aberto.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            aberto.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.fechar()

    def _aceitar(self) -> None:
        while True:
            try:
                cliente, _ = self._escuta.accept()
            except OSError:
                return
            self._sockets.append(cliente)
            threading.Thread(target=self._atender, args=(cliente,), daemon=True).start()

    def _atender(self, cliente: socket.socket) -> None:
        servidor = None
        if not self._mudo.is_set():
            servidor = socket.create_connection(self._destino)
            self._sockets.append(servidor)
            threading.Thread(
                target=self._bombear, args=(servidor, cliente), daemon=True
            ).start()
        self._bombear(cliente, servidor)

    def _bombear(self, origem: socket.socket, destino: socket.socket | None) -> None:
        while True:
            try:
                dados = origem.recv(65536)
            except OSError:
                return
            if not dados:
                return
            if destino is None or self._mudo.is_set():
                continue
            try:
                destino.sendall(dados)
            except OSError:
                return
