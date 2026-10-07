"""Ciclo de vida da conexao AMQP de um processo: backoff, jitter e prontidao."""

from __future__ import annotations

import errno
import os
import socket
import ssl
import time
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from pika.adapters.utils.connection_workflow import AMQPConnectorStackTimeout
from pika.exceptions import AMQPConnectionError, ChannelClosedByBroker
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador
from tests.integracao.broker import EsperasRegistradas

if TYPE_CHECKING:
    from pathlib import Path

_URL = "amqp://os:segredo@rabbitmq:5672/%2F"  # gitleaks:allow


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


@pytest.fixture
def relogio(monkeypatch: pytest.MonkeyPatch) -> Relogio:
    relogio = Relogio()
    monkeypatch.setattr(amqp, "_relogio", relogio)
    # Sem jitter: a espera e o teto do sorteio, para conferir a sequencia.
    monkeypatch.setattr(amqp, "_sortear", lambda teto: teto)
    return relogio


def _conexao(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *resultados: Any,
    declarar: Any = None,
) -> amqp.ConexaoDoProcesso:
    fila = list(resultados)

    def conectar(_params: Any) -> Any:
        resultado = fila.pop(0)
        if isinstance(resultado, BaseException):
            raise resultado
        return resultado

    monkeypatch.setattr(amqp, "conectar", conectar)
    return amqp.ConexaoDoProcesso(
        amqp.parametros(_URL, "teste"),
        processo="teste",
        sinal=Sinalizador("teste", tmp_path),
        declarar=declarar or (lambda _canal: None),
    )


class _Aberta:
    is_open = True

    def __init__(self) -> None:
        self.atendida: list[float] = []

    def close(self) -> None:
        self.is_open = False

    def process_data_events(self, time_limit: float) -> None:
        self.atendida.append(time_limit)

    def add_on_connection_blocked_callback(self, callback: Any) -> None:
        self.ao_bloquear = callback

    def add_on_connection_unblocked_callback(self, callback: Any) -> None:
        self.ao_desbloquear = callback


@pytest.mark.usefixtures("relogio")
def test_espera_dobra_a_cada_falha_ate_o_teto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conexao = _conexao(tmp_path, monkeypatch)
    parar = EsperasRegistradas()

    for _ in range(7):
        conexao.esperar(parar)

    assert parar.esperas == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


@pytest.mark.usefixtures("relogio")
def test_sucesso_volta_a_espera_ao_minimo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conexao = _conexao(tmp_path, monkeypatch)
    parar = EsperasRegistradas()

    for _ in range(4):
        conexao.esperar(parar)
    conexao.sucesso()
    conexao.esperar(parar)

    assert parar.esperas == [1.0, 2.0, 4.0, 8.0, 1.0]


def test_conexao_que_durou_um_minuto_recomeca_do_minimo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relogio: Relogio
) -> None:
    conexao = _conexao(
        tmp_path, monkeypatch, (_Aberta(), object()), (_Aberta(), object())
    )
    parar = EsperasRegistradas()
    for _ in range(5):
        conexao.esperar(parar)

    # Caiu antes de um minuto: o atraso segue crescendo.
    assert conexao.conectar()
    relogio.agora += amqp.CONEXAO_ESTAVEL_S - 1
    conexao.esperar(parar)
    # Ficou um minuto de pe: a queda seguinte recomeca do minimo.
    assert conexao.conectar()
    relogio.agora += amqp.CONEXAO_ESTAVEL_S
    conexao.esperar(parar)

    assert parar.esperas == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 1.0]


def test_jitter_sorteia_entre_zero_e_o_atraso() -> None:
    esperas = [amqp._sortear(4.0) for _ in range(200)]

    assert all(0 <= espera <= 4.0 for espera in esperas)
    assert len(set(esperas)) > 100


def test_conectar_declara_e_marca_pronto_e_a_queda_tira_o_pronto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canal = object()
    declarados: list[Any] = []
    aberta = _Aberta()
    conexao = _conexao(
        tmp_path, monkeypatch, (aberta, canal), declarar=declarados.append
    )

    assert conexao.conectar()
    assert declarados == [canal]
    assert (conexao.conexao, conexao.canal) == (aberta, canal)
    assert (tmp_path / "teste-pronto").exists()

    conexao.desconectar()
    assert not aberta.is_open
    assert (conexao.conexao, conexao.canal) == (None, None)
    assert not (tmp_path / "teste-pronto").exists()


@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(AMQPConnectionError("recusada"), id="broker-fora"),
        pytest.param(ChannelClosedByBroker(404, "NOT_FOUND"), id="topologia-ausente"),
    ],
)
def test_broker_que_recusa_a_conexao_ou_a_declaracao_deixa_fora_de_pronto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, falha: BaseException
) -> None:
    aberta = _Aberta()

    def declarar(_canal: Any) -> None:
        raise falha

    conexao = _conexao(tmp_path, monkeypatch, (aberta, object()), declarar=declarar)
    (tmp_path / "teste-pronto").touch()

    assert conexao.conectar() is False
    assert not aberta.is_open
    assert conexao.canal is None
    assert not (tmp_path / "teste-pronto").exists()


