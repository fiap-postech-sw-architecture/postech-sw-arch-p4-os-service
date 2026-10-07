"""Contratos de mensageria copiados do platform (RFC-004 secao 5.5, ADR-036).

``contratos/`` na raiz do repositorio e copia do ``contratos/`` do platform no
SHA de ``contratos/ORIGEM`` (um teste confere o checksum). O catalogo sai do
``asyncapi.yaml``: quem produz cada tipo (o ``userId`` da operacao de envio),
para onde ele vai (exchange e routing key do canal) e o que a fila do OS recebe.
A validacao usa o payload da mensagem no AsyncAPI, que junta o envelope, as
constantes ``tipo``, ``versao`` e ``origem`` e o schema de ``dados``. Os schemas
nao proibem campo extra (leitor tolerante); ``versao`` desconhecida reprova pela
constante.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID, uuid4

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import best_match
from referencing import Registry, Resource

from src.compartilhado.aplicacao.mensageria import ContratoInvalidoError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

CONTRATOS: Final = Path(__file__).resolve().parents[4] / "contratos"
_ASYNCAPI: Final = CONTRATOS / "asyncapi.yaml"

# Identidade do servico nos contratos: `origem` do envelope e `userId` das
# operacoes de envio do AsyncAPI.
ORIGEM: Final = "os-service"
USUARIO: Final = "os"
FILA: Final = "os.eventos"
VERSAO: Final = 1


@dataclass(frozen=True, slots=True)
class Destino:
    exchange: str
    routing_key: str


class Catalogo:
    """Tipos, produtores, destinos e validadores lidos do AsyncAPI."""

    def __init__(self, asyncapi: dict[str, Any]) -> None:
        self._produtores: dict[str, str] = {}
        self._destinos: dict[str, Destino] = {}
        consumidos: list[str] = []
        for operacao in asyncapi["operations"].values():
            canal = _resolver(asyncapi, operacao["channel"]["$ref"])
            if operacao["action"] == "send":
                (tipo,) = canal["messages"]
                self._produtores[tipo] = operacao["bindings"]["amqp"]["userId"]
                self._destinos[tipo] = Destino(
                    exchange=canal["bindings"]["amqp"]["exchange"]["name"],
                    routing_key=canal["address"],
                )
            elif canal["address"] == FILA:
                consumidos = [
                    m["$ref"].rsplit("/", 1)[-1] for m in operacao["messages"]
                ]
        self.consumidos: frozenset[str] = frozenset(consumidos)
        self.publicados: frozenset[str] = frozenset(
            tipo for tipo, usuario in self._produtores.items() if usuario == USUARIO
        )
        registro: Registry[Any] = Registry().with_resources(
            (p.as_uri(), Resource.from_contents(json.loads(p.read_text("utf-8"))))
            for p in (CONTRATOS / "schemas").glob("*.schema.json")
        )
        mensagens = asyncapi["components"]["messages"]
        # $id = URI do asyncapi.yaml: os $ref relativos (./schemas/...) caem
        # nos arquivos do registro.
        self._validadores = {
            tipo: Draft202012Validator(
                {"$id": _ASYNCAPI.as_uri(), **mensagem["payload"]["schema"]},
                registry=registro,
                format_checker=FormatChecker(),
            )
            for tipo, mensagem in mensagens.items()
        }

    def produtor(self, tipo: str) -> str | None:
        """Usuario do RabbitMQ que publica o ``tipo`` (``None`` se desconhecido)."""
        return self._produtores.get(tipo)

    def destino(self, tipo: str) -> Destino:
        """Exchange e routing key de um comando que o OS publica.

        Raises:
            ContratoInvalidoError: o OS nao publica esse ``tipo``.
        """
        if tipo not in self.publicados:
            msg = "tipo que o OS Service nao publica"
            raise ContratoInvalidoError(msg)
        return self._destinos[tipo]

    def validar(self, envelope: object) -> None:
        """Confere o envelope inteiro contra o payload do ``tipo`` no AsyncAPI.

        Raises:
            ContratoInvalidoError: tipo desconhecido ou envelope fora do schema.
        """
        tipo = envelope.get("tipo") if isinstance(envelope, dict) else None
        validador = self._validadores.get(tipo) if isinstance(tipo, str) else None
        if validador is None:
            msg = "tipo desconhecido"
            raise ContratoInvalidoError(msg)
        erro = best_match(validador.iter_errors(envelope))
        if erro is not None:
            msg = "envelope fora do contrato"
            raise ContratoInvalidoError(
                msg, caminho=erro.json_path, regra=str(erro.validator)
            )

    def montar_envelope(
        self,
        tipo: str,
        dados: Mapping[str, Any],
        *,
        correlation_id: UUID,
        causation_id: UUID | None,
        ocorrido_em: datetime,
    ) -> dict[str, Any]:
        """Envelope de um comando do OS (RFC-004 secao 5.2), ja validado.

        Raises:
            ContratoInvalidoError: o OS nao publica o ``tipo`` ou ``dados``
                fora do schema (bug de quem chama).
        """
        self.destino(tipo)
        envelope = {
            "id": str(uuid4()),
            # O Comando e StrEnum: no JSON vai so o texto.
            "tipo": str(tipo),
            "versao": VERSAO,
            "origem": ORIGEM,
            "correlation_id": str(correlation_id),
            "causation_id": str(causation_id) if causation_id is not None else None,
            "ocorrido_em": ocorrido_em.isoformat(),
            "dados": _json(dados),
        }
        self.validar(envelope)
        return envelope


@cache
def catalogo() -> Catalogo:
    """Catalogo do ``contratos/asyncapi.yaml`` (lido uma vez por processo)."""
    return Catalogo(yaml.safe_load(_ASYNCAPI.read_text("utf-8")))


def _resolver(documento: dict[str, Any], ref: str) -> Any:  # noqa: ANN401  # no do YAML
    no: Any = documento
    for parte in ref.removeprefix("#/").split("/"):
        no = no[parte.replace("~1", "/").replace("~0", "~")]
    return no


def _json(valor: Any) -> Any:  # noqa: ANN401  # estrutura JSON heterogenea
    """Converte UUID e Enum, os tipos dos ``dados`` dos comandos, para JSON.

    Nenhum comando do OS leva data nem dinheiro (RFC-004 secao 5.3); o que nao
    for JSON chega ao schema como esta e reprova la.
    """
    if isinstance(valor, dict):
        return {str(chave): _json(item) for chave, item in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_json(item) for item in valor]
    if isinstance(valor, Enum):
        return _json(valor.value)
    if isinstance(valor, UUID):
        return str(valor)
    return valor
