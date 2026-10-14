"""A instrumentacao do FastAPI de verdade, com o hook de URL e o provider de producao.

O app e um FastAPI pequeno com as rotas que importam (com id no caminho, com
corpo, sondas, ``/metrics`` e uma que estoura); nada de banco. O oraculo e o
``tudo_que_o_span_leva``: nome, atributos, eventos, links, descricao do status e
recurso, e nao so os atributos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation._semconv import (
    _OpenTelemetrySemanticConventionStability as EstabilidadeDoSemconv,
)
from opentelemetry.trace import SpanKind, StatusCode

from src.compartilhado.infraestrutura.observability import (
    _ATRIBUTOS_DE_URL,
    _ATRIBUTOS_DO_CHAMADOR,
    instrumentar_api,
    redigir_url_da_span,
)
from tests.rastreamento import Rastreador, tudo_que_o_span_leva

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.trace import ReadableSpan

ORDEM = str(uuid4())
TEMPLATE = "/api/v1/ordens-de-servico/{ordem_id}"
# Cada valor identifica quem chama ou o que ele pediu: nenhum pode chegar a um span.
CANARIOS = (
    ORDEM,
    "ABC1D23",
    "529.982.247-25",
    "CANARIO-UA",
    "canario.exemplo.org",
    "testclient",
)


def _app(rastreador: Rastreador) -> FastAPI:
    """As rotas ficam num ``APIRouter`` incluido, como no OS: a partir do FastAPI
    0.137 elas ficam aninhadas e a resolucao da instrumentacao 0.63b grava o
    caminho cru no nome do span e em ``http.route``."""
    app = FastAPI()
    router = APIRouter(prefix="/api/v1")

    @router.get("/ordens-de-servico/{ordem_id}")
    def obter(ordem_id: str) -> dict[str, str]:
        return {"id": ordem_id}

    @router.post("/clientes")
    def cadastrar(corpo: dict[str, Any]) -> dict[str, Any]:
        return corpo

    @router.get("/saude")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/saude/pronto")
    def readiness() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/estoura")
    def estourar() -> None:
        msg = "CPF 529.982.247-25 e placa ABC1D23 no texto da excecao"
        raise RuntimeError(msg)

    app.include_router(router)

    # `/metrics` e uma rota direta do app, como no OS.
    @app.get("/metrics")
    def metricas() -> dict[str, str]:
        return {}

    instrumentar_api(app, rastreador.provedor)
    return app


@pytest.fixture(params=["", "http", "http/dup"], ids=["antigo", "novo", "dup"])
def modo_semconv(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Os tres modos de ``OTEL_SEMCONV_STABILITY_OPT_IN``: a convencao de atributos.

    O instrumentador le a variavel uma vez por processo; o teste a le de novo.
    """
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", request.param)
    monkeypatch.setattr(EstabilidadeDoSemconv, "_initialized", False)
    monkeypatch.setattr(
        EstabilidadeDoSemconv, "_OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING", {}
    )
    return request.param  # type: ignore[no-any-return]  # param e str


def _servidor(rastreador: Rastreador) -> ReadableSpan:
    (servidor,) = [s for s in rastreador.spans() if s.kind is SpanKind.SERVER]
    return servidor


def _sem_canarios(span: ReadableSpan) -> None:
    texto = tudo_que_o_span_leva(span)
    achados = [c for c in CANARIOS if c in texto]
    assert not achados, f"o span leva {achados}"


