"""Parametros e propriedades AMQP e a propagacao do contexto W3C."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pika
import pytest
from opentelemetry.trace import (
    INVALID_SPAN_CONTEXT,
    SpanKind,
    StatusCode,
    get_current_span,
    use_span,
)

from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    cabecalhos_do_contexto_atual,
    contexto_dos_cabecalhos,
    span_de_mensagem,
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
    [
        pytest.param(None, id="sem-headers"),
        pytest.param({}, id="headers-vazios"),
        pytest.param({"traceparent": b"00-binario"}, id="binario"),
        pytest.param({"traceparent": "invalido"}, id="invalido"),
        pytest.param(
            {"traceparent": "00-" + "a" * 200 + "-00f067aa0ba902b7-01"},
            id="traceparent-acima-do-teto",
        ),
    ],
)
def test_cabecalho_ausente_binario_ou_invalido_vira_contexto_vazio(
    cabecalhos: dict[str, object] | None,
) -> None:
    contexto = contexto_dos_cabecalhos(cabecalhos)

    assert get_current_span(contexto).get_span_context() == INVALID_SPAN_CONTEXT


def test_tracestate_acima_de_512_e_ignorado_sem_ir_para_o_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # O SDK loga o membro malformado inteiro; acima do teto ele nem e lido.
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    hostil = "x@y=" + "joao@exemplo.com.br" * 40

    with caplog.at_level(logging.DEBUG):
        contexto = contexto_dos_cabecalhos(
            {"traceparent": traceparent, "tracestate": hostil}
        )

    span = get_current_span(contexto).get_span_context()
    assert span.trace_id == 0x4BF92F3577B34DA6A3CE929D0E0E4736
    assert len(span.trace_state) == 0
    assert "joao@exemplo.com.br" not in caplog.text


def test_tracestate_de_ate_512_segue_no_contexto() -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    # Dois membros: o W3C limita o valor de cada um a 256 caracteres.
    tracestate = "a1=" + "x" * 253 + ",a2=" + "x" * 252

    contexto = contexto_dos_cabecalhos(
        {"traceparent": traceparent, "tracestate": tracestate}
    )

    assert len(tracestate) == 512
    assert get_current_span(contexto).get_span_context().trace_state.to_header() == (
        tracestate
    )


def test_erro_que_escapa_do_span_marca_so_o_tipo() -> None:
    rastreador = Rastreador()

    with (
        pytest.raises(RuntimeError),
        span_de_mensagem(
            rastreador.tracer,
            "process DiagnosticoConcluido",
            contexto=contexto_dos_cabecalhos(None),
            tipo=SpanKind.CONSUMER,
            atributos={"messaging.system": "rabbitmq"},
        ),
    ):
        raise RuntimeError("placa BRA2E19 no texto da excecao")

    (span,) = rastreador.spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "RuntimeError"
    assert span.events == ()
