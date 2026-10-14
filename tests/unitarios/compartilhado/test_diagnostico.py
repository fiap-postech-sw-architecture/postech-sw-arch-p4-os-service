"""``onde``: o ``arquivo:linha`` da excecao para o log, sem a mensagem dela."""

from __future__ import annotations

import re

from src.compartilhado.aplicacao.diagnostico import onde


def _falha() -> None:
    msg = "dado do cliente na mensagem"
    raise ValueError(msg)


def test_onde_a_excecao_nasceu_sem_a_mensagem() -> None:
    try:
        _falha()
    except ValueError as exc:
        local = onde(exc)

    assert re.fullmatch(r"test_diagnostico\.py:\d+", local)
    assert "cliente" not in local


def test_excecao_que_nunca_foi_levantada_nao_tem_origem() -> None:
    assert onde(ValueError("sem traceback")) == "desconhecido"
