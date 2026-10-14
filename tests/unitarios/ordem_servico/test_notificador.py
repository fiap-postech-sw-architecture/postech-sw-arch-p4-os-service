"""O laco do ``notificador`` com fila, SMTP, leitura e escuta falsos (ADR-036).

Cada desfecho de uma linha e do ciclo: o e-mail enviado, fechado sem envio, a
falha de envio, a falha inesperada, o banco fora e a linha que mudou de dono. A
fila de verdade (SQL, lease e atrasos) e o SMTP de verdade estao nos testes de
integracao. O laco recebe as pecas pelo construtor e roda pelo ``executar``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import structlog
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from src.compartilhado.infraestrutura.mensageria.outbox import ATRASOS_S
from src.ordem_servico.aplicacao.emails import Email
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.aplicacao.ports import FalhaEnvioEmailError
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura import notificador as modulo
from src.ordem_servico.infraestrutura.email_adapter import TIMEOUT_SEGUNDOS
from src.ordem_servico.infraestrutura.fila_de_notificacoes import LinhaDeNotificacao
from src.ordem_servico.infraestrutura.notificador import (
    ConfigNotificador,
    Notificador,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_EMAIL = Email("maria@cliente.com", "Assunto", "Corpo com o link https://x.test/t")
# Conectar, EHLO, MAIL, RCPT, DATA, fim dos dados e QUIT: cada operacao do envio
# pode levar o prazo do socket.
_OPERACOES_DO_ENVIO = 7


def _banco_fora() -> OperationalError:
    return OperationalError("SELECT contato FROM clientes", {}, Exception("fora"))


def _linha(tentativas: int = 0) -> LinhaDeNotificacao:
    return LinhaDeNotificacao(
        id=1,
        notificacao=Notificacao(
            uuid4(), ModeloDeNotificacao.SITUACAO, StatusOrdem.ENTREGUE
        ),
        tentativas=tentativas,
        lease_ate=datetime.now(UTC),
    )


class FilaFalsa:
    """Responde o que o teste programa e guarda o que o notificador pediu."""

    def __init__(self, *linhas: LinhaDeNotificacao) -> None:
        self.linhas = list(linhas)
        self.entregues: list[LinhaDeNotificacao] = []
        self.falhas: list[tuple[LinhaDeNotificacao, str]] = []
        self.marcar: bool | Exception = True
        self.registrar: str | Exception = "nova_tentativa"
        self.apagadas = 0
        self.limpezas = 0
        self.erro_ao_reivindicar: Exception | None = None

    def reivindicar(self, lease: timedelta) -> LinhaDeNotificacao | None:
        if self.erro_ao_reivindicar is not None:
            raise self.erro_ao_reivindicar
        return self.linhas.pop(0) if self.linhas else None

    def marcar_entregue(self, linha: LinhaDeNotificacao) -> bool:
        if isinstance(self.marcar, Exception):
            raise self.marcar
        self.entregues.append(linha)
        return self.marcar

    def registrar_falha(self, linha: LinhaDeNotificacao, motivo: str) -> str:
        if isinstance(self.registrar, Exception):
            raise self.registrar
        self.falhas.append((linha, motivo))
        return self.registrar

    def limpar(self, *, entre_lotes: Callable[[], None]) -> int:
        self.limpezas += 1
        entre_lotes()
        return self.apagadas


class SmtpFalso:
    def __init__(self) -> None:
        self.enviados: list[tuple[str, str, str]] = []
        self.erro: Exception | None = None
        self.ao_enviar: Callable[[], None] | None = None

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        if self.erro is not None:
            raise self.erro
        self.enviados.append((destinatario, assunto, corpo))
        if self.ao_enviar is not None:
            self.ao_enviar()


class Montagem:
    """O e-mail (ou o erro) que a leitura devolve a cada linha."""

    def __init__(self, resultado: Email | Exception | None) -> None:
        self.resultado = resultado

    def __call__(self, notificacao: Notificacao) -> Email | None:
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return self.resultado


class EscutaFalsa:
    """No lugar do LISTEN: cada espera fecha um ciclo; a ultima pede a parada."""

    def __init__(self) -> None:
        self.ciclos = 1
        self.esperas = 0
        self.fechada = False
        self.ao_esperar: Callable[[int], None] | None = None

    def esperar(self, parar: threading.Event, poll_s: float) -> None:
        self.esperas += 1
        if self.ao_esperar is not None:
            self.ao_esperar(self.esperas)
        if self.esperas >= self.ciclos:
            parar.set()

    def fechar(self) -> None:
        self.fechada = True


@dataclass
class Cenario:
    """O laco e as pecas falsas que ele recebeu."""

    notificador: Notificador
    fila: FilaFalsa
    smtp: SmtpFalso
    montagem: Montagem
    escuta: EscutaFalsa
    pasta: Path
    parar: threading.Event

    def rodar(self, ciclos: int = 1) -> None:
        """Roda o laco por ``ciclos`` esperas e para."""
        self.escuta.ciclos = ciclos
        self.notificador.executar(self.parar)

    def entregar(self, linha: LinhaDeNotificacao) -> None:
        """Um ciclo com a ``linha`` pronta para ser reivindicada."""
        self.fila.linhas.append(linha)
        self.rodar()


@pytest.fixture
def cenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cenario:
    # O logger de um modulo ja usado guarda a configuracao da primeira vez.
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    fila, smtp, montagem, escuta = (
        FilaFalsa(),
        SmtpFalso(),
        Montagem(_EMAIL),
        EscutaFalsa(),
    )
    notificador = Notificador(
        engine=MagicMock(),
        email=smtp,
        config=ConfigNotificador(poll_s=0.01, diretorio_de_saude=tmp_path),
        fila=fila,
        escuta=escuta,
        montar=montagem,
    )
    return Cenario(
        notificador, fila, smtp, montagem, escuta, tmp_path, threading.Event()
    )


def _log(eventos: list[dict[str, Any]], texto: str) -> list[dict[str, Any]]:
    return [e for e in eventos if e["event"] == texto]


def test_padroes_de_producao_do_laco() -> None:
    # `src/notificador.py` sobe o processo sem config: valem estes padroes.
    config = ConfigNotificador()

    assert config.poll_s == 5.0
    assert config.lease == timedelta(seconds=60)
    assert config.atrasos_s == ATRASOS_S
    # O lease nao e renovado: tem de cobrir o pior envio, e um SMTP mudo leva o
    # prazo do socket em cada operacao.
    assert config.lease.total_seconds() > _OPERACOES_DO_ENVIO * TIMEOUT_SEGUNDOS


def test_envia_o_e_mail_e_marca_a_linha_entregue(cenario: Cenario) -> None:
    linha = _linha()

    with capture_logs() as logs:
        cenario.entregar(linha)

    assert cenario.smtp.enviados == [
        (_EMAIL.destinatario, _EMAIL.assunto, _EMAIL.corpo)
    ]
    assert cenario.fila.entregues == [linha]
    (enviado,) = _log(logs, "notification sent")
    # Log so com a OS, o modelo e o resultado.
    assert set(enviado) == {"event", "log_level", "correlation_id", "modelo"}
    assert enviado["correlation_id"] == str(linha.notificacao.ordem_id)


def test_sem_e_mail_a_enviar_fecha_a_linha_sem_chamar_o_smtp(
    cenario: Cenario,
) -> None:
    cenario.montagem.resultado = None
    linha = _linha()

    with capture_logs() as logs:
        cenario.entregar(linha)

    assert cenario.smtp.enviados == []
    assert cenario.fila.entregues == [linha]
    assert len(_log(logs, "notification closed without sending")) == 1
    assert _log(logs, "notification sent") == []


@pytest.mark.parametrize(
    ("desfecho", "nivel", "evento"),
    [
        pytest.param(
            "nova_tentativa",
            "warning",
            "notification send failed; retry scheduled",
            id="nova-tentativa",
        ),
        pytest.param("dead", "error", "notification send failed; row dead", id="dead"),
        pytest.param(
            "perdida",
            "warning",
            "notification failure after losing the lease",
            id="perdida",
        ),
    ],
)
def test_falha_de_envio_conta_tentativa_e_o_log_so_leva_o_nome_da_excecao(
    cenario: Cenario, desfecho: str, nivel: str, evento: str
) -> None:
    cenario.smtp.erro = FalhaEnvioEmailError("ConnectionRefusedError")
    cenario.fila.registrar = desfecho
    linha = _linha(tentativas=2)

    with capture_logs() as logs:
        cenario.entregar(linha)

    assert cenario.fila.falhas == [
        (linha, "falha ao enviar o e-mail (ConnectionRefusedError)")
    ]
    assert cenario.fila.entregues == []
    (registro,) = _log(logs, evento)
    assert registro["log_level"] == nivel
    assert "maria@cliente.com" not in repr(logs)


def test_falha_inesperada_conta_tentativa_com_o_nome_da_excecao_e_o_lugar(
    cenario: Cenario,
) -> None:
    cenario.montagem.resultado = KeyError("segredo")

    with capture_logs() as logs:
        cenario.entregar(_linha())

    ((_, motivo),) = cenario.fila.falhas
    assert motivo == "falha ao enviar o e-mail (KeyError)"
    (registro,) = _log(logs, "notification send failed; retry scheduled")
    assert registro["erro"] == "KeyError"
    assert registro["onde"].startswith("test_notificador.py:")
    assert "segredo" not in repr(logs)


def test_banco_fora_na_leitura_nao_e_falha_da_linha(cenario: Cenario) -> None:
    cenario.montagem.resultado = _banco_fora()

    with capture_logs() as logs:
        cenario.entregar(_linha())

    # Nenhuma tentativa gasta: a linha volta quando o lease vencer.
    assert cenario.fila.falhas == []
    assert cenario.fila.entregues == []
    (ciclo,) = _log(logs, "notification cycle failed")
    assert ciclo["erro"] == "OperationalError"


def test_enviado_sem_a_marca_por_banco_fora_nao_gasta_tentativa(
    cenario: Cenario,
) -> None:
    cenario.fila.marcar = _banco_fora()

    with capture_logs() as logs:
        cenario.entregar(_linha())

    assert len(cenario.smtp.enviados) == 1
    assert cenario.fila.falhas == []
    assert (
        len(_log(logs, "notification sent but not marked; it returns after the lease"))
        == 1
    )


def test_envio_que_passou_do_lease_avisa(cenario: Cenario) -> None:
    cenario.fila.marcar = False

    with capture_logs() as logs:
        cenario.entregar(_linha())

    assert len(_log(logs, "notification sent after losing the lease")) == 1
    assert _log(logs, "notification sent") == []


def test_falha_que_nao_grava_por_banco_fora_avisa_sem_derrubar(
    cenario: Cenario,
) -> None:
    cenario.smtp.erro = FalhaEnvioEmailError("TimeoutError")
    cenario.fila.registrar = _banco_fora()

    with capture_logs() as logs:
        cenario.entregar(_linha())

    assert (
        len(_log(logs, "notification failure not recorded; it returns after the lease"))
        == 1
    )


def test_ciclo_que_chega_ao_fim_marca_pronto_e_o_encerramento_desmarca(
    cenario: Cenario,
) -> None:
    prontos: list[bool] = []
    cenario.escuta.ao_esperar = lambda _n: prontos.append(
        (cenario.pasta / "notificador-pronto").exists()
    )

    cenario.entregar(_linha())

    assert (cenario.pasta / "notificador-heartbeat").exists()
    # Pronto no fim do ciclo; encerrado, fora de pronto. A linha saiu antes.
    assert prontos == [True]
    assert not (cenario.pasta / "notificador-pronto").exists()
    assert len(cenario.fila.entregues) == 1
    # A limpeza das linhas antigas e do ciclo (uma vez por intervalo).
    assert cenario.fila.limpezas == 1


def test_banco_fora_no_ciclo_tira_de_pronto_loga_sem_o_sql_e_segue(
    cenario: Cenario,
) -> None:
    (cenario.pasta / "notificador-pronto").touch()
    cenario.montagem.resultado = _banco_fora()
    cenario.fila.linhas.extend([_linha(), _linha()])
    prontos: list[bool] = []

    def ao_esperar(ciclo: int) -> None:
        prontos.append((cenario.pasta / "notificador-pronto").exists())
        if ciclo == 1:
            # Segundo ciclo: o banco voltou.
            cenario.montagem.resultado = _EMAIL

    cenario.escuta.ao_esperar = ao_esperar

    with capture_logs() as logs:
        cenario.rodar(ciclos=2)

    # Com o banco fora o processo segue vivo e fora de pronto; na volta, pronto.
    assert prontos == [False, True]
    (falha,) = _log(logs, "notification cycle failed")
    assert falha["erro"] == "OperationalError"
    assert "clientes" not in repr(logs)
    assert len(cenario.fila.entregues) == 1


def test_limpeza_roda_uma_vez_por_intervalo_e_so_loga_quando_apagou(
    cenario: Cenario,
) -> None:
    cenario.fila.apagadas = 3

    with capture_logs() as logs:
        cenario.rodar(ciclos=2)

    assert cenario.fila.limpezas == 1
    (registro,) = _log(logs, "old notification rows deleted")
    assert registro["linhas"] == 3


def test_limpeza_que_nao_apagou_nada_nao_loga(cenario: Cenario) -> None:
    with capture_logs() as logs:
        cenario.rodar()

    assert cenario.fila.limpezas == 1
    assert _log(logs, "old notification rows deleted") == []


def test_pedido_de_parada_antes_do_ciclo_nao_reivindica_linha(
    cenario: Cenario,
) -> None:
    cenario.fila.linhas.append(_linha())
    cenario.parar.set()

    cenario.rodar()

    assert len(cenario.fila.linhas) == 1


def test_pedido_de_parada_no_meio_da_drenagem_nao_reivindica_a_seguinte(
    cenario: Cenario,
) -> None:
    cenario.fila.linhas.extend([_linha(), _linha()])
    # O SIGTERM chega durante o envio da primeira.
    cenario.smtp.ao_enviar = cenario.parar.set

    cenario.rodar()

    assert len(cenario.fila.entregues) == 1
    assert len(cenario.fila.linhas) == 1


def test_o_encerramento_fecha_a_escuta_do_notify(cenario: Cenario) -> None:
    cenario.rodar()

    assert cenario.escuta.fechada


def test_erro_inesperado_do_laco_sobe_mas_fecha_a_escuta_e_sai_de_pronto(
    cenario: Cenario,
) -> None:
    (cenario.pasta / "notificador-pronto").touch()
    cenario.fila.erro_ao_reivindicar = RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        cenario.rodar()

    assert cenario.escuta.fechada
    assert not (cenario.pasta / "notificador-pronto").exists()
