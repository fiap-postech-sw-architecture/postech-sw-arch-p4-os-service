"""Adapter SMTP da ``EmailPort`` (ADR-036 "E-mail ao cliente"; ADR-018 do p3).

SMTP puro pela ``smtplib`` (stdlib), configurado por ambiente:

- ``SMTP_HOST`` (padrao ``localhost``);
- ``SMTP_PORT`` (padrao ``1025``, a porta SMTP do Mailpit);
- ``SMTP_FROM`` (padrao ``oficina@pytstop.local``).

Na demonstracao o servidor e o Mailpit (caixa de entrada web na 8025); outro
servidor so muda a configuracao. Sem credencial nem TLS: o Mailpit nao pede, e
credencial real entraria por Secret.

O envio roda no processo ``notificador``, longe da transacao da saga. O prazo de
5 s vale para cada operacao do socket: um servidor que aceita a conexao e nao
responde nao prende o laco do processo por mais que isso.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Final

from src.compartilhado.infraestrutura.ambiente import inteiro_do_ambiente
from src.ordem_servico.aplicacao.emails import FalhaEnvioEmailError

_TIMEOUT_SEGUNDOS: Final = 5
_PORTA_PADRAO: Final = 1025


class SmtpEmailAdapter:
    """Realiza ``EmailPort`` falando SMTP com o servidor configurado."""

    def __init__(self, host: str, porta: int, remetente: str) -> None:
        self._host = host
        self._porta = porta
        self._remetente = remetente

    @classmethod
    def do_ambiente(cls) -> SmtpEmailAdapter:
        """Le ``SMTP_*``; porta invalida aborta o boot (e nao cada entrega)."""
        return cls(
            host=os.environ.get("SMTP_HOST", "localhost"),
            porta=inteiro_do_ambiente("SMTP_PORT", _PORTA_PADRAO, minimo=1),
            remetente=os.environ.get("SMTP_FROM", "oficina@pytstop.local"),
        )

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        """Envia um e-mail de texto.

        Raises:
            FalhaEnvioEmailError: conexao, prazo ou protocolo. So o nome da
                excecao de origem segue: a mensagem dela pode trazer o endereco.
        """
        mensagem = EmailMessage()
        mensagem["From"] = self._remetente
        mensagem["To"] = destinatario
        mensagem["Subject"] = assunto
        mensagem.set_content(corpo)
        try:
            with smtplib.SMTP(self._host, self._porta, timeout=_TIMEOUT_SEGUNDOS) as c:
                c.send_message(mensagem)
        except (OSError, smtplib.SMTPException) as exc:
            raise FalhaEnvioEmailError(type(exc).__name__) from None
