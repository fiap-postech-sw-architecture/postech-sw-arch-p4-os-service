"""O provider dos spans (ADR-043): amostrador, limpeza do texto de erro e exportacao.

O provider e o de producao (``criar_provedor``); o exportador dos testes e o
``InMemorySpanExporter`` do ``Rastreador``, e o do OTLP e trocado por um que guarda
o que recebeu.
"""

from __future__ import annotations

import atexit
import sys
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, StatusCode
from structlog.testing import capture_logs

import src.compartilhado.infraestrutura.observability as observability_modulo
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    contexto_dos_cabecalhos,
)
from src.compartilhado.infraestrutura.observability import (
    atributos_do_recurso,
    criar_provedor,
    criar_tracer,
)
from tests.conftest import MODULO_DO_OTLP
from tests.rastreamento import Rastreador, tudo_que_o_span_leva

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opentelemetry.sdk.trace import ReadableSpan

_PAI_AMOSTRADO = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
_PAI_NAO_AMOSTRADO = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"


@pytest.fixture(autouse=True)
def _logger_fresco(monkeypatch: pytest.MonkeyPatch) -> None:
    # capture_logs nao intercepta o logger de modulo ja usado por outro teste
    # (cache_logger_on_first_use): um novo por teste vale em qualquer ordem.
    monkeypatch.setattr(
        observability_modulo, "_log", structlog.get_logger("test_provedor_de_spans")
    )


class TestAmostrador:
    def test_cliente_sem_pai_nao_vira_trace(self, rastreador: Rastreador) -> None:
        # A consulta do poll, da raspagem ou do ciclo ocioso: o unico span do trace.
        with rastreador.tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT):
            pass

        assert rastreador.spans() == []

    def test_cliente_dentro_de_um_span_fica_no_trace_dele(
        self, rastreador: Rastreador
    ) -> None:
        with (
            rastreador.tracer.start_as_current_span("GET /x", kind=SpanKind.SERVER),
            rastreador.tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT),
        ):
            pass

        servidor, consulta = rastreador.spans("GET /x"), rastreador.spans("SELECT")
        assert len(servidor) == len(consulta) == 1
        assert consulta[0].parent is not None
        assert consulta[0].parent.span_id == servidor[0].get_span_context().span_id
        assert (
            consulta[0].get_span_context().trace_id
            == servidor[0].get_span_context().trace_id
        )

    @pytest.mark.parametrize(
        "tipo",
        [
            pytest.param(SpanKind.SERVER, id="servidor"),
            pytest.param(SpanKind.PRODUCER, id="publicacao"),
            pytest.param(SpanKind.CONSUMER, id="consumo"),
            pytest.param(SpanKind.INTERNAL, id="interno"),
        ],
    )
    def test_span_que_nao_e_de_cliente_abre_o_trace(
        self, rastreador: Rastreador, tipo: SpanKind
    ) -> None:
        with rastreador.tracer.start_as_current_span("x", kind=tipo):
            pass

        assert len(rastreador.spans()) == 1

    def test_pai_remoto_amostrado_continua_o_trace_do_chamador(
        self, rastreador: Rastreador
    ) -> None:
        pai = contexto_dos_cabecalhos({"traceparent": _PAI_AMOSTRADO})

        with (
            rastreador.tracer.start_as_current_span(
                "process X", context=pai, kind=SpanKind.CONSUMER
            ),
            rastreador.tracer.start_as_current_span("INSERT", kind=SpanKind.CLIENT),
        ):
            pass

        consumo, insercao = rastreador.spans("process X"), rastreador.spans("INSERT")
        assert len(consumo) == len(insercao) == 1
        assert consumo[0].get_span_context().trace_id == int(
            "4bf92f3577b34da6a3ce929d0e0e4736", 16
        )
        assert insercao[0].parent is not None
        assert insercao[0].parent.span_id == consumo[0].get_span_context().span_id

    def test_pai_remoto_nao_amostrado_derruba_o_span_e_os_comandos_de_banco(
        self, rastreador: Rastreador
    ) -> None:
        # A requisicao com traceparent de flag 00 nao e gravada, e os comandos de
        # banco dela, filhos de um pai local nao amostrado, tambem nao.
        pai = contexto_dos_cabecalhos({"traceparent": _PAI_NAO_AMOSTRADO})

        with (
            rastreador.tracer.start_as_current_span(
                "GET /x", context=pai, kind=SpanKind.SERVER
            ),
            rastreador.tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT),
        ):
            pass

        assert rastreador.spans() == []