class TestHookDeUrl:
    def test_span_leva_o_template_e_nada_de_quem_chama(
        self, rastreador: Rastreador, modo_semconv: str
    ) -> None:
        cliente = TestClient(_app(rastreador))

        resposta = cliente.get(
            f"/api/v1/ordens-de-servico/{ORDEM}?placa=ABC1D23&doc=529.982.247-25",
            headers={"User-Agent": "Mozilla/5.0 CANARIO-UA", "Host": CANARIOS[4]},
        )

        assert resposta.status_code == 200
        servidor = _servidor(rastreador)
        atributos = dict(servidor.attributes or {})
        assert servidor.name == f"GET {TEMPLATE}"
        assert {atributos[n] for n in _ATRIBUTOS_DE_URL} == {TEMPLATE}
        assert atributos["url.query"] == "REDACTED"
        do_chamador = [n for n in _ATRIBUTOS_DO_CHAMADOR if n in atributos]
        # Ha atributo de user-agent e de endereco do par em qualquer convencao.
        assert any("user_agent" in n for n in do_chamador)
        assert any(n.endswith(("ip", "address")) for n in do_chamador)
        assert {atributos[n] for n in do_chamador} == {"REDACTED"}
        _sem_canarios(servidor)

    def test_sem_query_nao_marca_a_query(self, rastreador: Rastreador) -> None:
        TestClient(_app(rastreador)).get(f"/api/v1/ordens-de-servico/{ORDEM}")

        assert "url.query" not in dict(_servidor(rastreador).attributes or {})

    def test_requisicao_sem_rota_leva_nao_roteada(self, rastreador: Rastreador) -> None:
        resposta = TestClient(_app(rastreador)).get("/api/v1/nao-existe/tok-3f9a1c")

        assert resposta.status_code == 404
        servidor = _servidor(rastreador)
        assert servidor.name == "GET"
        atributos = dict(servidor.attributes or {})
        assert {atributos[n] for n in _ATRIBUTOS_DE_URL} == {"nao_roteada"}
        assert "tok-3f9a1c" not in tudo_que_o_span_leva(servidor)

    def test_metodo_nao_permitido_mantem_o_template(
        self, rastreador: Rastreador
    ) -> None:
        resposta = TestClient(_app(rastreador)).delete(
            f"/api/v1/ordens-de-servico/{ORDEM}"
        )

        assert resposta.status_code == 405
        servidor = _servidor(rastreador)
        assert servidor.name == f"DELETE {TEMPLATE}"
        assert ORDEM not in tudo_que_o_span_leva(servidor)

    @pytest.mark.parametrize(
        "host",
        [
            "[",
            "a]b",
            "[::1",
            "x/metrics",
            "x/api/v1/saude",
            "usuario:senha@maldoso.exemplo",
        ],
        ids=repr,
    )
    def test_host_torto_nao_derruba_o_hook_nem_tira_a_requisicao_do_trace(
        self, rastreador: Rastreador, modo_semconv: str, host: str
    ) -> None:
        # O hook nao monta nem interpreta a URL: um Host que o urlsplit recusaria
        # nao pode deixar o caminho cru no span, e um Host que lembra uma rota
        # excluida nao pode apagar a requisicao do trace.
        resposta = TestClient(_app(rastreador)).get(
            f"/api/v1/ordens-de-servico/{ORDEM}?x=1", headers={"Host": host}
        )

        assert resposta.status_code == 200
        servidor = _servidor(rastreador)
        atributos = dict(servidor.attributes or {})
        assert {atributos[n] for n in _ATRIBUTOS_DE_URL} == {TEMPLATE}
        assert ORDEM not in tudo_que_o_span_leva(servidor)
        # Valor a valor: um Host como `[` estaria em qualquer JSON.
        assert not [v for v in atributos.values() if host in str(v)]
        assert host not in servidor.name

    def test_span_que_nao_grava_e_ignorado(self) -> None:
        class SpanQueNaoGrava:
            def is_recording(self) -> bool:
                return False

            def set_attribute(self, chave: str, valor: object) -> None:
                pytest.fail(f"gravou {chave}={valor}")

        redigir_url_da_span(SpanQueNaoGrava(), {"query_string": b"a=1"})  # type: ignore[arg-type]  # dublê sem os outros metodos do Span


class TestSpansDaApi:
    @pytest.mark.parametrize(
        "caminho",
        ["/metrics", "/api/v1/saude", "/api/v1/saude/pronto"],
    )
    def test_raspagem_e_sondas_nao_geram_span(
        self, rastreador: Rastreador, caminho: str
    ) -> None:
        assert TestClient(_app(rastreador)).get(caminho).status_code == 200

        assert rastreador.spans() == []

    @pytest.mark.parametrize(
        "caminho", ["/os/metrics", "/os/api/v1/saude", "/os/api/v1/saude/pronto"]
    )
    def test_prefixo_do_root_path_tambem_fica_fora(
        self, rastreador: Rastreador, caminho: str
    ) -> None:
        # O uvicorn poe o --root-path (/os atras do Kong) no caminho do scope.
        cliente = TestClient(_app(rastreador), root_path="/os")

        assert cliente.get(caminho).status_code == 200

        assert rastreador.spans() == []

    @pytest.mark.parametrize(
        "caminho", ["/metrics/", "/api/v1/saude/x", "/api/v1/saude-x"]
    )
    def test_rota_parecida_com_a_excluida_segue_com_trace(
        self, rastreador: Rastreador, caminho: str
    ) -> None:
        TestClient(_app(rastreador)).get(caminho)

        assert len(rastreador.spans()) == 1

    def test_corpo_e_resposta_nao_geram_span_interno(
        self, rastreador: Rastreador
    ) -> None:
        # Sem `exclude_spans`, cada POST com corpo ganha um span `http receive` e
        # cada resposta, um `http send`: so o SERVER fica.
        resposta = TestClient(_app(rastreador)).post(
            "/api/v1/clientes", json={"nome": "Maria"}
        )

        assert resposta.status_code == 200
        (span,) = rastreador.spans()
        assert span.kind is SpanKind.SERVER

    def test_erro_nao_tratado_marca_o_span_sem_o_texto_da_excecao(
        self, rastreador: Rastreador
    ) -> None:
        cliente = TestClient(_app(rastreador), raise_server_exceptions=False)

        resposta = cliente.get("/api/v1/estoura")

        assert resposta.status_code == 500
        servidor = _servidor(rastreador)
        assert servidor.status.status_code is StatusCode.ERROR
        assert servidor.status.description is None
        assert [e.name for e in servidor.events] == ["exception"]
        texto = tudo_que_o_span_leva(servidor)
        assert "RuntimeError" in texto
        assert "529.982.247-25" not in texto
        assert "ABC1D23" not in texto
