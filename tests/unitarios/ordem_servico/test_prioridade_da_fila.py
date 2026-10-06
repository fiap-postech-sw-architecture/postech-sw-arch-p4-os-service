"""A fila de OS classifica todo status: ativo com prioridade, ou encerrado."""

from __future__ import annotations

from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.repository import (
    _ESTADOS_ENCERRADOS,
    _PRIORIDADE_STATUS,
)


def test_todo_status_tem_prioridade_ou_e_encerrado() -> None:
    # Status novo sem prioridade cairia no fim da fila padrao sem aviso.
    assert set(_PRIORIDADE_STATUS) | _ESTADOS_ENCERRADOS == set(StatusOrdem)
    assert not set(_PRIORIDADE_STATUS) & _ESTADOS_ENCERRADOS


def test_prioridades_distintas() -> None:
    assert len(set(_PRIORIDADE_STATUS.values())) == len(_PRIORIDADE_STATUS)
