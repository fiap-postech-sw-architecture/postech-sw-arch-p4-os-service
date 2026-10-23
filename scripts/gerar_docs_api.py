"""Gera os documentos da API em docs/api a partir do proprio app (make docs-api).

- ``docs/api/openapi.json``: o ``app.openapi()`` do servico, o mesmo do ``/docs``.
- ``docs/api/os-service.postman_collection.json``: colecao Postman v2.1 com uma
  requisicao por metodo e rota do OpenAPI, cada uma com corpo e resposta de
  exemplo (``EXEMPLOS``) e as variaveis ``{{baseUrl}}`` e ``{{token}}``; o
  login, o refresh e as criacoes guardam o token e os ids num script de teste.

Os dois saem juntos, do mesmo OpenAPI, para nao divergirem. O script so monta o
app e le o OpenAPI dele: nao sobe servidor nem banco. ``tests/unitarios/
test_documentacao.py`` reprova quando os arquivos ficam atras do codigo, quando
falta exemplo para uma rota ou quando um exemplo sai do schema da rota.
"""

from __future__ import annotations

import json
import re
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

RAIZ = Path(__file__).resolve().parents[1]
DOCS_API = RAIZ / "docs" / "api"
OPENAPI = DOCS_API / "openapi.json"
COLECAO = DOCS_API / "os-service.postman_collection.json"

ESQUEMA_POSTMAN = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
DESCRICAO = (
    "Uma requisicao por rota do OS Service, com corpo e resposta de exemplo. "
    "baseUrl aponta para a API do compose (make compose-up) e pode ser trocada "
    "pelo Kong do kind (https://localhost/os; o certificado e autoassinado: "
    "desligue a verificacao de SSL do Postman). O script de teste do login (o "
    "admin de demonstracao do compose) e o do refresh guardam token, o "
    "access_token que as rotas protegidas mandam como Bearer, e refresh_token; os "
    "do cadastro do cliente, do veiculo e da abertura da OS guardam cliente_id, "
    "veiculo_id e ordem_id (antes deles, valem os ids dos exemplos). As pastas "
    "seguem as tags do OpenAPI, e nao um roteiro: para rodar em sequencia, siga a "
    "ordem de docs/api/exemplos.md, com as remocoes do cliente depois "
    "da abertura da OS e o logout, que revoga o token, por ultimo. Gerada por "
    "scripts/gerar_docs_api.py (make docs-api); o OpenAPI esta em "
    "docs/api/openapi.json."
)
# Os tokens daqui sao variaveis vazias e marcadores da colecao, nao credenciais:
# o bandit (B105) os toma por senha pelo nome da chave.
VARIAVEIS = {
    "baseUrl": "http://localhost:8000",
    "token": "",  # nosec B105
    "refresh_token": "",  # nosec B105
    "cliente_id": "facfeed2-183a-4eac-bbea-2f7647625c88",
    "veiculo_id": "12fdbbf4-044c-480b-8c44-6d7988ac4b1a",
    "ordem_id": "3f6e1c2a-8d4b-4f7e-9a11-5c2d0b7e9f40",
}

