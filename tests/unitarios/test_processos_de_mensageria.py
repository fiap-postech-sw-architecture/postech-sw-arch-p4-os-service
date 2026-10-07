"""Composicao dos processos ``src.relay`` e ``src.consumidor`` e o despachante."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import src.consumidor as processo_consumidor
import src.relay as processo_relay
from src.compartilhado.aplicacao.mensageria import Desfecho, MensagemRecebida
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem
from src.ordem_servico.aplicacao.saga.orquestrador import (
    EventoAdiantadoError,
    Tratamento,
)
from src.ordem_servico.aplicacao.saga.saga import EtapaSaga

if TYPE_CHECKING:
    from tests.rastreamento import Rastreador


def test_os_23_eventos_que_o_os_consome_vao_para_o_orquestrador() -> None:
    despachante = processo_consumidor.montar_despachante(timedelta(seconds=7))

    assert set(despachante) == catalogo().consumidos
    assert len(despachante) == 23
    (handler,) = set(despachante.values())
    assert isinstance(handler, partial)
    assert handler.func is processo_consumidor.tratar_evento_da_saga
    assert handler.keywords == {"prazo_resposta": timedelta(seconds=7)}


def _mensagem() -> MensagemRecebida:
    return MensagemRecebida(
        id=uuid4(),
        tipo="DiagnosticoIniciado",
        versao=1,
        origem="execution-service",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        ocorrido_em=datetime.now(UTC),
        dados={},
    )


class _OrquestradorFalso:
    """Devolve (ou levanta) o resultado programado e guarda como foi montado."""

    def __init__(self, resultado: Tratamento | Exception) -> None:
        self._resultado = resultado
        self.montado_com: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> _OrquestradorFalso:
        self.montado_com = kwargs
        return self

    def tratar(self, _mensagem: MensagemRecebida) -> Tratamento:
        if isinstance(self._resultado, Exception):
            raise self._resultado
        return self._resultado


def test_handler_monta_o_orquestrador_na_transacao_e_marca_o_span(
    monkeypatch: pytest.MonkeyPatch, rastreador: Rastreador
) -> None:
    falso = _OrquestradorFalso(
        Tratamento(
            Desfecho.PROCESSADA,
            EtapaSaga.AGUARDANDO_DIAGNOSTICO,
            EtapaSaga.AGUARDANDO_ORCAMENTO,
        )
    )
    monkeypatch.setattr(processo_consumidor, "OrquestradorDaSaga", falso)
    transacao = TransacaoDaMensagem(MagicMock())

    with rastreador.tracer.start_as_current_span("process DiagnosticoConcluido"):
        desfecho = processo_consumidor.tratar_evento_da_saga(
            _mensagem(), transacao, prazo_resposta=timedelta(seconds=9)
        )

    assert desfecho is Desfecho.PROCESSADA
    assert falso.montado_com["publicador"] is transacao
    assert falso.montado_com["prazo_resposta"] == timedelta(seconds=9)
    (span,) = rastreador.spans()
    assert span.attributes == {
        "pytstop.saga.etapa": "aguardando_diagnostico",
        "pytstop.saga.etapa_nova": "aguardando_orcamento",
        "pytstop.saga.desfecho": "processada",
    }


def test_handler_marca_o_adiantado_no_span_e_relanca(
    monkeypatch: pytest.MonkeyPatch, rastreador: Rastreador
) -> None:
    falso = _OrquestradorFalso(EventoAdiantadoError(EtapaSaga.AGUARDANDO_AGENDAMENTO))
    monkeypatch.setattr(processo_consumidor, "OrquestradorDaSaga", falso)

    with (
        rastreador.tracer.start_as_current_span("process ExecucaoIniciada"),
        pytest.raises(EventoAdiantadoError),
    ):
        processo_consumidor.tratar_evento_da_saga(
            _mensagem(),
            TransacaoDaMensagem(MagicMock()),
            prazo_resposta=timedelta(seconds=9),
        )

    (span,) = rastreador.spans()
    assert span.attributes == {
        "pytstop.saga.etapa": "aguardando_agendamento",
        "pytstop.saga.desfecho": "adiantada",
    }


class _Processo:
    """Relay ou consumidor falso: guarda como foi montado e se rodou."""

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
    for modulo in (processo_relay, processo_consumidor):
        monkeypatch.setattr(modulo, "instalar_sinais", lambda parar: None)
        monkeypatch.setattr(
            modulo,
            "preparar",
            lambda nome: (
                registro.setdefault("processos", []).append(nome)
                or (registro["engine"], registro["parametros"])
            ),
        )
        monkeypatch.setattr(modulo, "criar_tracer", lambda nome: f"tracer-{nome}")
        monkeypatch.setattr(modulo, "subir_metricas", lambda: 9100)
    monkeypatch.setattr(processo_relay, "Relay", _Processo)
    monkeypatch.setattr(processo_consumidor, "Consumidor", _Processo)
    return registro


def test_main_do_relay_monta_executa_e_fecha_o_banco(
    boot_falso: dict[str, Any],
) -> None:
    processo_relay.main()

    (relay,) = _Processo.criados
    assert boot_falso["processos"] == ["relay"]
    assert relay.executou
    assert relay.kwargs["engine"] is boot_falso["engine"]
    assert relay.kwargs["parametros"] is boot_falso["parametros"]
    assert relay.kwargs["tracer"] == "tracer-relay"
    boot_falso["engine"].dispose.assert_called_once()


def test_main_do_consumidor_monta_executa_e_fecha_o_banco(
    boot_falso: dict[str, Any],
) -> None:
    processo_consumidor.main()

    (consumidor,) = _Processo.criados
    assert boot_falso["processos"] == ["consumidor"]
    assert consumidor.executou
    (handler,) = set(consumidor.kwargs["despachante"].values())
    assert handler.keywords == {"prazo_resposta": timedelta(seconds=120)}
    assert consumidor.kwargs["tracer"] == "tracer-consumidor"
    boot_falso["engine"].dispose.assert_called_once()


def test_main_do_consumidor_le_o_prazo_tecnico_do_ambiente(
    boot_falso: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", "2")

    processo_consumidor.main()

    (consumidor,) = _Processo.criados
    (handler,) = set(consumidor.kwargs["despachante"].values())
    assert handler.keywords == {"prazo_resposta": timedelta(seconds=2)}


@pytest.mark.parametrize("valor", ["0", "-1", "dois"])
def test_prazo_tecnico_invalido_aborta_o_boot_e_fecha_o_banco(
    boot_falso: dict[str, Any], monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("SAGA_PRAZO_RESPOSTA_SEGUNDOS", valor)

    with pytest.raises(RuntimeError, match="SAGA_PRAZO_RESPOSTA_SEGUNDOS"):
        processo_consumidor.main()

    assert _Processo.criados == []
    boot_falso["engine"].dispose.assert_called_once()
