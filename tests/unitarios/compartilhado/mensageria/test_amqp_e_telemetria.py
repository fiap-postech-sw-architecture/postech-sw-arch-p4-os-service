"""Parametros e propriedades AMQP e a propagacao do contexto W3C."""

from __future__ import annotations

from unittest.mock import MagicMock

import pika
import pytest
from opentelemetry.trace import INVALID_SPAN_CONTEXT, get_current_span, use_span

from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
    contexto_dos_cabecalhos,
)
from tests.rastreamento import Rastreador, traceparent

_URL = "amqp://os:segredo@rabbitmq:5672/%2F"  # gitleaks:allow


def test_parametros_fixam_heartbeat_bloqueio_e_uma_tentativa_por_conexao() -> None:
    parametros = amqp.parametros(_URL, "consumidor")

    assert parametros.heartbeat == 30
    assert parametros.blocked_connection_timeout == 30
    assert parametros.socket_timeout == 5
    assert parametros.connection_attempts == 1
    assert amqp.usuario(parametros) == "os"


def test_propriedades_seguem_o_envelope_e_o_usuario_da_conexao() -> None:
    envelope = {"id": "m-1", "correlation_id": "c-1", "tipo": "GerarOrcamento"}

    propriedades = amqp.propriedades(
        envelope, usuario="os", cabecalhos={"traceparent": "00-x"}
    )

    assert propriedades.message_id == "m-1"
    assert propriedades.correlation_id == "c-1"
    assert propriedades.type == "GerarOrcamento"
    assert propriedades.user_id == "os"
    assert propriedades.content_type == "application/json"
    assert propriedades.delivery_mode == 2
    assert propriedades.headers == {"traceparent": "00-x"}


def test_conectar_fecha_a_conexao_se_o_canal_falhar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conexao = MagicMock()
    conexao.channel.side_effect = pika.exceptions.ChannelClosedByBroker(403, "x")
    monkeypatch.setattr(pika, "BlockingConnection", lambda _params: conexao)

    parametros = amqp.parametros(_URL, "relay")

    with pytest.raises(pika.exceptions.ChannelClosedByBroker):
        amqp.conectar(parametros)

    conexao.close.assert_called_once()


def test_fechar_ignora_conexao_ja_morta() -> None:
    conexao = MagicMock()
    conexao.close.side_effect = pika.exceptions.StreamLostError("caiu")

    amqp.fechar(conexao)  # nao propaga

    fechada = MagicMock(is_open=False)
    amqp.fechar(fechada)
    fechada.close.assert_not_called()


def test_sem_span_nao_ha_contexto_a_propagar() -> None:
    assert get_current_span().get_span_context() == INVALID_SPAN_CONTEXT
    assert cabecalhos_do_contexto_atual() == {}


def test_contexto_vai_e_volta_pelos_cabecalhos() -> None:
    rastreador = Rastreador()
    with rastreador.tracer.start_as_current_span("origem") as span:
        cabecalhos = cabecalhos_do_contexto_atual()

    contexto = contexto_dos_cabecalhos(cabecalhos)
    with use_span(get_current_span(contexto)):
        relido = get_current_span().get_span_context()

    (terminado,) = rastreador.spans()
    assert cabecalhos == {"traceparent": traceparent(terminado)}
    assert relido.trace_id == span.get_span_context().trace_id
    assert relido.span_id == span.get_span_context().span_id


@pytest.mark.parametrize(
    "cabecalhos",
    [None, {}, {"traceparent": b"00-binario"}, {"traceparent": "invalido"}],
)
def test_cabecalho_ausente_binario_ou_invalido_vira_contexto_vazio(
    cabecalhos: dict[str, object] | None,
) -> None:
    contexto = contexto_dos_cabecalhos(cabecalhos)

    assert get_current_span(contexto).get_span_context() == INVALID_SPAN_CONTEXT