_CLIENTE = VARIAVEIS["cliente_id"]
_VEICULO = VARIAVEIS["veiculo_id"]
_ORDEM = VARIAVEIS["ordem_id"]
_ATENDENTE = "9809e49f-24a6-4e03-a4bf-c655feb2a0dd"
_TOKENS = {
    "access_token": "<jwt de acesso, 15 min>",  # nosec B105
    "refresh_token": "<jwt de refresh, 7 dias>",  # nosec B105
    "token_type": "bearer",  # nosec B105
}
_REFRESH = {"refresh_token": "{{refresh_token}}"}  # nosec B105
_CPF = {"documento": "529.982.247-25", "tipo_documento": "cpf"}
_VEICULO_NOVO = {"placa": "BRA2E19", "marca": "Fiat", "modelo": "Argo", "ano": 2022}
_VEICULO_RESPOSTA = {"id": _VEICULO, **_VEICULO_NOVO}
_CLIENTE_NOVO = {
    "nome": "Maria Souza",
    **_CPF,
    "contato": "maria.souza@example.com",
}
_CLIENTE_RESPOSTA = {
    "id": _CLIENTE,
    "nome": "Maria Souza",
    "documento_formatado": "529.982.247-25",
    "documento_mascarado": "***.***.***-25",
    "tipo_documento": "cpf",
    "contato": "maria.souza@example.com",
    "ativo": True,
    "veiculos": [_VEICULO_RESPOSTA],
}
_DADOS_PESSOAIS = {
    key: _CLIENTE_RESPOSTA[key]
    for key in (
        "id",
        "nome",
        "documento_formatado",
        "tipo_documento",
        "contato",
        "veiculos",
        "ativo",
    )
}
_DESCRICAO = "Barulho no freio dianteiro ao frear em descida"
_ORDEM_RESPOSTA = {
    "id": _ORDEM,
    "cliente_id": _CLIENTE,
    "veiculo_id": _VEICULO,
    "descricao_problema": _DESCRICAO,
    "status": "recebida",
    "situacao": "Recebida",
    "orcamento": None,
    "pagamento": None,
    "motivo_cancelamento": None,
    "versao": 1,
    "criado_em": "2026-10-18T13:00:00Z",
    "atualizado_em": "2026-10-18T13:00:00Z",
    "etapa": "aguardando_diagnostico",
}
_FINALIZADA = {
    **_ORDEM_RESPOSTA,
    "orcamento": {
        "orcamento_id": "c2a9e3f4-6b1d-4e8a-9f20-7d3c5b8a1e64",
        "total": "350.00",
        "moeda": "BRL",
        "link_decisao": "https://localhost/billing/api/v1/publico/orcamentos/<token>",
        "valido_ate": "2026-10-25T13:20:00Z",
    },
    "pagamento": {
        "pagamento_id": "5e8b1f2a-3c4d-4a6b-8e9f-0a1b2c3d4e5f",
        "status": "confirmado",
        "valor": "350.00",
        "moeda": "BRL",
        "checkout_url": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=<id>",
        "expira_em": "2026-10-19T14:00:00Z",
    },
}
_ABERTURA = {
    "seq": 1,
    "em": "2026-10-18T13:00:00Z",
    "de": None,
    "para": "aguardando_diagnostico",
    "gatilho": "abertura",
    "mensagem_id": None,
    "comando": "SolicitarDiagnostico",
    "comando_id": "be43f983-a6d9-4daf-a720-1a209659559f",
    "motivo": None,
    "ator": "<sub do atendente>",
}
_CANCELAMENTO_PEDIDO = {
    **_ABERTURA,
    "seq": 2,
    "em": "2026-10-18T13:05:00Z",
    "de": "aguardando_diagnostico",
    "para": "compensando",
    "gatilho": "cancelamento_solicitado",
    "comando": "DescartarDiagnostico",
    "comando_id": "a66d5f2e-4fe7-44a7-9ead-890342adfc5e",
    "motivo": "cancelamento",
}
_SAGA = {
    "ordem_id": _ORDEM,
    "etapa": "aguardando_diagnostico",
    "motivo": None,
    "falha": None,
    "plano_compensacao": [],
    "comando_em_voo": None,
    "reenvios": 0,
    "prazo_resposta_em": None,
    "passos": [_ABERTURA],
}
_MOTIVO = "Cliente vendeu o veiculo"


def _pagina(item: dict[str, Any]) -> dict[str, Any]:
    return {"items": [item], "total": 1, "offset": 0, "limit": 20}


# Variavel da colecao -> campo da resposta que o script de teste guarda nela (os
# nomes de campo, e nao credenciais, que o B105 toma por senha).
_GUARDA_TOKENS = {"token": "access_token", "refresh_token": "refresh_token"}  # nosec B105

