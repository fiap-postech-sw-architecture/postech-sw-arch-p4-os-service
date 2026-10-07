"""Registro do coletor da saga e fatos que nao viram metrica (sem banco)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from src.compartilhado.dominio.events import DomainEvent
from src.ordem_servico.infraestrutura import metricas_da_saga


def test_coletor_e_registrado_uma_vez_por_processo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registro = CollectorRegistry()
    monkeypatch.setattr(metricas_da_saga, "REGISTRY", registro)
    monkeypatch.setattr(metricas_da_saga, "_coletor", None)

    metricas_da_saga.registrar_coletor(lambda: pytest.fail("sem raspagem aqui"))
    metricas_da_saga.registrar_coletor(lambda: pytest.fail("sem raspagem aqui"))

    # describe, e nao collect, no registro: nenhuma consulta antes do boot.
    assert sorted(registro._names_to_collectors) == [
        "pytstop_saga_ativas",
        "pytstop_saga_etapa_mais_antiga_segundos",
    ]


def _amostras() -> list[object]:
    return [
        metrica.samples
        for instrumento in (
            metricas_da_saga.SAGAS_INICIADAS,
            metricas_da_saga.SAGAS_FINALIZADAS,
            metricas_da_saga.DURACAO_DA_ETAPA,
        )
        for metrica in instrumento.collect()
    ]


def test_fato_que_nao_e_da_saga_nao_conta() -> None:
    antes = _amostras()

    metricas_da_saga._observar(DomainEvent(agregado_id=uuid4()))

    assert _amostras() == antes
