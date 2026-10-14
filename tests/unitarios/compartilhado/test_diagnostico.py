"""``onde``: o ``arquivo:linha`` da excecao para o log, sem a mensagem dela."""

from __future__ import annotations

import re
from types import SimpleNamespace

from src.compartilhado.aplicacao.diagnostico import onde
from src.ordem_servico.aplicacao.saga.prazos import comando_a_reenviar


def _falha() -> None:
    msg = "dado do cliente na mensagem"
    raise ValueError(msg)


def test_fora_do_servico_vale_o_quadro_mais_interno_sem_a_mensagem() -> None:
    try:
        _falha()
    except ValueError as exc:
        local = onde(exc)

    assert re.fullmatch(r"test_diagnostico\.py:\d+", local)
    assert "cliente" not in local


def test_vale_o_ultimo_quadro_do_servico_e_nao_o_da_biblioteca() -> None:
    # O `Comando(...)` levanta dentro do `enum` (stdlib), chamado de `prazos.py`.
    saga = SimpleNamespace(comando_em_voo={"tipo": "placa-ABC1D23", "dados": {}})
    try:
        comando_a_reenviar(saga)
    except ValueError as exc:
        local = onde(exc)

    assert re.fullmatch(r"prazos\.py:\d+", local)
    assert "ABC1D23" not in local


def test_excecao_que_nunca_foi_levantada_nao_tem_origem() -> None:
    assert onde(ValueError("sem traceback")) == "desconhecido"
