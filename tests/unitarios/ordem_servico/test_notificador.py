"""O laco do ``notificador`` com fila, SMTP e leitura falsos (ADR-036).

Cada desfecho de uma linha e do ciclo: o e-mail enviado, fechado sem envio, a
falha de envio, a falha inesperada, o banco fora e a linha que mudou de dono. A
fila de verdade (SQL, lease e atrasos) e o SMTP de verdade estao nos testes de
integracao.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import structlog
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from src.ordem_servico.aplicacao.emails import Email, FalhaEnvioEmailError
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura import notificador as modulo
from src.ordem_servico.infraestrutura.fila_de_notificacoes import LinhaDeNotificacao
from src.ordem_servico.infraestrutura.notificador import (
    ConfigNotificador,
    Notificador,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_EMAIL = Email("maria@cliente.com", "Assunto", "Corpo com o link https://x.test/t")


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

    def reivindicar(self, lease: object) -> LinhaDeNotificacao | None:
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

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        if self.erro is not None:
            raise self.erro
        self.enviados.append((destinatario, assunto, corpo))


class Montagem:
    """O que o ``_montar`` devolve (ou levanta) a cada linha."""

    def __init__(self, resultado: Email | Exception | None) -> None:
        self.resultado = resultado

    def __call__(self, notificacao: Notificacao) -> Email | None:
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return self.resultado


@pytest.fixture
def notificador(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Notificador:
    # O logger de um modulo ja usado guarda a configuracao da primeira vez.
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    instancia = Notificador(
        engine=MagicMock(),
        email=SmtpFalso(),
        config=ConfigNotificador(poll_s=0.01, diretorio_de_saude=tmp_path),
    )
    instancia._fila = FilaFalsa()  # type: ignore[assignment]
    instancia._montar = Montagem(_EMAIL)  # type: ignore[method-assign]
    return instancia


def _fila(n: Notificador) -> FilaFalsa:
    return n._fila  # type: ignore[return-value]


def _smtp(n: Notificador) -> SmtpFalso:
    return n._email  # type: ignore[return-value]


def _log(eventos: list[dict[str, Any]], texto: str) -> list[dict[str, Any]]:
    return [e for e in eventos if e["event"] == texto]


def test_envia_o_e_mail_e_marca_a_linha_entregue(notificador: Notificador) -> None:
    linha = _linha()

    with capture_logs() as logs:
        notificador._entregar(linha)

    assert _smtp(notificador).enviados == [
        (_EMAIL.destinatario, _EMAIL.assunto, _EMAIL.corpo)
    ]
    assert _fila(notificador).entregues == [linha]
    (enviado,) = _log(logs, "notification sent")
    # Log so com a OS, o modelo e o resultado.
    assert set(enviado) == {"event", "log_level", "correlation_id", "modelo"}
    assert enviado["correlation_id"] == str(linha.notificacao.ordem_id)


def test_sem_e_mail_a_enviar_fecha_a_linha_sem_chamar_o_smtp(
    notificador: Notificador,
) -> None:
    notificador._montar = Montagem(None)  # type: ignore[method-assign]
    linha = _linha()

    with capture_logs() as logs:
        notificador._entregar(linha)

    assert _smtp(notificador).enviados == []
    assert _fila(notificador).entregues == [linha]
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
    notificador: Notificador, desfecho: str, nivel: str, evento: str
) -> None:
    _smtp(notificador).erro = FalhaEnvioEmailError("ConnectionRefusedError")
    _fila(notificador).registrar = desfecho
    linha = _linha(tentativas=2)

    with capture_logs() as logs:
        notificador._entregar(linha)

    assert _fila(notificador).falhas == [
        (linha, "falha ao enviar o e-mail (ConnectionRefusedError)")
    ]
    assert _fila(notificador).entregues == []
    (registro,) = _log(logs, evento)
    assert registro["log_level"] == nivel
    assert "maria@cliente.com" not in repr(logs)


def test_falha_inesperada_conta_tentativa_com_o_nome_da_excecao_e_o_lugar(
    notificador: Notificador,
) -> None:
    notificador._montar = Montagem(KeyError("segredo"))  # type: ignore[method-assign]

    with capture_logs() as logs:
        notificador._entregar(_linha())

    ((_, motivo),) = _fila(notificador).falhas
    assert motivo == "falha ao enviar o e-mail (KeyError)"
    (registro,) = _log(logs, "notification send failed; retry scheduled")
    assert registro["erro"] == "KeyError"
    assert registro["onde"].startswith("test_notificador.py:")
    assert "segredo" not in repr(logs)


def test_banco_fora_na_leitura_nao_e_falha_da_linha(notificador: Notificador) -> None:
    notificador._montar = Montagem(_banco_fora())  # type: ignore[method-assign]

    with pytest.raises(OperationalError):
        notificador._entregar(_linha())

    # Nenhuma tentativa gasta: a linha volta quando o lease vencer.
    assert _fila(notificador).falhas == []
    assert _fila(notificador).entregues == []


def test_enviado_sem_a_marca_por_banco_fora_nao_gasta_tentativa(
    notificador: Notificador,
) -> None:
    _fila(notificador).marcar = _banco_fora()

    with capture_logs() as logs:
        notificador._entregar(_linha())

    assert len(_smtp(notificador).enviados) == 1
    assert _fila(notificador).falhas == []
    assert (
        len(_log(logs, "notification sent but not marked; it returns after the lease"))
        == 1
    )


def test_envio_que_passou_do_lease_avisa(notificador: Notificador) -> None:
    _fila(notificador).marcar = False

    with capture_logs() as logs:
        notificador._entregar(_linha())

    assert len(_log(logs, "notification sent after losing the lease")) == 1
    assert _log(logs, "notification sent") == []


def test_falha_que_nao_grava_por_banco_fora_avisa_sem_derrubar(
    notificador: Notificador,
) -> None:
    _smtp(notificador).erro = FalhaEnvioEmailError("TimeoutError")
    _fila(notificador).registrar = _banco_fora()

    with capture_logs() as logs:
        notificador._entregar(_linha())

    assert (
        len(_log(logs, "notification failure not recorded; it returns after the lease"))
        == 1
    )


def _executar(notificador: Notificador, ciclos: int) -> threading.Event:
    """Roda o laco por ``ciclos`` esperas e para."""
    parar = threading.Event()
    esperas = 0

    def esperar(evento: threading.Event, poll_s: float) -> None:
        nonlocal esperas
        esperas += 1
        if esperas >= ciclos:
            evento.set()

    notificador._escuta = MagicMock(esperar=esperar)  # type: ignore[assignment]
    notificador.executar(parar)
    return parar


def test_ciclo_que_chega_ao_fim_marca_pronto_e_o_encerramento_desmarca(
    notificador: Notificador, tmp_path: Path
) -> None:
    _fila(notificador).linhas.append(_linha())

    _executar(notificador, ciclos=1)

    assert (tmp_path / "notificador-heartbeat").exists()
    # Encerrado: fora de pronto, e a linha foi entregue antes.
    assert not (tmp_path / "notificador-pronto").exists()
    assert len(_fila(notificador).entregues) == 1


def test_banco_fora_no_ciclo_tira_de_pronto_loga_sem_o_sql_e_segue(
    notificador: Notificador, tmp_path: Path
) -> None:
    (tmp_path / "notificador-pronto").touch()
    notificador._montar = Montagem(_banco_fora())  # type: ignore[method-assign]
    _fila(notificador).linhas.extend([_linha(), _linha()])
    prontos: list[bool] = []

    def esperar(evento: threading.Event, poll_s: float) -> None:
        prontos.append((tmp_path / "notificador-pronto").exists())
        if len(prontos) == 1:
            # Segundo ciclo: o banco voltou.
            notificador._montar = Montagem(_EMAIL)  # type: ignore[method-assign]
        else:
            evento.set()

    notificador._escuta = MagicMock(esperar=esperar)  # type: ignore[assignment]

    with capture_logs() as logs:
        notificador.executar(threading.Event())

    # Com o banco fora o processo segue vivo e fora de pronto; na volta, pronto.
    assert prontos == [False, True]
    (falha,) = _log(logs, "notification cycle failed")
    assert falha["erro"] == "OperationalError"
    assert "clientes" not in repr(logs)
    assert len(_fila(notificador).entregues) == 1


def test_limpeza_roda_uma_vez_por_intervalo_e_so_loga_quando_apagou(
    notificador: Notificador,
) -> None:
    _fila(notificador).apagadas = 3

    with capture_logs() as logs:
        notificador._limpar_se_devido()
        notificador._limpar_se_devido()

    assert _fila(notificador).limpezas == 1
    (registro,) = _log(logs, "old notification rows deleted")
    assert registro["linhas"] == 3


def test_limpeza_que_nao_apagou_nada_nao_loga(notificador: Notificador) -> None:
    with capture_logs() as logs:
        notificador._limpar_se_devido()

    assert _fila(notificador).limpezas == 1
    assert _log(logs, "old notification rows deleted") == []


def test_pedido_de_parada_antes_do_ciclo_nao_reivindica_linha(
    notificador: Notificador,
) -> None:
    _fila(notificador).linhas.append(_linha())
    parar = threading.Event()
    parar.set()

    notificador._drenar(parar)

    assert len(_fila(notificador).linhas) == 1
