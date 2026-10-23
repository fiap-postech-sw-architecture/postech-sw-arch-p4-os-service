"""A guarda de ``tests/respostas_da_api.py``: o que ela espia, ignora e reprova.

A fixture ``respostas_vistas`` e autouse: ela ja espia cada teste deste arquivo, e os
testes leem dela o que o espiao guardou.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.cliente_veiculo.interfaces.router import router as router_clientes
from src.compartilhado.interfaces.dependencies import obter_session
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from src.compartilhado.interfaces.router_publico import router as router_publico
from src.main import app
from tests.respostas_da_api import Resposta, conferir, nao_declaradas

_SAUDE = "/api/v1/saude"
_CLIENTES = "/api/v1/clientes"
_CLIENTE = _CLIENTES + "/{cliente_id}"
_INEXISTENTE = "6f1b8e4a-5c1d-4c53-9a43-1b2f6c7d8e90"


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    documento: dict[str, Any] = copy.deepcopy(app.openapi())
    return documento


def _app_com_a_rota_de_clientes() -> FastAPI:
    """A rota real de clientes, sem token: o 401 sai do gate, antes da rota."""
    mini = FastAPI()
    mini.include_router(router_clientes)
    mini.dependency_overrides[obter_session] = lambda: MagicMock()
    registrar_error_handlers(mini)
    return mini


def test_guarda_a_resposta_de_rota_do_servico_com_o_template(
    respostas_vistas: list[Resposta],
) -> None:
    mini = FastAPI()
    mini.include_router(router_publico)

    assert TestClient(mini).get(_SAUDE).status_code == 200

    assert respostas_vistas == [Resposta("GET", _SAUDE, 200)]


def test_guarda_o_erro_que_sai_do_gate_com_a_rota_que_o_atenderia(
    respostas_vistas: list[Resposta], openapi: dict[str, Any]
) -> None:
    resposta = TestClient(_app_com_a_rota_de_clientes()).get(_CLIENTES)

    assert resposta.status_code == 401
    assert respostas_vistas == [Resposta("GET", _CLIENTES, 401)]
    assert nao_declaradas(openapi, respostas_vistas) == []


def test_guarda_o_metodo_da_requisicao_e_nao_so_o_get(
    respostas_vistas: list[Resposta], openapi: dict[str, Any]
) -> None:
    cliente = TestClient(_app_com_a_rota_de_clientes())

    resposta = cliente.delete(_CLIENTE.format(cliente_id=_INEXISTENTE))

    assert resposta.status_code == 401
    assert respostas_vistas == [Resposta("DELETE", _CLIENTE, 401)]
    assert nao_declaradas(openapi, respostas_vistas) == []


def test_ignora_a_rota_que_o_teste_inventa_com_o_caminho_de_uma_real(
    respostas_vistas: list[Resposta],
) -> None:
    mini = FastAPI()

    @mini.get(_SAUDE)
    def saude_do_teste() -> None:  # o endpoint e deste modulo, nao de src
        return None

    assert TestClient(mini).get(_SAUDE).status_code == 200

    assert respostas_vistas == []


def test_ignora_rota_inexistente_e_pedido_que_nao_e_http(
    respostas_vistas: list[Resposta],
) -> None:
    mini = FastAPI()
    mini.include_router(router_publico)

    with TestClient(mini) as cliente:  # o lifespan passa pelo mesmo espiao
        assert cliente.get("/nao-existe").status_code == 404

    assert respostas_vistas == []


def test_status_que_a_operacao_nao_declara_e_apontado(
    openapi: dict[str, Any],
) -> None:
    sem_401 = copy.deepcopy(openapi)
    del sem_401["paths"][_CLIENTES]["get"]["responses"]["401"]
    vistas = [Resposta("GET", _CLIENTES, 401), Resposta("GET", _CLIENTES, 401)]

    assert nao_declaradas(openapi, vistas) == []
    assert nao_declaradas(sem_401, vistas) == [Resposta("GET", _CLIENTES, 401)]


def test_aceita_o_default_do_openapi(openapi: dict[str, Any]) -> None:
    com_default = copy.deepcopy(openapi)
    com_default["paths"][_SAUDE]["get"]["responses"]["default"] = {"description": "x"}

    assert nao_declaradas(com_default, [Resposta("GET", _SAUDE, 418)]) == []


@pytest.mark.parametrize(
    "resposta",
    [
        pytest.param(Resposta("GET", _SAUDE, 500), id="500-vale-para-toda-rota"),
        pytest.param(Resposta("DELETE", _SAUDE, 405), id="metodo-que-a-rota-nao-tem"),
        pytest.param(Resposta("GET", "/api/v1/outra", 404), id="rota-fora-do-openapi"),
    ],
)
def test_nao_cobra_o_que_nao_e_contrato_de_rota(
    openapi: dict[str, Any], resposta: Resposta
) -> None:
    assert nao_declaradas(openapi, [resposta]) == []


def test_conferir_reprova_com_o_metodo_a_rota_e_o_status() -> None:
    vistas = [Resposta("GET", _SAUDE, 200), Resposta("GET", _SAUDE, 404)]

    with pytest.raises(AssertionError, match=r"GET /api/v1/saude -> 404"):
        conferir(vistas)
