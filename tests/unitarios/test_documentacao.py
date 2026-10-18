"""Documentos do servico: README, docs/arquitetura.md e docs/api.

- Os comandos dos blocos ``bash`` do README rodam colados no terminal.
- ``docs/api`` (OpenAPI e colecao Postman) acompanha o app (``make docs-api``),
  e a colecao guarda o token e os ids que as outras requisicoes usam.
- Toda rota do OpenAPI tem um ``curl`` na secao Exemplos da API do README, e
  cada ``curl`` traz o corpo no schema e os cabecalhos que a operacao pede.
- ``docs/arquitetura.md`` segue o codigo: o ER e as tabelas, o metadata dos
  mapeamentos; os estados, a ``MaquinaDeStatus``; a saga, o ``FLUXO_NORMAL``; a
  mensageria, o catalogo do AsyncAPI e a ``ETAPA_ESPERADA``.
- Os links relativos do README e da arquitetura caem em arquivo e secao reais.
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

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.infraestrutura.bootstrap import iniciar_todos_mapeamentos
from src.compartilhado.infraestrutura.database import metadata
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.main import app
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    ETAPA_ESPERADA,
    FLUXO_NORMAL,
)
from src.ordem_servico.dominio.maquina_de_status import MaquinaDeStatus
from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS, StatusOrdem

if TYPE_CHECKING:
    from collections.abc import Iterator
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
    texto = README.read_text(encoding="utf-8")

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


def _modulo(nome: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(nome, RAIZ / "scripts" / f"{nome}.py")
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    # O dataclass do modulo procura o proprio modulo em sys.modules; o
    # monkeypatch o tira de la no fim, sem deixar estado para os outros testes.
    monkeypatch.setitem(sys.modules, nome, modulo)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    # Ida e volta pelo JSON: e assim que o documento chega ao arquivo.
    documento: dict[str, Any] = json.loads(json.dumps(app.openapi()))
    return documento


@pytest.fixture(scope="module")
def gerador() -> Iterator[ModuleType]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield _modulo("gerar_docs_api", monkeypatch)


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
    validador = Draft202012Validator(
        {**schema, "components": openapi["components"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    return [f"{onde}: {erro.message}" for erro in validador.iter_errors(valor)]


# docs/api: o OpenAPI e a colecao Postman.


def test_openapi_versionado_e_o_do_app(openapi: dict[str, Any]) -> None:
    versionado = json.loads(OPENAPI.read_text(encoding="utf-8"))

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
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))

    pares = sorted(_par(item["request"]) for item in _requisicoes(colecao))

    assert pares == _pares(openapi)


def test_colecao_postman_e_a_que_o_script_gera(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))

    assert colecao == gerador.colecao(openapi), "colecao atras do script: make docs-api"


def test_rota_com_token_na_colecao_e_a_que_o_openapi_protege(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))

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


_GUARDA = re.compile(
    r"if \(pm\.response\.code === (\d+)\) "
    r'pm\.collectionVariables\.set\("(\w+)", pm\.response\.json\(\)\.(\w+)\);'
)


def test_login_e_criacoes_guardam_o_token_e_os_ids_que_as_outras_usam() -> None:
    texto = COLECAO.read_text(encoding="utf-8")
    guardadas: dict[str, list[tuple[str, str]]] = {}
    for item in _requisicoes(json.loads(texto)):
        resposta = item["response"][0]
        for evento in item.get("event", []):
            assert evento["listen"] == "test"
            for linha in evento["script"]["exec"]:
                # Uma linha por variavel, com o status e um campo da resposta.
                ((status, variavel, campo),) = _GUARDA.findall(linha)
                assert int(status) == resposta["code"]
                assert campo in json.loads(resposta["body"])
                guardadas.setdefault(variavel, []).append(_par(item["request"]))

    assert set(guardadas) == set(re.findall(r"\{\{(\w+)\}\}", texto)) - {"baseUrl"}
    assert guardadas["token"] == [
        ("post", "/api/v1/autenticacao/login"),
        ("post", "/api/v1/autenticacao/refresh"),
    ]


def _erros_do_exemplo(openapi: dict[str, Any], item: dict[str, Any]) -> list[str]:
    """O corpo e as respostas de exemplo de uma requisicao contra os schemas."""
    metodo, rota = _par(item["request"])
    operacao = openapi["paths"][rota][metodo]
    onde = f"{metodo.upper()} {rota}"
    erros: list[str] = []
    if "requestBody" in operacao:
        # A variavel da colecao ({{cliente_id}}) vale um uuid, como a do shell no curl.
        bruto = item["request"].get("body", {}).get("raw", "null")
        corpo = json.loads(re.sub(r"\{\{\w+\}\}", _UUID_QUALQUER, bruto))
        erros += _fora_do_schema(openapi, operacao["requestBody"], corpo, onde)
    elif "body" in item["request"]:
        erros.append(f"{onde}: corpo numa rota sem requestBody")
    for resposta in item["response"]:
        declarada = operacao["responses"][str(resposta["code"])]
        if resposta["body"]:
            valor = json.loads(resposta["body"])
            erros += _fora_do_schema(openapi, declarada, valor, f"{onde} resposta")
    return erros


def test_exemplos_da_colecao_seguem_o_schema_da_rota(openapi: dict[str, Any]) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))

    erros = [
        erro
        for item in _requisicoes(colecao)
        for erro in _erros_do_exemplo(openapi, item)
    ]

    assert erros == []


def test_exemplo_com_corpo_a_mais_ou_fora_do_formato_reprova(
    openapi: dict[str, Any],
) -> None:
    saude = {
        "request": {
            "method": "GET",
            "url": {"path": ["api", "v1", "saude"]},
            "body": {},
        },
        "response": [{"code": 200, "body": ""}],
    }
    abertura = {
        "request": {
            "method": "POST",
            "url": {"path": ["api", "v1", "ordens-de-servico"]},
            "body": {
                "raw": json.dumps(
                    {
                        "cliente_id": "nao-e-um-uuid",
                        "veiculo_id": _UUID_QUALQUER,
                        "descricao_problema": "Freio",
                    }
                )
            },
        },
        "response": [{"code": 201, "body": ""}],
    }

    assert _erros_do_exemplo(openapi, saude) == [
        "GET /api/v1/saude: corpo numa rota sem requestBody"
    ]
    assert _erros_do_exemplo(openapi, abertura) == [
        "POST /api/v1/ordens-de-servico: 'nao-e-um-uuid' is not a 'uuid'"
    ]
    assert _fora_do_schema(openapi, {}, None, "GET /x") == [
        "GET /x: a rota nao declara corpo JSON"
    ]


def test_rota_nova_sem_exemplo_reprova_a_geracao(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    com_rota_nova = json.loads(json.dumps(openapi))
    com_rota_nova["paths"]["/api/v1/nova"] = {"get": {"summary": "x", "tags": ["X"]}}

    with pytest.raises(ValueError, match="faltam \\[\\('get', '/api/v1/nova'\\)\\]"):
        gerador.colecao(com_rota_nova)


@pytest.fixture
def docs_api_temporario(
    gerador: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """O ``main()`` do gerador escrevendo num ``docs/api`` de ``tmp_path``."""
    destino = tmp_path / "docs" / "api"
    monkeypatch.setattr(gerador, "RAIZ", tmp_path)
    monkeypatch.setattr(gerador, "DOCS_API", destino)
    monkeypatch.setattr(gerador, "OPENAPI", destino / OPENAPI.name)
    monkeypatch.setattr(gerador, "COLECAO", destino / COLECAO.name)
    return destino


def test_main_grava_os_arquivos_versionados_byte_a_byte_e_de_novo_igual(
    gerador: ModuleType,
    docs_api_temporario: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versionados = {
        OPENAPI.name: OPENAPI.read_bytes(),
        COLECAO.name: COLECAO.read_bytes(),
    }

    assert gerador.main() == 0
    primeira = {p.name: p.read_bytes() for p in docs_api_temporario.iterdir()}
    assert gerador.main() == 0

    assert primeira == versionados, "docs/api atras do codigo: rode make docs-api"
    assert {p.name: p.read_bytes() for p in docs_api_temporario.iterdir()} == primeira
    assert capsys.readouterr().out.splitlines()[0] == (
        f"atualizados: docs/api/{OPENAPI.name} e docs/api/{COLECAO.name}"
    )


def test_main_com_rota_sem_exemplo_reprova_sem_gravar(
    gerador: ModuleType,
    docs_api_temporario: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delitem(gerador.EXEMPLOS, ("get", "/api/v1/saude"))

    assert gerador.main() == 1
    assert not docs_api_temporario.exists()
    assert "faltam [('get', '/api/v1/saude')]" in capsys.readouterr().err


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
    # Os nomes dos cabecalhos, em minusculas.
    cabecalhos: frozenset[str] = frozenset()


def _curl(trecho: str) -> Curl | None:
    """O ``curl`` do comeco de ``trecho``, ate o pipe, o ``)`` ou o ``;``."""
    lexer = shlex.shlex(trecho, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    metodo, url, corpo = "", "", None
    cabecalhos: set[str] = set()
    palavras = iter(lexer)
    for palavra in palavras:
        if palavra[:1] in {"|", ")", ";", "&", "<", ">"}:
            break
        if palavra in _OPCOES_COM_VALOR:
            valor = next(palavras)
            metodo = valor.upper() if palavra in {"-X", "--request"} else metodo
            corpo = valor if palavra in _DADOS else corpo
            if palavra in {"-H", "--header"}:
                cabecalhos.add(valor.partition(":")[0].strip().lower())
            elif palavra == "--json":  # o curl ja manda o Content-Type
                cabecalhos.add("content-type")
        elif not palavra.startswith("-") and not url:
            url = palavra
    caminho = re.match(r"(?:https?://)?[^/]+(/[^?#]*)", url)
    if caminho is None:
        return None
    metodo = metodo or ("POST" if corpo else "GET")
    return Curl(metodo, caminho[1], corpo, frozenset(cabecalhos))


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
        "curl -s --json '{\"b\": 2}' localhost:8000/z\n"
        "curl --version\n"
        "```\n"
    )

    assert _curls(texto) == [
        Curl(
            "POST",
            "/api/v1/autenticacao/login",
            '{"email": "a@b.dev"}',
            frozenset({"content-type"}),
        ),
        Curl("GET", "/api/v1/clientes", None, frozenset({"authorization"})),
        Curl("DELETE", "/api/v1/clientes/$CLIENTE_ID", None),
        Curl("GET", "/os/api/v1/saude", None),
        Curl("POST", "/x", '{"r": "$REFRESH"}'),
        Curl("POST", "/y", '{"a": 1}'),
        Curl("POST", "/z", '{"b": 2}', frozenset({"content-type"})),
    ]


def test_parametro_da_rota_so_casa_com_variavel_do_shell() -> None:
    assert _casa("/api/v1/sagas/$ORDEM_ID", "/api/v1/sagas/{ordem_id}")
    assert _casa("/api/v1/sagas/${ORDEM_ID}", "/api/v1/sagas/{ordem_id}")
    assert not _casa(
        "/api/v1/ordens-de-servico/metricas", "/api/v1/ordens-de-servico/{ordem_id}"
    )
    assert not _casa("/api/v1/sagas/$ORDEM_ID/compensacao", "/api/v1/sagas/{ordem_id}")


def test_toda_rota_do_openapi_tem_um_curl_na_secao_exemplos_da_api(
    openapi: dict[str, Any],
) -> None:
    exemplos = _secao(README.read_text(encoding="utf-8"), "## Exemplos da API")
    cobertas = {_rota(openapi, curl) for curl in _curls(exemplos)}

    faltam = [par for par in _pares(openapi) if par not in cobertas]

    assert faltam == [], "rota do OpenAPI sem curl no README (secao Exemplos da API)"


def _erros_do_curl(openapi: dict[str, Any], curl: Curl) -> list[str]:
    """O que a operacao pede e o curl nao traz: token, corpo e Content-Type."""
    par = _rota(openapi, curl)
    if par is None:
        return []
    operacao = openapi["paths"][par[1]][par[0]]
    onde = f"{curl.metodo} {curl.caminho}"
    erros: list[str] = []
    if "security" in operacao and "authorization" not in curl.cabecalhos:
        erros.append(f"{onde}: rota protegida sem Authorization")
    if "requestBody" not in operacao:
        erros += [f"{onde}: corpo numa rota sem requestBody"] if curl.corpo else []
    elif curl.corpo is None:
        erros.append(f"{onde}: rota com requestBody e curl sem corpo")
    else:
        if "content-type" not in curl.cabecalhos:
            erros.append(f"{onde}: corpo sem Content-Type (a API responderia 422)")
        corpo = json.loads(_VARIAVEL.sub(_UUID_QUALQUER, curl.corpo))
        erros += _fora_do_schema(openapi, operacao["requestBody"], corpo, onde)
    return erros


def test_curl_do_readme_tem_o_token_o_corpo_e_o_content_type_que_a_rota_pede(
    openapi: dict[str, Any],
) -> None:
    curls = _curls(README.read_text(encoding="utf-8"))

    erros = [erro for curl in curls for erro in _erros_do_curl(openapi, curl)]

    assert len(curls) > 30
    assert erros == []


def test_curl_sem_token_sem_content_type_ou_com_corpo_errado_reprova(
    openapi: dict[str, Any],
) -> None:
    abertura = "/api/v1/ordens-de-servico"
    corpo = (
        '{"cliente_id": "nao-e-um-uuid", "veiculo_id": "$V", "descricao_problema": "x"}'
    )
    com_token = frozenset({"authorization"})

    assert _erros_do_curl(openapi, Curl("POST", abertura, None)) == [
        f"POST {abertura}: rota protegida sem Authorization",
        f"POST {abertura}: rota com requestBody e curl sem corpo",
    ]
    assert _erros_do_curl(openapi, Curl("POST", abertura, corpo, com_token)) == [
        f"POST {abertura}: corpo sem Content-Type (a API responderia 422)",
        f"POST {abertura}: 'nao-e-um-uuid' is not a 'uuid'",
    ]
    assert _erros_do_curl(openapi, Curl("GET", "/api/v1/saude", "{}")) == [
        "GET /api/v1/saude: corpo numa rota sem requestBody"
    ]


# docs/arquitetura.md e as referencias do README.


def test_readme_aponta_a_arquitetura_e_os_documentos_da_api() -> None:
    texto = README.read_text(encoding="utf-8")

    assert ARQUITETURA.is_file()
    assert "](docs/arquitetura.md)" in texto
    assert "](docs/api/)" in texto


_CERCA = re.compile(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)


def _ancoras(texto: str) -> set[str]:
    """As ancoras que o GitHub gera para os titulos do Markdown.

    Minusculas, sem pontuacao, espaco vira hifen, e o titulo repetido ganha
    ``-1``, ``-2``... Os blocos de codigo ficam de fora (``#`` neles nao e titulo).
    """
    ancoras: set[str] = set()
    vistas: dict[str, int] = {}
    for titulo in re.findall(r"^#{1,6} (.+)$", _CERCA.sub("", texto), re.MULTILINE):
        base = re.sub(r"[^\w\- ]", "", titulo.strip().lower()).replace(" ", "-")
        repeticao = vistas.get(base, 0)
        vistas[base] = repeticao + 1
        ancoras.add(f"{base}-{repeticao}" if repeticao else base)
    return ancoras


def test_ancora_gerada_como_a_do_github() -> None:
    texto = "# Ação (LGPD)\n```bash\n# nao e titulo\n```\n## Testes\n### Testes\n"

    assert _ancoras(texto) == {"ação-lgpd", "testes", "testes-1"}


def _links_relativos(origem: Path) -> list[tuple[str, Path, str]]:
    """(alvo, arquivo de destino, ancora) dos links de ``origem`` para o repositorio."""
    links = []
    for alvo in re.findall(
        r"\]\(([^)\s]+)\)", _CERCA.sub("", origem.read_text(encoding="utf-8"))
    ):
        caminho, _, ancora = alvo.partition("#")
        # http:, https:, mailto: saem do repositorio.
        if re.match(r"[a-z]+:", caminho) is None:
            links.append((alvo, origem.parent / caminho if caminho else origem, ancora))
    return links


def _quebrados(links: list[tuple[str, Path, str]]) -> list[str]:
    return [
        alvo
        for alvo, destino, ancora in links
        if not destino.exists()
        or (
            ancora != ""
            and destino.suffix == ".md"
            and ancora not in _ancoras(destino.read_text(encoding="utf-8"))
        )
    ]


def test_links_relativos_do_readme_e_da_arquitetura_existem() -> None:
    links = [*_links_relativos(README), *_links_relativos(ARQUITETURA)]

    assert len(links) > 60
    assert _quebrados(links) == [], "link para arquivo ou secao que nao existe"


def test_link_para_arquivo_ou_secao_que_nao_existe_reprova(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# Seção A\n", encoding="utf-8")
    origem = tmp_path / "README.md"
    origem.write_text(
        "# Topo\n[a](docs/a.md#seção-a) [b](#topo) [c](docs/) [d](https://x.dev/#y)\n"
        "[e](docs/b.md) [f](docs/a.md#outra) [g](#fim) [h](docs/a.md)\n"
        "```bash\n[nao e link](nada.md)\n```\n",
        encoding="utf-8",
    )

    assert _quebrados(_links_relativos(origem)) == [
        "docs/b.md",
        "docs/a.md#outra",
        "#fim",
    ]


# docs/arquitetura.md: o modelo de dados, os estados da OS e a saga, lidos do codigo.


def _secao(texto: str, titulo: str) -> str:
    """De ``titulo`` (com os ``#``) ate o proximo titulo do mesmo nivel ou acima."""
    nivel = len(titulo.split(" ", 1)[0])
    resto = texto.split(f"\n{titulo}\n", 1)[1]
    return re.split(rf"\n#{{1,{nivel}}} ", resto, maxsplit=1)[0]


def _mermaid(texto: str, tipo: str) -> str:
    (bloco,) = re.findall(rf"```mermaid\n{tipo}\n(.*?)```", texto, re.DOTALL)
    return str(bloco)


def _colunas_do_banco() -> set[tuple[str, str]]:
    iniciar_todos_mapeamentos()
    return {
        (tabela.name, coluna.name)
        for tabela in metadata.tables.values()
        for coluna in tabela.columns
    }


def _colunas_do_er(texto: str) -> set[tuple[str, str]]:
    entidades = re.findall(
        r"^ {4}(\w+) \{\n(.*?)^ {4}\}", _mermaid(texto, "erDiagram"), re.M | re.S
    )
    return {
        (tabela, linha.split()[1])
        for tabela, corpo in entidades
        for linha in corpo.splitlines()
    }


def _colunas_das_tabelas(texto: str, tabelas: set[str]) -> set[tuple[str, str]]:
    """(tabela, coluna) das tabelas Markdown da secao 3.

    Cada tabela vem depois do paragrafo que comeca com o nome dela entre
    crases; a primeira celula de cada linha traz uma ou mais colunas.
    """
    colunas: set[tuple[str, str]] = set()
    atual = ""
    for linha in _CERCA.sub("", _secao(texto, "## 3. Modelo de dados")).splitlines():
        nome = re.match(r"`(\w+)`", linha)
        if nome is not None and nome[1] in tabelas:
            atual = nome[1]
        elif linha.startswith("| `"):
            primeira = linha.split("|")[1]
            colunas |= {(atual, coluna) for coluna in re.findall(r"`(\w+)`", primeira)}
    return colunas


def test_er_da_arquitetura_tem_as_colunas_dos_mapeamentos() -> None:
    texto = ARQUITETURA.read_text(encoding="utf-8")
    banco = _colunas_do_banco()

    assert len(banco) > 90
    assert _colunas_do_er(texto) == banco, "coluna nova: o ER de docs/arquitetura.md"


def test_tabelas_da_arquitetura_tem_as_colunas_dos_mapeamentos() -> None:
    texto = ARQUITETURA.read_text(encoding="utf-8")
    banco = _colunas_do_banco()

    tabelas = _colunas_das_tabelas(texto, {tabela for tabela, _ in banco})

    assert tabelas == banco, "coluna nova: a tabela dela em docs/arquitetura.md"


def test_estados_da_os_na_arquitetura_sao_os_da_maquina_de_status() -> None:
    diagrama = _mermaid(ARQUITETURA.read_text(encoding="utf-8"), "stateDiagram-v2")
    setas = set(re.findall(r"^ +(\S+) --> ([^\s:]+)", diagrama, re.M))
    maquina = MaquinaDeStatus()

    assert {seta for seta in setas if "[*]" not in seta} == {
        (de.value, para.value)
        for de in StatusOrdem
        for para in maquina.transicoes_validas(de)
    }
    assert {de for de, para in setas if para == "[*]"} == {
        status.value for status in ESTADOS_TERMINAIS
    }
    assert {para for de, para in setas if de == "[*]"} == {StatusOrdem.RECEBIDA.value}


def test_linhas_da_saga_na_arquitetura_sao_as_do_fluxo_normal() -> None:
    texto = ARQUITETURA.read_text(encoding="utf-8")
    linhas: dict[str, tuple[str, str]] = {}
    for passo in re.findall(r"^\| T\d+ \|.*$", texto, re.M):
        _, evento, _, etapa, comando = (c.strip() for c in passo.strip("|").split("|"))
        # As linhas das requisicoes (abertura e entrega) nao sao evento da saga.
        if evento.startswith("`"):
            linhas[evento.split("`")[1]] = (etapa.strip("`"), comando.strip("`"))

    assert linhas == {
        evento: (t.seguinte, t.comando or "nenhum")
        for evento, t in FLUXO_NORMAL.items()
    }


def test_toda_etapa_da_saga_aparece_na_arquitetura() -> None:
    citadas = set(re.findall(r"`(\w+)`", ARQUITETURA.read_text(encoding="utf-8")))

    assert {etapa.value for etapa in EtapaSaga} - citadas == set()


def _celulas(trecho: str) -> dict[str, list[str]]:
    """Primeira celula de cada linha de dados das tabelas -> as demais, sem crase."""
    linhas = [
        [celula.strip().strip("`") for celula in linha.strip("|").split("|")]
        for linha in trecho.splitlines()
        if linha.startswith("| `")
    ]
    tabela = {primeira: resto for primeira, *resto in linhas}
    assert len(tabela) == len(linhas), "linha repetida na tabela"
    return tabela


def test_comandos_publicados_na_arquitetura_sao_os_do_catalogo() -> None:
    secao = _secao(ARQUITETURA.read_text(encoding="utf-8"), "### Comandos publicados")

    assert {comando: celulas[0] for comando, celulas in _celulas(secao).items()} == {
        comando.value: catalogo().destino(comando).routing_key for comando in Comando
    }


def test_eventos_consumidos_na_arquitetura_sao_os_do_catalogo() -> None:
    secao = _secao(ARQUITETURA.read_text(encoding="utf-8"), "### Eventos consumidos")

    assert {evento: celulas[:2] for evento, celulas in _celulas(secao).items()} == {
        evento: [catalogo().produtor(evento), ETAPA_ESPERADA[evento].value]
        for evento in catalogo().consumidos
    }


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


def test_secao_de_cobertura_do_readme_e_a_que_o_script_gera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cobertura = _modulo("cobertura_resumo", monkeypatch)
    texto = README.read_text(encoding="utf-8")
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
