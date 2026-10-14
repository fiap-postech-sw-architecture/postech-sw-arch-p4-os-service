"""``onde``: o ``arquivo:linha`` da excecao para o log, sem a mensagem dela."""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

from src.compartilhado.aplicacao.diagnostico import onde
from src.ordem_servico.aplicacao.saga.prazos import (
    comando_a_reenviar,
    transicao_do_reenvio,
)


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
    # `transicao_do_reenvio` chama `comando_a_reenviar`, que levanta no `Comando(...)`
    # dentro do `enum` (stdlib): o quadro certo e o do `Comando(...)` em `prazos.py`,
    # o mais interno do servico, e nao o do `enum` nem o de quem o chamou.
    saga = SimpleNamespace(comando_em_voo={"tipo": "placa-ABC1D23", "dados": {}})
    linhas, inicio = inspect.getsourcelines(comando_a_reenviar)
    esperada = inicio + next(
        i for i, linha in enumerate(linhas) if 'Comando(em_voo["tipo"])' in linha
    )
    try:
        transicao_do_reenvio(saga, 5)
    except ValueError as exc:
        local = onde(exc)

    assert local == f"prazos.py:{esperada}"
    assert "ABC1D23" not in local


def test_excecao_que_nunca_foi_levantada_nao_tem_origem() -> None:
    assert onde(ValueError("sem traceback")) == "desconhecido"
