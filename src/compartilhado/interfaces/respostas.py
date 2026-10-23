"""Respostas de erro das rotas no OpenAPI, no envelope do contrato (ADR-039).

Entradas do ``responses`` do FastAPI. Todo erro do servico sai como
``{erro: {codigo, mensagem, id_requisicao}}`` (``error_handler.py``) e o exemplo
mostra o ``codigo`` estavel que o cliente pode ler. A rota declara so o status que
devolve: ``tests/respostas_da_api.py`` reprova o teste de API que recebe um status
que o OpenAPI nao diz.
"""

from __future__ import annotations

from typing import Any

from limits import parse

# Qualquer valor de exemplo: o que importa na documentacao e o formato.
_ID_REQUISICAO = "3f1e6a52-9d0b-4c1c-8a55-0d6b9b7a1c11"


def erro(descricao: str, codigo: str, mensagem: str) -> dict[str, Any]:
    """Resposta de erro documentada, com o envelope de exemplo.

    ``Any`` e o tipo do valor do parametro ``responses`` do FastAPI.
    """
    return {
        "description": descricao,
        "content": {
            "application/json": {
                "example": {
                    "erro": {
                        "codigo": codigo,
                        "mensagem": mensagem,
                        "id_requisicao": _ID_REQUISICAO,
                    }
                }
            }
        },
    }


def limite_excedido(limite: str) -> dict[str, Any]:
    """429 do ``@limiter.limit(limite)``: ``limite`` na notacao do ``limits``.

    O texto do limite na mensagem e o do ``limits`` (``5 per 1 minute``), o mesmo
    que o SlowAPI poe no ``detail`` da resposta real.
    """
    return erro(
        f"Rate limit excedido ({limite} por IP).",
        "RATE_LIMIT_EXCEDIDO",
        f"Limite de requisicoes excedido: {parse(limite)}",
    )
