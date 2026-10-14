"""Composicao do processo ``src.prazos``, o laco dele e a presenca do consumidor."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
import structlog
from pika.exceptions import AMQPConnectionError, ChannelClosedByBroker
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

import src.prazos as processo_prazos
from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador
from src.ordem_servico.aplicacao.ports import SituacaoDaFila
from src.ordem_servico.aplicacao.saga.vencer_prazos import ResultadoDoCiclo
from src.ordem_servico.infraestrutura import (
    presenca_do_consumidor,
    processo_dos_prazos,
)
from src.ordem_servico.infraestrutura.presenca_do_consumidor import (
    PresencaPikaDoConsumidor,
)
from src.ordem_servico.infraestrutura.processo_dos_prazos import ProcessoDosPrazos


class _Processo:
    """``ProcessoDosPrazos`` falso: guarda como foi montado e se rodou."""

    criados: ClassVar[list[_Processo]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.executou = False
        _Processo.criados.append(self)

    def executar(self, parar: object) -> None:
        self.executou = True


@pytest.fixture
def boot_falso(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    registro: dict[str, Any] = {"engine": MagicMock(), "parametros": object()}
    _Processo.criados = []
    monkeypatch.setattr(processo_prazos, "instalar_sinais", lambda parar: None)
    monkeypatch.setattr(
        processo_prazos,
        "preparar",
        lambda nome: (
            registro.setdefault("processos", []).append(nome)
            or (registro["engine"], registro["parametros"])
        ),
    )
    monkeypatch.setattr(processo_prazos, "criar_tracer", lambda nome: MagicMock())
    monkeypatch.setattr(processo_prazos, "subir_metricas", lambda: 9100)
    monkeypatch.setattr(processo_prazos, "criar_session_factory", lambda e: MagicMock())
    monkeypatch.setattr(
        processo_prazos, "PresencaPikaDoConsumidor", lambda *a: MagicMock()
    )
    monkeypatch.setattr(processo_prazos, "ProcessoDosPrazos", _Processo)
    return registro


def test_main_monta_executa_e_fecha_o_banco(boot_falso: dict[str, Any]) -> None:
    processo_prazos.main()

    (processo,) = _Processo.criados
    assert boot_falso["processos"] == ["prazos"]
    assert processo.executou
    assert processo.kwargs["intervalo_s"] == 30
    vencer = processo.kwargs["vencer"]
    assert vencer._max_reenvios == 5
    assert vencer._contexto.prazo_resposta == timedelta(seconds=120)
    boot_falso["engine"].dispose.assert_called_once()


def test_main_le_os_tres_parametros_do_ambiente(
    boot_falso: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "2")
    monkeypatch.setenv("SAGA_MAX_REENVIOS", "3")
    monkeypatch.setenv("PRAZOS_INTERVALO_SEGUNDOS", "0.5")

    processo_prazos.main()

    (processo,) = _Processo.criados
    vencer = processo.kwargs["vencer"]
    assert (
        vencer._contexto.prazo_resposta,
        vencer._max_reenvios,
        processo.kwargs["intervalo_s"],
    ) == (timedelta(seconds=2), 3, 0.5)


@pytest.mark.parametrize(
    ("variavel", "valor"),
    [
        ("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "0"),
        ("SAGA_MAX_REENVIOS", "-1"),
        ("PRAZOS_INTERVALO_SEGUNDOS", "301"),
    ],
)
def test_parametro_invalido_aborta_o_boot_antes_de_abrir_banco_e_broker(
    boot_falso: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    variavel: str,
    valor: str,
) -> None:
    monkeypatch.setenv(variavel, valor)

    with pytest.raises(RuntimeError, match=variavel):
        processo_prazos.main()

    assert "processos" not in boot_falso
    assert _Processo.criados == []


class _Ciclo:
    """``VencerPrazos`` falso: devolve (ou levanta) o que o teste programa."""

    def __init__(self, parar: threading.Event, *resultados: Any) -> None:
        self._parar = parar
        self._resultados = list(resultados)
        self.chamadas = 0
        self.tratadas: list[Any] = []

    def executar(self, ao_tratar: Any) -> ResultadoDoCiclo:
        self.chamadas += 1
        self.tratadas.append(ao_tratar)
        ao_tratar()
        resultado = self._resultados.pop(0)
        if not self._resultados:
            self._parar.set()
        if isinstance(resultado, Exception):
            raise resultado
        assert isinstance(resultado, ResultadoDoCiclo)
        return resultado


def _processo(
    tmp_path: Path, ciclo: _Ciclo, intervalo_s: float = 0.01
) -> tuple[ProcessoDosPrazos, MagicMock, Sinalizador]:
    presenca = MagicMock()
    sinal = Sinalizador("prazos", tmp_path)
    return (
        ProcessoDosPrazos(
            vencer=ciclo,
            presenca=presenca,
            sinal=sinal,
            intervalo_s=intervalo_s,  # type: ignore[arg-type]
        ),
        presenca,
        sinal,
    )


def test_laco_roda_um_ciclo_por_intervalo_toca_o_heartbeat_e_fecha_a_presenca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processo_dos_prazos, "_log", structlog.get_logger())
    parar = threading.Event()
    ciclo = _Ciclo(
        parar,
        ResultadoDoCiclo(),
        ResultadoDoCiclo(reenviadas=2, esgotadas=1),
        ResultadoDoCiclo(pausado=True),
    )
    processo, presenca, sinal = _processo(tmp_path, ciclo)

    with capture_logs() as logs:
        processo.executar(parar)

    assert ciclo.chamadas == 3
    assert sinal.heartbeat.exists()
    presenca.fechar.assert_called_once()
    assert presenca.manter.called
    # So o ciclo com trabalho vai para o log; o ocioso e o pausado, nao.
    feitos = [e for e in logs if e["event"] == "deadline cycle done"]
    assert [(e["reenviadas"], e["esgotadas"], e["recusadas"]) for e in feitos] == [
        (2, 1, 0)
    ]
    eventos = [e["event"] for e in logs]
    assert (eventos[0], eventos[-1]) == ("prazos started", "prazos stopped")


def test_cada_saga_tratada_atende_o_broker_alem_de_tocar_o_heartbeat(
    tmp_path: Path,
) -> None:
    # Um ciclo longo nao pode passar dos heartbeats AMQP sem atender o broker.
    parar = threading.Event()
    ciclo = _Ciclo(parar, ResultadoDoCiclo(reenviadas=3))
    processo, presenca, sinal = _processo(tmp_path, ciclo, intervalo_s=3600)

    processo.executar(parar)

    assert ciclo.tratadas == [processo._depois_de_cada_saga]
    # Uma vez pelo ciclo simulado; a espera nao rodou (o `parar` ja estava ligado).
    assert presenca.manter.call_count == 1
    assert sinal.heartbeat.exists()


def test_banco_fora_nao_derruba_o_processo_e_o_ciclo_seguinte_tenta_de_novo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processo_dos_prazos, "_log", structlog.get_logger())
    parar = threading.Event()
    ciclo = _Ciclo(
        parar,
        OperationalError("SELECT 1", {}, Exception("banco fora")),
        ResultadoDoCiclo(reenviadas=1),
    )
    processo, presenca, _ = _processo(tmp_path, ciclo)

    with capture_logs() as logs:
        processo.executar(parar)

    assert ciclo.chamadas == 2
    assert "deadline cycle failed" in [e["event"] for e in logs]
    presenca.fechar.assert_called_once()


def test_sigterm_interrompe_a_espera_entre_os_ciclos(tmp_path: Path) -> None:
    parar = threading.Event()
    ciclo = _Ciclo(parar, ResultadoDoCiclo(), ResultadoDoCiclo())
    # Intervalo de uma hora: so o `parar` faz o laco sair no tempo do teste.
    processo, _, _ = _processo(tmp_path, ciclo, intervalo_s=3600)
    parar.set()

    processo.executar(parar)

    assert ciclo.chamadas == 0


class _Canal:
    def __init__(self, consumidores: int | Exception, prontas: int = 0) -> None:
        self._consumidores = consumidores
        self._prontas = prontas

    def queue_declare(self, fila: str, *, passive: bool) -> Any:
        assert (fila, passive) == ("os.eventos", True)
        if isinstance(self._consumidores, Exception):
            raise self._consumidores
        return MagicMock(
            method=MagicMock(
                consumer_count=self._consumidores, message_count=self._prontas
            )
        )


class _Broker:
    """``ConexaoDoProcesso`` falsa: conecta (ou nao) e entrega o canal pedido."""

    def __init__(self, canal: _Canal | None, *, conecta: bool = True) -> None:
        self.canal: _Canal | None = None
        self.conexao: object | None = None
        self._proximo = canal
        self._conecta = conecta
        self.desconexoes = 0
        self.atendeu = 0
        self.erro_ao_atender: Exception | None = None

    def conectar(self) -> bool:
        if self._conecta:
            self.canal, self.conexao = self._proximo, object()
        return self._conecta

    def desconectar(self) -> None:
        self.desconexoes += 1
        self.canal = self.conexao = None

    def atender(self) -> None:
        self.atendeu += 1
        if self.erro_ao_atender is not None:
            raise self.erro_ao_atender


def _presenca(
    monkeypatch: pytest.MonkeyPatch, broker: _Broker
) -> PresencaPikaDoConsumidor:
    monkeypatch.setattr(presenca_do_consumidor, "_log", structlog.get_logger())
    monkeypatch.setattr(
        presenca_do_consumidor.amqp, "ConexaoDoProcesso", lambda *a, **k: broker
    )
    return PresencaPikaDoConsumidor(MagicMock(), MagicMock())


@pytest.mark.parametrize(
    ("consumidores", "prontas", "em_dia"),
    [
        pytest.param(0, 0, False, id="sem-consumidor"),
        pytest.param(1, 0, True, id="um-consumidor"),
        pytest.param(3, 0, True, id="tres-consumidores"),
        pytest.param(1, 7, False, id="com-consumidor-e-mensagens-prontas"),
        pytest.param(0, 7, False, id="sem-consumidor-e-mensagens-prontas"),
    ],
)
def test_a_situacao_vem_do_consumer_count_e_do_message_count_da_declaracao_passiva(
    monkeypatch: pytest.MonkeyPatch, consumidores: int, prontas: int, em_dia: bool
) -> None:
    presenca = _presenca(monkeypatch, _Broker(_Canal(consumidores, prontas)))

    situacao = presenca.situacao_da_fila()

    assert situacao == SituacaoDaFila(consumidores=consumidores, prontas=prontas)
    assert situacao.em_dia is em_dia


def test_sem_conexao_ao_broker_nao_ha_situacao_e_nada_levanta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _Broker(None, conecta=False)
    presenca = _presenca(monkeypatch, broker)

    assert presenca.situacao_da_fila() is None
    assert broker.desconexoes == 0


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(AMQPConnectionError(), id="conexao-perdida"),
        pytest.param(ChannelClosedByBroker(404, "NOT_FOUND"), id="fila-inexistente"),
    ],
)
def test_erro_na_declaracao_desconecta_e_nao_ha_situacao(
    monkeypatch: pytest.MonkeyPatch, erro: Exception
) -> None:
    broker = _Broker(_Canal(erro))
    presenca = _presenca(monkeypatch, broker)

    assert presenca.situacao_da_fila() is None
    assert broker.desconexoes == 1


def test_manter_atende_o_broker_e_a_queda_desconecta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _Broker(_Canal(1))
    presenca = _presenca(monkeypatch, broker)
    presenca.manter()
    assert broker.atendeu == 0  # sem conexao, nada a atender

    assert presenca.situacao_da_fila() is not None
    presenca.manter()
    assert (broker.atendeu, broker.desconexoes) == (1, 0)

    broker.erro_ao_atender = AMQPConnectionError()
    presenca.manter()
    assert broker.desconexoes == 1
    presenca.fechar()
    assert broker.desconexoes == 2
