from __future__ import annotations

import pytest

from tests.manifestos import exigir_kubectl
from tests.rastreamento import Rastreador


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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marca tudo em tests/integracao com ``integracao`` (filtro ``-m``)."""
    for item in items:
        if "integracao" in item.path.parts:
            item.add_marker(pytest.mark.integracao)