# O pika deixa sair crus, sem embrulhar em AMQPError, dois erros da abertura que
# sao broker fora: o nome sem resolucao no DNS (o Service headless do broker sem
# pod pronto) e o prazo da pilha vencido (o broker aceitou o TCP e nao respondeu
# o AMQP).
@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            id="nome-sem-resolucao",
        ),
        pytest.param(AMQPConnectorStackTimeout("prazo da pilha"), id="broker-mudo"),
    ],
)
def test_erro_cru_da_abertura_que_e_broker_fora_deixa_fora_de_pronto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, falha: BaseException
) -> None:
    conexao = _conexao(tmp_path, monkeypatch, falha)
    (tmp_path / "teste-pronto").touch()

    assert conexao.conectar() is False
    assert not (tmp_path / "teste-pronto").exists()


# Os outros OSError crus da abertura sao defeito de ambiente ou de configuracao:
# reconectar em laco os esconderia; o processo cai e o Kubernetes o reinicia.
@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(
            OSError(errno.EMFILE, "Too many open files"), id="sem-descritores"
        ),
        pytest.param(
            ssl.SSLCertVerificationError(1, "certificate verify failed"),
            id="falha-de-tls",
        ),
    ],
)
def test_oserror_da_abertura_que_nao_e_do_nome_do_broker_sobe_cru(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, falha: OSError
) -> None:
    conexao = _conexao(tmp_path, monkeypatch, falha)

    with pytest.raises(type(falha), match=str(falha.strerror)):
        conexao.conectar()


def test_abertura_lenta_que_falha_toca_o_heartbeat_de_novo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "teste-heartbeat"
    heartbeat.touch()

    def conectar(_params: Any) -> Any:
        # O pika nao poe prazo na resolucao do nome: o heartbeat, tocado antes
        # da tentativa, envelhece enquanto ela dura.
        antigo = time.time() - 120
        os.utime(heartbeat, (antigo, antigo))
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    conexao = _conexao(tmp_path, monkeypatch)
    monkeypatch.setattr(amqp, "conectar", conectar)

    assert conexao.conectar() is False
    assert time.time() - heartbeat.stat().st_mtime < 5


def test_broker_que_aceita_o_tcp_e_nao_fala_amqp_fica_fora_de_pronto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O pika de verdade contra um socket que aceita a conexao (fila do sistema,
    # sem accept) e nunca responde: o prazo da pilha vence e sai cru.
    monkeypatch.setattr(amqp, "_log", structlog.get_logger())
    with socket.create_server(("127.0.0.1", 0)) as mudo:
        porta = mudo.getsockname()[1]
        url = f"amqp://os:segredo@127.0.0.1:{porta}/%2F"  # gitleaks:allow
        parametros = amqp.parametros(url, "teste")
        parametros.stack_timeout = 0.5
        conexao = amqp.ConexaoDoProcesso(
            parametros,
            processo="teste",
            sinal=Sinalizador("teste", tmp_path),
            declarar=lambda _canal: None,
        )

        with capture_logs() as logs:
            assert conexao.conectar() is False

    erros = [log["erro"] for log in logs if log["event"] == "broker unavailable"]
    assert erros == ["AMQPConnectorStackTimeout"]
    assert not (tmp_path / "teste-pronto").exists()


def test_connection_blocked_e_unblocked_do_broker_viram_o_estado_bloqueada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aberta = _Aberta()
    conexao = _conexao(tmp_path, monkeypatch, (aberta, object()), (_Aberta(), object()))
    assert conexao.conectar()

    aberta.ao_bloquear(aberta, object())
    assert conexao.bloqueada
    aberta.ao_desbloquear(aberta, object())
    assert not conexao.bloqueada

    # Conexao nova comeca desbloqueada.
    aberta.ao_bloquear(aberta, object())
    conexao.desconectar()
    assert conexao.conectar()
    assert not conexao.bloqueada


def test_atender_o_broker_toca_o_heartbeat_do_processo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Entre os lotes da limpeza o processo so atende o broker.
    aberta = _Aberta()
    conexao = _conexao(tmp_path, monkeypatch, (aberta, object()))
    assert conexao.conectar()
    assert not (tmp_path / "teste-heartbeat").exists()

    conexao.atender()

    assert aberta.atendida == [0]
    assert (tmp_path / "teste-heartbeat").exists()


@pytest.mark.usefixtures("relogio")
def test_cada_espera_conta_uma_reconexao_do_processo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conexao = _conexao(tmp_path, monkeypatch)
    antes = (
        REGISTRY.get_sample_value(
            "pytstop_reconexoes_ao_broker_total", {"processo": "teste"}
        )
        or 0.0
    )

    conexao.esperar(EsperasRegistradas())
    conexao.esperar(EsperasRegistradas())

    assert (
        REGISTRY.get_sample_value(
            "pytstop_reconexoes_ao_broker_total", {"processo": "teste"}
        )
        == antes + 2
    )
