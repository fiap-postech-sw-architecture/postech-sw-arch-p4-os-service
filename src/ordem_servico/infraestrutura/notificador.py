"""O laco do processo ``notificador``: entrega os e-mails da tabela ``notificacoes``.

A transicao da OS grava o pedido na mesma transacao (``notificacoes_mapping``);
aqui, fora dela, cada pedido vira um e-mail: le a OS e o contato do cliente
(``MontarEmail``, numa transacao de leitura que fecha antes do envio) e fala
SMTP. SMTP lento ou fora so atrasa e-mails: nao prende a saga, a outbox nem o
AMQP, que nao passam por este processo (ADR-036 "E-mail ao cliente").

Falha de envio conta uma tentativa da linha, com os atrasos da outbox, ate
``dead`` na quinta; a ordem vale so entre os e-mails da mesma OS. O banco fora
deixa o processo fora de pronto, sem reinicia-lo, e as linhas voltam no ciclo
seguinte (ou quando o lease vencer), sem gastar tentativa. Nao ha metrica nem
alerta proprios: o e-mail e complementar e nao segura a saga, e a linha ``dead``
fica no log e na tabela.

Log so com ``correlation_id`` (a OS), ``modelo`` e o resultado: nunca endereco,
nome, ``link_decisao`` nem ``checkout_url``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Literal, Protocol

import structlog
from sqlalchemy.exc import SQLAlchemyError

from src.compartilhado.infraestrutura.database import criar_session_factory
from src.compartilhado.infraestrutura.escuta import EscutaDeCanal
from src.compartilhado.infraestrutura.mensageria.outbox import ATRASOS_S
from src.compartilhado.infraestrutura.mensageria.processo import (
    DIRETORIO_DE_SAUDE,
    INTERVALO_DE_LIMPEZA_S,
    Agenda,
    Sinalizador,
    onde,
)
from src.ordem_servico.aplicacao.emails import MontarEmail
from src.ordem_servico.aplicacao.ports import FalhaEnvioEmailError
from src.ordem_servico.infraestrutura.adapters import ClienteSQLAlchemyAdapter
from src.ordem_servico.infraestrutura.fila_de_notificacoes import FilaDeNotificacoes
from src.ordem_servico.infraestrutura.notificacoes_mapping import CANAL_NOTIFICACAO
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy import Engine

    from src.ordem_servico.aplicacao.emails import Email
    from src.ordem_servico.aplicacao.notificacoes import Notificacao
    from src.ordem_servico.aplicacao.ports import EmailPort
    from src.ordem_servico.infraestrutura.fila_de_notificacoes import (
        LinhaDeNotificacao,
    )

_log = structlog.get_logger(__name__)

# O envio de uma linha leva no maximo algumas operacoes de socket de 5 s: o lease
# de uma linha so precisa cobrir isso, e a liveness do Kubernetes (90 s) fica
# longe do heartbeat por linha.
_LEASE_PADRAO_S: Final = 60
_POLL_PADRAO_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class ConfigNotificador:
    """Ajustes do laco; os padroes sao os de producao."""

    poll_s: float = _POLL_PADRAO_S
    lease: timedelta = timedelta(seconds=_LEASE_PADRAO_S)
    # Um atraso para cada falha antes da que leva a `dead` (os da outbox).
    atrasos_s: tuple[float, ...] = ATRASOS_S
    diretorio_de_saude: Path = DIRETORIO_DE_SAUDE


class _Fila(Protocol):
    """Os passos do laco sobre a tabela (``FilaDeNotificacoes``)."""

    def reivindicar(self, lease: timedelta) -> LinhaDeNotificacao | None: ...

    def marcar_entregue(self, linha: LinhaDeNotificacao) -> bool: ...

    def registrar_falha(
        self, linha: LinhaDeNotificacao, motivo: str
    ) -> Literal["dead", "nova_tentativa", "perdida"]: ...

    def limpar(self, *, entre_lotes: Callable[[], None]) -> int: ...


class _Escuta(Protocol):
    """A espera entre os ciclos (``EscutaDeCanal``)."""

    def esperar(self, parar: threading.Event, poll_s: float) -> None: ...

    def fechar(self) -> None: ...


class Notificador:
    """Entrega a tabela ``notificacoes`` por SMTP ate o ``parar`` (SIGTERM).

    A fila, a escuta do ``NOTIFY`` e a montagem do e-mail saem do ``engine`` por
    padrao; quem testa o laco os injeta.
    """

    def __init__(  # noqa: PLR0913  # colaboradores, todos por palavra-chave
        self,
        *,
        engine: Engine,
        email: EmailPort,
        config: ConfigNotificador | None = None,
        fila: _Fila | None = None,
        escuta: _Escuta | None = None,
        montar: Callable[[Notificacao], Email | None] | None = None,
    ) -> None:
        self._email = email
        self._config = config or ConfigNotificador()
        self._fila = fila or FilaDeNotificacoes(engine, self._config.atrasos_s)
        self._escuta = escuta or EscutaDeCanal(engine, CANAL_NOTIFICACAO)
        self._sessoes = criar_session_factory(engine)
        self._montar = montar or self._montar_no_banco
        self._sinal = Sinalizador("notificador", self._config.diretorio_de_saude)
        self._limpeza = Agenda(INTERVALO_DE_LIMPEZA_S)

    def executar(self, parar: threading.Event) -> None:
        """Laco principal; o banco fora nao derruba o processo."""
        _log.info(
            "notifier started",
            poll_s=self._config.poll_s,
            lease_s=self._config.lease.total_seconds(),
        )
        try:
            while not parar.is_set():
                self._sinal.bater()
                try:
                    self._drenar(parar)
                    self._limpar_se_devido()
                except SQLAlchemyError as exc:
                    # Sem o SQL nem os parametros da excecao: a leitura do
                    # contato esta no caminho.
                    _log.error(
                        "notification cycle failed",
                        erro=type(exc).__name__,
                        onde=onde(exc),
                    )
                    self._sinal.marcar_nao_pronto()
                else:
                    self._sinal.marcar_pronto()
                self._escuta.esperar(parar, self._config.poll_s)
        finally:
            self._escuta.fechar()
            self._sinal.marcar_nao_pronto()
            _log.info("notifier stopped")

    def _drenar(self, parar: threading.Event) -> None:
        """Entrega linha a linha ate nao sobrar nenhuma vencida."""
        while not parar.is_set():
            self._sinal.bater()
            linha = self._fila.reivindicar(self._config.lease)
            if linha is None:
                return
            self._entregar(linha)

    def _entregar(self, linha: LinhaDeNotificacao) -> None:
        contexto = {
            "correlation_id": str(linha.notificacao.ordem_id),
            "modelo": linha.notificacao.modelo.value,
        }
        try:
            email = self._montar(linha.notificacao)
            if email is not None:
                self._email.enviar(email.destinatario, email.assunto, email.corpo)
        except FalhaEnvioEmailError as exc:
            # A mensagem e so o nome da excecao de origem (o endereco nao sai).
            self._falhou(linha, str(exc), contexto)
        except SQLAlchemyError:
            # Banco fora na leitura: nao e falha da linha, que volta com o lease.
            raise
        except Exception as exc:  # noqa: BLE001  # a linha falha, o laco segue
            self._falhou(linha, type(exc).__name__, contexto, onde=onde(exc))
        else:
            self._entregue(linha, contexto, enviado=email is not None)

    def _montar_no_banco(self, notificacao: Notificacao) -> Email | None:
        """O e-mail do pedido; a transacao de leitura fecha antes do SMTP."""
        with self._sessoes() as sessao:
            return MontarEmail(
                OrdemDeServicoSQLAlchemyRepository(sessao),
                ClienteSQLAlchemyAdapter(sessao),
            ).executar(notificacao)

    def _entregue(
        self, linha: LinhaDeNotificacao, contexto: dict[str, str], *, enviado: bool
    ) -> None:
        try:
            marcada = self._fila.marcar_entregue(linha)
        except SQLAlchemyError:
            # Enviada e sem a marca (banco fora): nao gasta tentativa. A linha
            # volta quando o lease vencer e o e-mail pode sair de novo.
            _log.warning(
                "notification sent but not marked; it returns after the lease",
                **contexto,
            )
            return
        if not marcada:
            # O envio passou do lease e outra replica pegou a linha.
            _log.warning("notification sent after losing the lease", **contexto)
        elif enviado:
            _log.info("notification sent", **contexto)
        else:
            _log.info("notification closed without sending", **contexto)

    def _falhou(
        self,
        linha: LinhaDeNotificacao,
        erro: str,
        contexto: dict[str, str],
        **extras: str,
    ) -> None:
        try:
            desfecho = self._fila.registrar_falha(
                linha, f"falha ao enviar o e-mail ({erro})"
            )
        except SQLAlchemyError:
            _log.warning(
                "notification failure not recorded; it returns after the lease",
                **contexto,
            )
            return
        tentativas = linha.tentativas + 1
        if desfecho == "perdida":
            _log.warning("notification failure after losing the lease", **contexto)
        elif desfecho == "dead":
            _log.error(
                "notification send failed; row dead",
                tentativas=tentativas,
                erro=erro,
                **extras,
                **contexto,
            )
        else:
            _log.warning(
                "notification send failed; retry scheduled",
                tentativas=tentativas,
                erro=erro,
                **extras,
                **contexto,
            )

    def _limpar_se_devido(self) -> None:
        if not self._limpeza.devida():
            return
        apagadas = self._fila.limpar(entre_lotes=self._sinal.bater)
        if apagadas:
            _log.info("old notification rows deleted", linhas=apagadas)
