"""Composicao dos processos ``src.relay`` e ``src.consumidor`` e o despachante."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import structlog
from structlog.testing import capture_logs

import src.consumidor as processo_consumidor
import src.relay as processo_relay
from src.compartilhado.aplicacao.mensageria import Desfecho, MensagemRecebida
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo


def test_todo_evento_que_o_os_consome_tem_handler_no_despachante() -> None:
    assert set(processo_consumidor.DESPACHANTE) == catalogo().consumidos


def test_handler_provisorio_registra_o_recebimento_com_o_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processo_consumidor, "_log", structlog.get_logger())
    mensagem = MensagemRecebida(
        id=uuid4(),
        tipo="PagamentoConfirmado",
        versao=1,
        origem="billing-service",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        ocorrido_em=datetime.now(UTC),
        dados={"referencia_provedor": "texto que nao vai para o log"},
    )

    with capture_logs() as logs:
        desfecho = processo_consumidor.DESPACHANTE["PagamentoConfirmado"](
            mensagem, MagicMock()
        )

    assert desfecho is Desfecho.PROCESSADA
    assert logs == [
        {
            "event": "event received",
            "log_level": "info",
            "tipo": "PagamentoConfirmado",
            "message_id": str(mensagem.id),
            "correlation_id": str(mensagem.correlation_id),
            "causation_id": str(mensagem.causation_id),
        }
    ]


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
    assert consumidor.kwargs["despachante"] is processo_consumidor.DESPACHANTE
    assert consumidor.kwargs["tracer"] == "tracer-consumidor"
    boot_falso["engine"].dispose.assert_called_once()
