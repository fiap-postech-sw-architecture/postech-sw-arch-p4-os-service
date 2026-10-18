"""Documentos do servico: README, docs/arquitetura.md e docs/api.

- Os comandos dos blocos ``bash`` do README rodam colados no terminal.
- ``docs/api`` (OpenAPI e colecao Postman) acompanha o app (``make docs-api``).
- Toda rota do OpenAPI tem um ``curl`` no README, com corpo que segue o schema.
- O README aponta ``docs/arquitetura.md`` e ``docs/api``.
- A secao Cobertura do README sai de ``scripts/cobertura_resumo.py``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

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


# README: um curl por rota do OpenAPI.

# Opcoes do curl que levam valor; as demais (-s, -k, -f...) sao flags.
_DADOS = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--json"})
_OPCOES_COM_VALOR = _DADOS | frozenset(
    {"-X", "--request", "-H", "--header", "-o", "--output", "-w", "--write-out"}
    | {"-m", "--max-time", "-u", "--user"}
)
_VARIAVEL = re.compile(r"\$\{?\w+\}?")
_UUID_QUALQUER = "00000000-0000-4000-8000-000000000000"


class Curl(NamedTuple):
    metodo: str
    caminho: str
    corpo: str | None


def _curl(trecho: str) -> Curl | None:
    """O ``curl`` do comeco de ``trecho``, ate o pipe, o ``)`` ou o ``;``."""
    lexer = shlex.shlex(trecho, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    metodo, url, corpo = "", "", None
    palavras = iter(lexer)
    for palavra in palavras:
        if palavra[:1] in {"|", ")", ";", "&", "<", ">"}:
            break
        if palavra in _OPCOES_COM_VALOR:
            valor = next(palavras)
            metodo = valor.upper() if palavra in {"-X", "--request"} else metodo
            corpo = valor if palavra in _DADOS else corpo
        elif not palavra.startswith("-") and not url:
            url = palavra
    caminho = re.match(r"(?:https?://)?[^/]+(/[^?#]*)", url)
    if caminho is None:
        return None
    return Curl(metodo or ("POST" if corpo else "GET"), caminho[1], corpo)


def _curls(texto: str) -> list[Curl]:
    chamadas = []
    for bloco in _BLOCO.findall(texto):
        for linha in bloco.replace("\\\n", " ").splitlines():
            for trecho in re.split(r"(?<![\w-])curl\s", linha)[1:]:
                chamada = _curl(trecho)
                if chamada is not None:
                    chamadas.append(chamada)
    return chamadas


def _casa(caminho: str, rota: str) -> bool:
    # O parametro da rota casa com uma variavel do shell ($ORDEM_ID), nunca com
    # um texto fixo: `/ordens-de-servico/metricas` nao vale por `/{ordem_id}`.
    partes, modelo = caminho.split("/"), rota.split("/")
    return len(partes) == len(modelo) and all(
        parte == trecho
        or (trecho.startswith("{") and _VARIAVEL.fullmatch(parte) is not None)
        for parte, trecho in zip(partes, modelo, strict=True)
    )


def _rota(openapi: dict[str, Any], curl: Curl) -> tuple[str, str] | None:
    for metodo, rota in _pares(openapi):
        if metodo.upper() == curl.metodo and _casa(curl.caminho, rota):
            return metodo, rota
    return None


def test_leitura_dos_curl_do_readme() -> None:
    texto = (
        "```bash\n"
        "TOKEN=$(curl -s -X POST localhost:8000/api/v1/autenticacao/login \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"email": "a@b.dev"}\' | jq -r .access_token)\n'
        'curl -s "localhost:8000/api/v1/clientes?limit=5" -H "Authorization: x"\n'
        "curl -s -X DELETE localhost:8000/api/v1/clientes/$CLIENTE_ID; echo\n"
        "curl -sk https://localhost/os/api/v1/saude\n"
        'curl -s localhost:8000/x -d \'{"r": "\'"$REFRESH"\'"}\'\n'
        "curl -s --data-binary '{\"a\": 1}' localhost:8000/y\n"
        "```\n"
    )

    assert _curls(texto) == [
        Curl("POST", "/api/v1/autenticacao/login", '{"email": "a@b.dev"}'),
        Curl("GET", "/api/v1/clientes", None),
        Curl("DELETE", "/api/v1/clientes/$CLIENTE_ID", None),
        Curl("GET", "/os/api/v1/saude", None),
        Curl("POST", "/x", '{"r": "$REFRESH"}'),
        Curl("POST", "/y", '{"a": 1}'),
    ]


def test_parametro_da_rota_so_casa_com_variavel_do_shell() -> None:
    assert _casa("/api/v1/sagas/$ORDEM_ID", "/api/v1/sagas/{ordem_id}")
    assert _casa("/api/v1/sagas/${ORDEM_ID}", "/api/v1/sagas/{ordem_id}")
    assert not _casa(
        "/api/v1/ordens-de-servico/metricas", "/api/v1/ordens-de-servico/{ordem_id}"
    )
    assert not _casa("/api/v1/sagas/$ORDEM_ID/compensacao", "/api/v1/sagas/{ordem_id}")


def test_toda_rota_do_openapi_tem_um_curl_no_readme(openapi: dict[str, Any]) -> None:
    cobertas = {_rota(openapi, curl) for curl in _curls(README.read_text())}

    faltam = [par for par in _pares(openapi) if par not in cobertas]

    assert faltam == [], "rota do OpenAPI sem curl no README (secao Exemplos da API)"


def test_corpo_dos_curl_do_readme_segue_o_schema_da_rota(
    openapi: dict[str, Any],
) -> None:
    erros: list[str] = []
    for curl in _curls(README.read_text()):
        par = _rota(openapi, curl)
        if par is None:
            continue
        operacao = openapi["paths"][par[1]][par[0]]
        onde = f"{curl.metodo} {curl.caminho}"
        if "requestBody" not in operacao:
            erros += [f"{onde}: corpo numa rota sem requestBody"] if curl.corpo else []
        elif curl.corpo is None:
            erros.append(f"{onde}: rota com requestBody e curl sem corpo")
        else:
            corpo = json.loads(_VARIAVEL.sub(_UUID_QUALQUER, curl.corpo))
            erros += _fora_do_schema(openapi, operacao["requestBody"], corpo, onde)

    assert erros == []


# docs/arquitetura.md e as referencias do README.


def test_readme_aponta_a_arquitetura_e_os_documentos_da_api() -> None:
    texto = README.read_text()

    assert ARQUITETURA.is_file()
    assert "](docs/arquitetura.md)" in texto
    assert "](docs/api/)" in texto


# A secao Cobertura do README: gerada pelo script, nunca digitada.

_LINHA_DA_TABELA = re.compile(
    r"^\| (?P<rotulo>[^|]+?) \| (?P<total>[\d.]+) \| (?P<cobertas>[\d.]+)"
    r" \| [\d,]+% \|$",
    re.MULTILINE,
)
_RAMOS = re.compile(r"e (?P<total>[\d.]+) ramos, (?P<cobertos>[\d.]+) cobertos")
_RUN = re.compile(
    r"\]\((?P<url>https://github\.com/fiap-postech-sw-architecture/"
    r"postech-sw-arch-p4-os-service/actions/runs/\d+)\)"
)


def _numero(texto: str) -> int:
    return int(texto.replace(".", ""))


def _tabela(texto: str) -> dict[str, tuple[int, int]]:
    return {
        linha["rotulo"]: (_numero(linha["total"]), _numero(linha["cobertas"]))
        for linha in _LINHA_DA_TABELA.finditer(texto)
        if linha["rotulo"] != "**total**"
    }


def test_secao_de_cobertura_do_readme_e_a_que_o_script_gera() -> None:
    cobertura = _modulo("cobertura_resumo")
    texto = README.read_text()
    assert texto.count(cobertura.MARCADOR_INICIO) == 1
    assert texto.count(cobertura.MARCADOR_FIM) == 1
    secao = texto.split(cobertura.MARCADOR_INICIO)[1].split(cobertura.MARCADOR_FIM)[0]
    contextos, camadas = secao.split("| Camada |")
    ramos, run = _RAMOS.search(secao), _RUN.search(secao)
    assert ramos is not None, "secao Cobertura sem a contagem dos ramos"
    assert run is not None, "secao Cobertura sem o link do run do CI"

    # Ponto fixo: os numeros que a secao mostra, passados de novo pelo script,
    # devolvem a secao inteira. Uma linha editada a mao (percentual, total,
    # formato) ou o link de outro repositorio nao passam.
    medida = cobertura.Medida(
        contextos=_tabela(contextos),
        camadas=_tabela("| Camada |" + camadas),
        ramos=(_numero(ramos["total"]), _numero(ramos["cobertos"])),
    )
    assert f"\n{cobertura.secao_do_readme(medida, run['url'])}\n" == secao, (
        "secao Cobertura editada a mao: rode make cobertura-readme RUN=<id do run>"
    )
