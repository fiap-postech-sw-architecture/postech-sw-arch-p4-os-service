"""Toda resposta que os testes de API recebem esta declarada no OpenAPI.

Durante cada teste o ``FastAPI.__call__`` roda espiado: a requisicao que uma rota do
servico atende (o endpoint vem de ``src``) deixa o metodo, o template da rota e o
status. No fim do teste, cada status tem de estar nas ``responses`` da operacao no
OpenAPI do app real. O status que a rota devolve e o OpenAPI nao diz reprova o teste
que o recebeu.

Ficam de fora o que nao e contrato de rota: a rota que o teste inventa (o endpoint
nao e de ``src``), o metodo que a rota nao tem (405) e o 500, que vale para toda
rota e nao se declara.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from fastapi import FastAPI

# Importado na coleta, e nao no fim do primeiro teste: o import cria o app e
# configura o logging, e um subconjunto de testes nao pode fazer isso no meio da
# execucao.
from src.main import app

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from starlette.types import Message, Receive, Scope, Send

# O 500 (ERRO_INTERNO) vale para toda rota e nao se declara.
_SEM_DECLARACAO = frozenset({500})


class Resposta(NamedTuple):
    metodo: str
    rota: str
    status: int


def anotar(scope: Scope, status: int | None, vistas: list[Resposta]) -> None:
    """Guarda a resposta de uma rota de ``src``; ignora qualquer outra."""
    rota = scope.get("route")
    endpoint = getattr(rota, "endpoint", None)
    template = getattr(rota, "path_format", None)
    if (
        status is not None
        and template is not None
        and getattr(endpoint, "__module__", "").startswith("src.")
    ):
        vistas.append(Resposta(scope["method"], template, status))


def espiar(vistas: list[Resposta]) -> Callable[..., Awaitable[None]]:
    """O ``FastAPI.__call__`` que guarda em ``vistas`` o que cada rota respondeu."""
    original = FastAPI.__call__

    async def chamar(self: FastAPI, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await original(self, scope, receive, send)
            return
        status: int | None = None

        async def enviar(mensagem: Message) -> None:
            nonlocal status
            if mensagem["type"] == "http.response.start":
                status = mensagem["status"]
            await send(mensagem)

        try:
            await original(self, scope, receive, enviar)
        finally:
            anotar(scope, status, vistas)

    return chamar


def nao_declaradas(
    openapi: dict[str, Any], vistas: Iterable[Resposta]
) -> list[Resposta]:
    """As respostas vistas que o OpenAPI nao declara na operacao delas."""
    fora = []
    for resposta in dict.fromkeys(vistas):
        operacao = openapi["paths"].get(resposta.rota, {}).get(resposta.metodo.lower())
        if operacao is None or resposta.status in _SEM_DECLARACAO:
            continue
        declaradas = operacao["responses"]
        if str(resposta.status) not in declaradas and "default" not in declaradas:
            fora.append(resposta)
    return fora


def conferir(vistas: Iterable[Resposta]) -> None:
    """Reprova se o app real devolveu o que o seu OpenAPI nao declara."""
    fora = nao_declaradas(app.openapi(), vistas)
    assert not fora, (
        "o OpenAPI nao declara o que a API devolveu neste teste "
        "(complete o `responses` da rota em src e rode make docs-api): "
        + "; ".join(f"{r.metodo} {r.rota} -> {r.status}" for r in fora)
    )