# Ordem dos fluxos de docs/api/exemplos.md; as chaves sao as do OpenAPI (metodo,
# rota). O "guarda" de uma requisicao vira o script de teste dela (_script_de_teste).
EXEMPLOS: dict[tuple[str, str], dict[str, Any]] = {
    ("get", "/api/v1/saude"): {"status": 200, "resposta": {"status": "ok"}},
    ("get", "/api/v1/saude/pronto"): {"status": 200, "resposta": {"status": "ok"}},
    ("post", "/api/v1/autenticacao/login"): {
        "corpo": {"email": "admin@pytstop.dev", "senha": "admin-demo-os-2026"},
        "status": 200,
        "resposta": _TOKENS,
        "guarda": _GUARDA_TOKENS,
    },
    ("post", "/api/v1/autenticacao/refresh"): {
        "corpo": _REFRESH,
        "status": 200,
        "resposta": _TOKENS,
        "guarda": _GUARDA_TOKENS,
    },
    ("get", "/.well-known/jwks.json"): {
        "status": 200,
        "resposta": {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": "<thumbprint RFC 7638 da chave>",
                    "n": "<modulo da chave publica, base64url>",
                    "e": "AQAB",
                }
            ]
        },
    },
    ("post", "/api/v1/autenticacao/registrar"): {
        "corpo": {
            "email": "atendente@pytstop.dev",
            "senha": "atendente-demo-2026",
            "papel": "atendente",
        },
        "status": 201,
        "resposta": {
            "id": _ATENDENTE,
            "email": "atendente@pytstop.dev",
            "papel": "atendente",
        },
    },
    ("post", "/api/v1/clientes"): {
        "corpo": _CLIENTE_NOVO,
        "status": 201,
        "resposta": {**_CLIENTE_RESPOSTA, "veiculos": []},
        "guarda": {"cliente_id": "id"},
    },
    ("post", "/api/v1/clientes/{cliente_id}/consentimento"): {
        "corpo": {"tipo": "tratamento_dados"},
        "status": 201,
        "resposta": {
            "id": "f5ebdd3e-154c-42c8-84f7-d7c9f14aa771",
            "cliente_id": _CLIENTE,
            "tipo": "tratamento_dados",
            "concedido_em": "2026-10-18T12:55:00Z",
            "revogado_em": None,
            "ativo": True,
        },
    },
    ("post", "/api/v1/clientes/{cliente_id}/veiculos"): {
        "corpo": _VEICULO_NOVO,
        "status": 201,
        "resposta": _VEICULO_RESPOSTA,
        "guarda": {"veiculo_id": "id"},
    },
    ("get", "/api/v1/clientes"): {
        "query": {"offset": 0, "limit": 20},
        "status": 200,
        "resposta": _pagina(
            {
                key: _CLIENTE_RESPOSTA[key]
                for key in (
                    "id",
                    "nome",
                    "documento_mascarado",
                    "tipo_documento",
                    "contato",
                    "ativo",
                )
            }
        ),
    },
    ("get", "/api/v1/clientes/{cliente_id}"): {
        "status": 200,
        "resposta": _CLIENTE_RESPOSTA,
    },
    ("put", "/api/v1/clientes/{cliente_id}"): {
        "corpo": {"nome": "Maria Souza Lima", "contato": "maria.lima@example.com"},
        "status": 200,
        "resposta": {
            **_CLIENTE_RESPOSTA,
            "nome": "Maria Souza Lima",
            "contato": "maria.lima@example.com",
        },
    },
    ("get", "/api/v1/clientes/{cliente_id}/veiculos"): {
        "status": 200,
        "resposta": [_VEICULO_RESPOSTA],
    },
    ("get", "/api/v1/clientes/{cliente_id}/dados-pessoais"): {
        "status": 200,
        "resposta": _DADOS_PESSOAIS,
    },
    ("get", "/api/v1/clientes/{cliente_id}/dados-pessoais/exportar"): {
        "status": 200,
        "resposta": _DADOS_PESSOAIS,
    },
    ("post", "/api/v1/ordens-de-servico"): {
        "corpo": {
            "cliente_id": "{{cliente_id}}",
            "veiculo_id": "{{veiculo_id}}",
            "descricao_problema": _DESCRICAO,
        },
        "status": 201,
        "resposta": _ORDEM_RESPOSTA,
        "guarda": {"ordem_id": "id"},
    },
    ("get", "/api/v1/ordens-de-servico"): {
        "query": {"offset": 0, "limit": 20, "incluir_encerradas": "false"},
        "status": 200,
        "resposta": _pagina(
            {
                key: _ORDEM_RESPOSTA[key]
                for key in (
                    "id",
                    "cliente_id",
                    "veiculo_id",
                    "status",
                    "situacao",
                    "criado_em",
                    "atualizado_em",
                )
            }
        ),
    },
    ("get", "/api/v1/ordens-de-servico/{ordem_id}"): {
        "status": 200,
        "resposta": _ORDEM_RESPOSTA,
    },
    ("get", "/api/v1/ordens-de-servico/{ordem_id}/historico"): {
        "status": 200,
        "resposta": {
            "ordem_id": _ORDEM,
            "mudancas": [
                {
                    "sequencia": 1,
                    "de": None,
                    "para": "recebida",
                    "origem": "atendimento",
                    "motivo": None,
                    "ator": "<sub do atendente>",
                    "ocorrido_em": "2026-10-18T13:00:00Z",
                }
            ],
            "passos": [_ABERTURA],
        },
    },
    ("post", "/api/v1/ordens-de-servico/{ordem_id}/cancelamento"): {
        "corpo": {"motivo": _MOTIVO},
        "status": 202,
        "resposta": {
            **_ORDEM_RESPOSTA,
            "motivo_cancelamento": _MOTIVO,
            "versao": 2,
            "atualizado_em": "2026-10-18T13:05:00Z",
            "etapa": "compensando",
        },
    },
    ("post", "/api/v1/ordens-de-servico/{ordem_id}/entrega"): {
        "status": 200,
        "resposta": {
            **_FINALIZADA,
            "status": "entregue",
            "situacao": "Entregue",
            "versao": 9,
            "atualizado_em": "2026-10-19T17:00:00Z",
            "etapa": "concluida",
        },
    },
    ("get", "/api/v1/ordens-de-servico/metricas"): {
        "status": 200,
        "resposta": {
            "total": 3,
            "por_status": {
                "recebida": 0,
                "em_diagnostico": 1,
                "aguardando_aprovacao": 1,
                "aguardando_pagamento": 0,
                "aguardando_execucao": 0,
                "em_execucao": 0,
                "finalizada": 0,
                "entregue": 0,
                "cancelada": 1,
            },
            "tempo_medio_por_status_segundos": {
                "recebida": 150.0,
                "em_diagnostico": 41550.0,
                "aguardando_aprovacao": 600.0,
            },
        },
    },
    ("get", "/api/v1/sagas/{ordem_id}"): {"status": 200, "resposta": _SAGA},
    ("post", "/api/v1/sagas/{ordem_id}/compensacao"): {
        "status": 202,
        "resposta": {
            **_SAGA,
            "etapa": "compensando",
            "motivo": "cancelamento",
            "plano_compensacao": ["DescartarDiagnostico"],
            "comando_em_voo": {
                "tipo": "DescartarDiagnostico",
                "enviado_em": "2026-10-18T13:40:00Z",
            },
            "prazo_resposta_em": "2026-10-18T13:42:00Z",
            "passos": [
                _ABERTURA,
                _CANCELAMENTO_PEDIDO,
                {
                    **_CANCELAMENTO_PEDIDO,
                    "seq": 3,
                    "em": "2026-10-18T13:30:00Z",
                    "de": "compensando",
                    "para": "falha_na_compensacao",
                    "gatilho": "prazo_esgotado",
                    "comando": None,
                    "comando_id": None,
                    "ator": "prazos",
                },
                {
                    **_CANCELAMENTO_PEDIDO,
                    "seq": 4,
                    "em": "2026-10-18T13:40:00Z",
                    "de": "falha_na_compensacao",
                    "para": "compensando",
                    "gatilho": "retomada",
                    "comando_id": "0d5c7e9a-2b4f-4c6e-8a1b-3d5f7a9c1e2b",
                    "ator": "<sub do admin>",
                },
            ],
        },
    },
    ("post", "/api/v1/publico/acompanhamento"): {
        "corpo": {"placa": "BRA2E19", "documento": "529.982.247-25"},
        "status": 200,
        "resposta": {
            "status": "em_diagnostico",
            "situacao": "Em diagnóstico",
            "criado_em": "2026-10-18T13:00:00Z",
            "atualizado_em": "2026-10-18T13:20:00Z",
        },
    },
    ("delete", "/api/v1/clientes/{cliente_id}/consentimento"): {
        "query": {"tipo": "tratamento_dados"},
        "status": 204,
    },
    ("delete", "/api/v1/clientes/{cliente_id}/veiculos/{veiculo_id}"): {"status": 204},
    ("delete", "/api/v1/clientes/{cliente_id}/dados-pessoais"): {"status": 204},
    ("delete", "/api/v1/clientes/{cliente_id}"): {"status": 204},
    ("post", "/api/v1/autenticacao/logout"): {
        "corpo": _REFRESH,
        "status": 200,
        "resposta": {"mensagem": "Logout realizado com sucesso"},
    },
}


