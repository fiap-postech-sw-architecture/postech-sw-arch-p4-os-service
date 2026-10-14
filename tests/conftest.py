from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult
from prometheus_client import REGISTRY

from tests.manifestos import exigir_kubectl
from tests.rastreamento import Rastreador, tudo_que_o_span_leva

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opentelemetry.sdk.trace import ReadableSpan

MODULO_DO_OTLP = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"


def valor_da_metrica(nome: str, **rotulos: str) -> float:
    """O valor da serie no ``REGISTRY``; 0.0 se ela ainda nao existe.

    O registro e global ao processo: os testes comparam o valor antes e depois do
    caso que a dispara, nunca o valor absoluto.
    """
    return REGISTRY.get_sample_value(nome, rotulos) or 0.0


def existe_a_serie(nome: str, **rotulos: str) -> bool:
    """A serie existe no ``REGISTRY`` (um contador pre-inicializado vale 0.0)."""
    return REGISTRY.get_sample_value(nome, rotulos) is not None


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    from src.compartilhado.interfaces.middleware import limiter

    limiter.reset()


@pytest.fixture(autouse=True)
def _reset_encryption_singleton() -> None:
    # Sem o reset, o primeiro teste que tocar o servico congela a
    # ENCRYPTION_KEY vigente para todos os seguintes (dependencia de ordem).
    from src.compartilhado.infraestrutura.encryption import EncryptionService

    EncryptionService._instance = None


@pytest.fixture(scope="session")
def kubectl() -> None:
    """Os testes que leem o render do kustomize pedem esta fixture."""
    exigir_kubectl()


@pytest.fixture
def rastreador() -> Rastreador:
    """Spans do teste em memoria (InMemorySpanExporter)."""
    return Rastreador()


@pytest.fixture
def otlp_falso(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Troca o exportador OTLP/gRPC por um que guarda o endpoint e cada span."""
    registro: dict[str, Any] = {"exportados": [], "encerrado": False}

    class ExportadorFalso:
        def __init__(self, endpoint: str, insecure: bool) -> None:
            registro["endpoint"] = endpoint
            registro["insecure"] = insecure

        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            registro["exportados"].extend(spans)
            registro.setdefault("texto", []).extend(
                tudo_que_o_span_leva(span) for span in spans
            )
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            registro["encerrado"] = True

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    modulo = types.ModuleType("trace_exporter")
    modulo.OTLPSpanExporter = ExportadorFalso  # type: ignore[attr-defined]  # modulo falso
    monkeypatch.setitem(sys.modules, MODULO_DO_OTLP, modulo)
    return registro


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marca tudo em tests/integracao com ``integracao`` (filtro ``-m``)."""
    for item in items:
        if "integracao" in item.path.parts:
            item.add_marker(pytest.mark.integracao)
