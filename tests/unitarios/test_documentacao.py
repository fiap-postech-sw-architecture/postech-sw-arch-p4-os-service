"""Documentos do servico: README, docs/arquitetura.md e docs/api.

- Os comandos dos blocos ``bash`` do README rodam colados no terminal.
- ``docs/api`` (OpenAPI e colecao Postman) acompanha o app (``make docs-api``).
- O README aponta ``docs/arquitetura.md`` e ``docs/api``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

from src.main import app

if TYPE_CHECKING:
    from types import ModuleType

RAIZ = Path(__file__).resolve().parents[2]
README = RAIZ / "README.md"
ARQUITETURA = RAIZ / "docs" / "arquitetura.md"
OPENAPI = RAIZ / "docs" / "api" / "openapi.json"
COLECAO = RAIZ / "docs" / "api" / "os-service.postman_collection.json"
# A cerca pode vir indentada: os passos numerados do README levam o bloco recuado.
_BLOCO = re.compile(
    r"^[ \t]*```(?:bash|sh)\n(.*?)^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE
)
_COMENTARIO = re.compile(r"(^|\s)#(\s|$)")


def _linhas_com_comentario(texto: str) -> list[str]:
    # No zsh interativo (o padrao do macOS) o # nao e comentario: o texto depois
    # dele vira argumento do comando, e no make vira alvo. O que o comando faz
    # fica na prosa acima do bloco.
    return [
        linha
        for bloco in _BLOCO.findall(texto)
        for linha in bloco.splitlines()
        if _COMENTARIO.search(linha)
    ]


def test_blocos_de_comando_nao_levam_comentario() -> None:
    texto = README.read_text()

    assert len(_BLOCO.findall(texto)) > 10
    assert _linhas_com_comentario(texto) == []


def test_bloco_indentado_de_passo_numerado_tambem_e_conferido() -> None:
    texto = (
        "1. Passo:\n\n"
        "   ```bash\n"
        "   kubectl get pods  # espera\n"
        "   ```\n\n"
        "```sh\n"
        "make test\n"
        "```\n"
    )

    assert _linhas_com_comentario(texto) == ["   kubectl get pods  # espera"]


def _modulo(nome: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(nome, RAIZ / "scripts" / f"{nome}.py")
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    # O dataclass do modulo procura o proprio modulo em sys.modules.
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    # Ida e volta pelo JSON: e assim que o documento chega ao arquivo.
    documento: dict[str, Any] = json.loads(json.dumps(app.openapi()))
    return documento


@pytest.fixture(scope="module")
def gerador() -> ModuleType:
    return _modulo("gerar_docs_api")


def _pares(openapi: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        (metodo, rota) for rota, ops in openapi["paths"].items() for metodo in ops
    )


def _fora_do_schema(
    openapi: dict[str, Any], declaracao: dict[str, Any], valor: object, onde: str
) -> list[str]:
    """Erros de ``valor`` contra o schema JSON de um requestBody ou de uma resposta."""
    schema = declaracao.get("content", {}).get("application/json", {}).get("schema")
    if schema is None:
        return [f"{onde}: a rota nao declara corpo JSON"]
    validador = Draft202012Validator({**schema, "components": openapi["components"]})
    return [f"{onde}: {erro.message}" for erro in validador.iter_errors(valor)]


# docs/api: o OpenAPI e a colecao Postman.


def test_openapi_versionado_e_o_do_app(openapi: dict[str, Any]) -> None:
    versionado = json.loads(OPENAPI.read_text())

    assert versionado == openapi, (
        "docs/api/openapi.json atras do app: rode make docs-api"
    )


def _requisicoes(colecao: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for pasta in colecao["item"] for item in pasta["item"]]


def _par(pedido: dict[str, Any]) -> tuple[str, str]:
    caminho = "/" + "/".join(pedido["url"]["path"])
    return pedido["method"].lower(), re.sub(r"\{\{(\w+)\}\}", r"{\1}", caminho)


def test_colecao_postman_tem_uma_requisicao_por_rota_do_openapi(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text())

    pares = sorted(_par(item["request"]) for item in _requisicoes(colecao))

    assert pares == _pares(openapi)


def test_colecao_postman_e_a_que_o_script_gera(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    colecao = json.loads(COLECAO.read_text())

    assert colecao == gerador.colecao(openapi), "colecao atras do script: make docs-api"


def test_rota_com_token_na_colecao_e_a_que_o_openapi_protege(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text())

    sem_token = sorted(
        _par(item["request"])
        for item in _requisicoes(colecao)
        if item["request"].get("auth") == {"type": "noauth"}
    )

    assert colecao["auth"]["bearer"][0]["value"] == "{{token}}"
    assert sem_token == sorted(
        (metodo, rota)
        for rota, ops in openapi["paths"].items()
        for metodo, operacao in ops.items()
        if "security" not in operacao
    )


def test_exemplos_da_colecao_seguem_o_schema_da_rota(openapi: dict[str, Any]) -> None:
    erros: list[str] = []
    for item in _requisicoes(json.loads(COLECAO.read_text())):
        metodo, rota = _par(item["request"])
        operacao = openapi["paths"][rota][metodo]
        onde = f"{metodo.upper()} {rota}"
        if "requestBody" in operacao:
            corpo = json.loads(item["request"].get("body", {}).get("raw", "null"))
            erros += _fora_do_schema(openapi, operacao["requestBody"], corpo, onde)
        elif "body" in item["request"]:
            erros.append(f"{onde}: corpo numa rota sem requestBody")
        for resposta in item["response"]:
            declarada = operacao["responses"][str(resposta["code"])]
            if resposta["body"]:
                valor = json.loads(resposta["body"])
                erros += _fora_do_schema(openapi, declarada, valor, f"{onde} resposta")

    assert erros == []


def test_rota_nova_sem_exemplo_reprova_a_geracao(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    com_rota_nova = json.loads(json.dumps(openapi))
    com_rota_nova["paths"]["/api/v1/nova"] = {"get": {"summary": "x", "tags": ["X"]}}

    with pytest.raises(ValueError, match="faltam \\[\\('get', '/api/v1/nova'\\)\\]"):
        gerador.colecao(com_rota_nova)


# docs/arquitetura.md e as referencias do README.


def test_readme_aponta_a_arquitetura_e_os_documentos_da_api() -> None:
    texto = README.read_text()

    assert ARQUITETURA.is_file()
    assert "](docs/arquitetura.md)" in texto
    assert "](docs/api/)" in texto