def _json(documento: object) -> str:
    return json.dumps(documento, indent=2, ensure_ascii=False) + "\n"


def _requisicao(
    metodo: str, rota: str, operacao: dict[str, Any], exemplo: dict[str, Any]
) -> dict[str, Any]:
    caminho = re.sub(r"\{(\w+)\}", r"{{\1}}", rota)
    query = exemplo.get("query", {})
    url: dict[str, Any] = {
        "raw": "{{baseUrl}}" + caminho + (f"?{urlencode(query)}" if query else ""),
        "host": ["{{baseUrl}}"],
        "path": caminho.strip("/").split("/"),
    }
    if query:
        url["query"] = [
            {"key": chave, "value": str(valor)} for chave, valor in query.items()
        ]
    pedido: dict[str, Any] = {
        "method": metodo.upper(),
        "header": [],
        "url": url,
        "description": operacao.get("description", operacao["summary"]),
    }
    # Rota sem seguranca no OpenAPI (sondas, login, refresh, JWKS, acompanhamento)
    # nao leva o token da colecao.
    if "security" not in operacao:
        pedido["auth"] = {"type": "noauth"}
    if "corpo" in exemplo:
        pedido["header"] = [{"key": "Content-Type", "value": "application/json"}]
        pedido["body"] = {
            "mode": "raw",
            "raw": _json(exemplo["corpo"]).rstrip("\n"),
            "options": {"raw": {"language": "json"}},
        }
    status = HTTPStatus(exemplo["status"])
    resposta: dict[str, Any] = {
        "name": f"{status.value} {status.phrase}",
        "originalRequest": pedido,
        "status": status.phrase,
        "code": status.value,
        "header": [],
        "body": "",
    }
    if "resposta" in exemplo:
        resposta["header"] = [{"key": "Content-Type", "value": "application/json"}]
        resposta["body"] = _json(exemplo["resposta"]).rstrip("\n")
        resposta["_postman_previewlanguage"] = "json"
    item: dict[str, Any] = {
        "name": operacao["summary"],
        "request": pedido,
        "response": [resposta],
    }
    if "guarda" in exemplo:
        item["event"] = [_script_de_teste(status.value, exemplo["guarda"])]
    return item