class TestLimpezaDoTextoDeErro:
    def test_excecao_fica_so_com_o_tipo(self, rastreador: Rastreador) -> None:
        # O `with` do SDK grava o evento `exception` (mensagem e stack) e a
        # descricao do status com o texto da excecao.
        with (
            pytest.raises(ValueError, match="BRA2E19"),
            rastreador.tracer.start_as_current_span("x"),
        ):
            msg = "placa BRA2E19 e CPF 529.982.247-25 no texto da excecao"
            raise ValueError(msg)

        (span,) = rastreador.spans()
        (evento,) = span.events
        assert evento.name == "exception"
        assert evento.attributes is not None
        assert evento.attributes["exception.type"] == "ValueError"
        assert "exception.message" not in evento.attributes
        assert "exception.stacktrace" not in evento.attributes
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description is None
        texto = tudo_que_o_span_leva(span)
        assert "BRA2E19" not in texto
        assert "529.982.247-25" not in texto

    def test_descricao_do_status_de_erro_sai(self, rastreador: Rastreador) -> None:
        # O instrumentador do SQLAlchemy a preenche com o texto do driver.
        with rastreador.tracer.start_as_current_span("INSERT") as span:
            span.set_status(StatusCode.ERROR, "DETAIL: Key (placa)=(BRA2E19) exists")

        (gravado,) = rastreador.spans()
        assert gravado.status.status_code is StatusCode.ERROR
        assert gravado.status.description is None

    @pytest.mark.parametrize(
        "codigo", [StatusCode.UNSET, StatusCode.OK], ids=["sem-status", "ok"]
    )
    def test_status_sem_erro_fica_como_esta(
        self, rastreador: Rastreador, codigo: StatusCode
    ) -> None:
        with rastreador.tracer.start_as_current_span("x") as span:
            span.set_status(codigo)

        (gravado,) = rastreador.spans()
        assert gravado.status.status_code is codigo

    def test_evento_que_nao_e_excecao_passa_inteiro(
        self, rastreador: Rastreador
    ) -> None:
        with rastreador.tracer.start_as_current_span("x") as span:
            span.add_event("etapa", {"pytstop.saga.etapa": "aguardando_decisao"})

        (gravado,) = rastreador.spans()
        (evento,) = gravado.events
        assert evento.name == "etapa"
        assert evento.attributes == {"pytstop.saga.etapa": "aguardando_decisao"}

    def test_evento_de_excecao_mantem_os_outros_atributos(
        self, rastreador: Rastreador
    ) -> None:
        with rastreador.tracer.start_as_current_span("x") as span:
            span.record_exception(
                RuntimeError("segredo"), attributes={"pytstop.tentativa": 2}
            )

        (gravado,) = rastreador.spans()
        (evento,) = gravado.events
        assert evento.attributes is not None
        assert evento.attributes["pytstop.tentativa"] == 2
        assert evento.attributes["exception.type"] == "RuntimeError"
        assert "segredo" not in tudo_que_o_span_leva(gravado)

    def test_o_exportador_recebe_o_span_ja_limpo(
        self, monkeypatch: pytest.MonkeyPatch, otlp_falso: dict[str, Any]
    ) -> None:
        # O caminho de producao inteiro: o lote do OTLP so ve o span depois da
        # limpeza, em qualquer ordem em que se acrescentem processadores.
        monkeypatch.setenv("OTEL_ENABLED", "true")
        provedor = criar_provedor("api")

        with (
            pytest.raises(RuntimeError, match="segredo"),
            provedor.get_tracer("t").start_as_current_span("x"),
        ):
            msg = "segredo do cliente"
            raise RuntimeError(msg)
        provedor.force_flush()

        (texto,) = otlp_falso["texto"]
        assert "segredo" not in texto
        assert "RuntimeError" in texto

    def test_exportador_de_envio_imediato_recebe_o_span_limpo_na_hora(self) -> None:
        # Ele serializa o span dentro do `on_end` do processador que o envolve: se
        # a limpeza nao viesse antes dele, a foto sairia com o texto da excecao.
        fotos: list[str] = []

        class _TiraFoto(SpanExporter):
            def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
                fotos.extend(tudo_que_o_span_leva(s) for s in spans)
                return SpanExportResult.SUCCESS

        provedor = criar_provedor("api", exportador=_TiraFoto())

        with (
            pytest.raises(RuntimeError, match="segredo"),
            provedor.get_tracer("t").start_as_current_span("x"),
        ):
            msg = "segredo do cliente"
            raise RuntimeError(msg)

        (foto,) = fotos
        assert "segredo" not in foto
        assert "RuntimeError" in foto

    def test_shutdown_e_flush_chegam_ao_exportador(
        self, monkeypatch: pytest.MonkeyPatch, otlp_falso: dict[str, Any]
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        provedor = criar_provedor("api")

        assert provedor.force_flush() is True
        provedor.shutdown()

        assert otlp_falso["encerrado"] is True

    def test_sem_exportador_o_flush_e_o_shutdown_nao_fazem_nada(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OTEL_ENABLED", raising=False)
        provedor = criar_provedor("api")

        assert provedor.force_flush() is True
        provedor.shutdown()


class TestProvedor:
    def test_flag_desligada_nao_importa_o_exportador(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OTEL_ENABLED", raising=False)
        # Import proibido: se o caminho desligado o tentasse, haveria warning.
        monkeypatch.setitem(sys.modules, MODULO_DO_OTLP, None)

        with capture_logs() as logs:
            criar_provedor("relay")

        assert logs == []

    @pytest.mark.parametrize("valor", ["false", "0", "", "off"], ids=repr)
    def test_valores_desligados_nao_exportam(
        self, monkeypatch: pytest.MonkeyPatch, otlp_falso: dict[str, Any], valor: str
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", valor)

        criar_provedor("relay")

        assert "endpoint" not in otlp_falso

    @pytest.mark.parametrize("valor", ["true", "TRUE", "1", " true "], ids=repr)
    def test_valores_ligados_exportam(
        self, monkeypatch: pytest.MonkeyPatch, otlp_falso: dict[str, Any], valor: str
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", valor)

        criar_provedor("relay")

        assert "endpoint" in otlp_falso

    def test_flag_ligada_sem_o_extra_avisa_e_segue_sem_exportar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        monkeypatch.setitem(sys.modules, MODULO_DO_OTLP, None)

        with capture_logs() as logs:
            provedor = criar_provedor("relay")
        with provedor.get_tracer("t").start_as_current_span("publish X") as span:
            valido = span.get_span_context().is_valid

        avisos = [log for log in logs if log["log_level"] == "warning"]
        assert len(avisos) == 1
        # A mensagem diz como instalar o extra.
        assert "--extra otel" in avisos[0]["event"]
        assert valido

    def test_endpoint_padrao_http_usa_canal_inseguro(
        self, monkeypatch: pytest.MonkeyPatch, otlp_falso: dict[str, Any]
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        criar_provedor("api")

        assert (otlp_falso["endpoint"], otlp_falso["insecure"]) == (
            "http://jaeger:4317",
            True,
        )

    def test_endpoint_https_usa_tls(
        self, monkeypatch: pytest.MonkeyPatch, otlp_falso: dict[str, Any]
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector:4317")

        criar_provedor("api")

        assert (otlp_falso["endpoint"], otlp_falso["insecure"]) == (
            "https://collector:4317",
            False,
        )

    def test_recurso_leva_servico_versao_e_processo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        monkeypatch.setenv("PYTSTOP_GIT_SHA", "abcdef0123456789")
        rastreador = Rastreador("prazos")

        with rastreador.tracer.start_as_current_span("x"):
            pass

        (span,) = rastreador.spans()
        assert span.resource.attributes["service.name"] == "pytstop-os-service"
        assert span.resource.attributes["service.version"] == "abcdef012345"
        assert span.resource.attributes["pytstop.processo"] == "prazos"

    @pytest.mark.parametrize(
        ("valor", "esperado"),
        [
            pytest.param(None, "pytstop-os-service", id="ausente"),
            pytest.param("  ", "pytstop-os-service", id="em-branco"),
            pytest.param("os-service", "os-service", id="definido"),
        ],
    )
    def test_nome_do_servico_tem_padrao(
        self, monkeypatch: pytest.MonkeyPatch, valor: str | None, esperado: str
    ) -> None:
        if valor is None:
            monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        else:
            monkeypatch.setenv("OTEL_SERVICE_NAME", valor)

        assert atributos_do_recurso("api")["service.name"] == esperado


class TestTracerDosProcessos:
    """``criar_tracer``: o SDK sempre ligado; o OTLP so com OTEL_ENABLED."""

    @pytest.fixture
    def finalizadores(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        registrados: list[Any] = []
        monkeypatch.setattr(atexit, "register", registrados.append)
        return registrados

    def test_sem_flag_o_span_existe_mas_nao_sai_do_processo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        otlp_falso: dict[str, Any],
        finalizadores: list[Any],
    ) -> None:
        monkeypatch.delenv("OTEL_ENABLED", raising=False)

        tracer = criar_tracer("relay")
        with tracer.start_as_current_span("publish X") as span:
            valido = span.get_span_context().is_valid
        for finalizar in finalizadores:
            finalizar()

        assert valido
        assert otlp_falso["exportados"] == []

    def test_com_flag_exporta_com_o_recurso_do_processo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        otlp_falso: dict[str, Any],
        finalizadores: list[Any],
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "os-service")

        tracer = criar_tracer("consumidor")
        with tracer.start_as_current_span("process X"):
            pass
        # O encerramento do processo descarrega o ultimo lote.
        for finalizar in finalizadores:
            finalizar()

        (span,) = otlp_falso["exportados"]
        assert span.name == "process X"
        assert span.resource.attributes["service.name"] == "os-service"
        assert span.resource.attributes["pytstop.processo"] == "consumidor"
        assert otlp_falso["encerrado"] is True
