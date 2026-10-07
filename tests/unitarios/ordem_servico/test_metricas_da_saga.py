"""Registro do coletor da saga e fatos que nao viram metrica (sem banco)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from prometheus_client import REGISTRY, CollectorRegistry
from sqlalchemy.orm import Session

from src.compartilhado.dominio.events import DomainEvent
from src.ordem_servico.aplicacao.saga.modelo import (
    CompensacaoIniciadaEvent,
    MotivoCompensacao,
)
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
        "pytstop_saga_coletor_disponivel",
        "pytstop_saga_etapa_mais_antiga_segundos",
    ]


def test_fato_sem_metrica_levanta_antes_do_commit() -> None:
    # Um fato novo da saga sem a observacao dele falharia em silencio no
    # after_commit; aqui levanta antes, com a transacao ainda desfazivel.
    sessao = Session()

    with pytest.raises(TypeError, match="DomainEvent"):
        metricas_da_saga.anotar(sessao, [DomainEvent(agregado_id=uuid4())])

    assert sessao.info == {}


def _compensacoes(motivo: str) -> float | None:
    return REGISTRY.get_sample_value(
        "pytstop_saga_compensacoes_total", {"motivo": motivo}
    )


def test_compensacoes_tem_a_serie_de_cada_motivo_e_contam_no_commit() -> None:
    # Series de rotulo fechado nascem com o processo: o increase() do painel
    # nao perde a primeira compensacao. O fato so vira metrica no commit.
    assert all(_compensacoes(m.value) is not None for m in MotivoCompensacao)
    antes = _compensacoes("reserva_falhou")
    sessao = Session()
    fato = CompensacaoIniciadaEvent(
        agregado_id=uuid4(), motivo=MotivoCompensacao.RESERVA_FALHOU
    )

    metricas_da_saga.anotar(sessao, [fato])
    assert _compensacoes("reserva_falhou") == antes
    sessao.commit()

    assert _compensacoes("reserva_falhou") == (antes or 0) + 1