def _script_de_teste(status: int, guarda: dict[str, str]) -> dict[str, Any]:
    """Guarda campos da resposta em variaveis da colecao, uma linha por variavel."""
    return {
        "listen": "test",
        "script": {
            "type": "text/javascript",
            "exec": [
                f"if (pm.response.code === {status}) pm.collectionVariables.set("
                f'"{variavel}", pm.response.json().{campo});'
                for variavel, campo in guarda.items()
            ],
        },
    }


def colecao(openapi: dict[str, Any]) -> dict[str, Any]:
    """Colecao Postman v2.1 do OpenAPI: uma pasta por tag, uma requisicao por rota.

    Raises:
        ValueError: ``EXEMPLOS`` nao cobre exatamente os pares do OpenAPI.
    """
    pares = {(metodo, rota) for rota, ops in openapi["paths"].items() for metodo in ops}
    if pares != EXEMPLOS.keys():
        faltam = sorted(pares - EXEMPLOS.keys())
        sobram = sorted(EXEMPLOS.keys() - pares)
        msg = f"EXEMPLOS fora do OpenAPI: faltam {faltam}, sobram {sobram}"
        raise ValueError(msg)
    pastas: dict[str, list[dict[str, Any]]] = {}
    for (metodo, rota), exemplo in EXEMPLOS.items():
        operacao = openapi["paths"][rota][metodo]
        pastas.setdefault(operacao["tags"][0], []).append(
            _requisicao(metodo, rota, operacao, exemplo)
        )
    return {
        "info": {
            "name": openapi["info"]["title"],
            "description": DESCRICAO,
            "schema": ESQUEMA_POSTMAN,
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
        },
        "variable": [
            {"key": chave, "value": valor} for chave, valor in VARIAVEIS.items()
        ],
        "item": [{"name": nome, "item": itens} for nome, itens in pastas.items()],
    }


def main() -> int:
    # O import monta o app (src.main o cria no import); os testes so usam colecao().
    from src.main import app

    documento = app.openapi()
    try:
        postman = colecao(documento)
    except ValueError as erro:
        print(erro, file=sys.stderr)
        return 1
    DOCS_API.mkdir(parents=True, exist_ok=True)
    OPENAPI.write_text(_json(documento), encoding="utf-8")
    COLECAO.write_text(_json(postman), encoding="utf-8")
    print(f"atualizados: {OPENAPI.relative_to(RAIZ)} e {COLECAO.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
