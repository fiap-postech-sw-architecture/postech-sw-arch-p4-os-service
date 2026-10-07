"""O que o OS produz e consome esta no contrato copiado, e a topologia o atende.

O catalogo sai do ``asyncapi.yaml``; aqui ele e conferido contra a RFC-004
secao 5.3 (o OS so publica comandos e consome os eventos de Billing e
Execucao), contra os exemplos do platform e contra a topologia do RabbitMQ que
o relay e o consumidor usam.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any
from uuid import uuid4

import pytest

from src.compartilhado.aplicacao.mensageria import ContratoInvalidoError
from src.compartilhado.infraestrutura.mensageria.consumidor import NIVEIS_DE_RETRY
from src.compartilhado.infraestrutura.mensageria.contratos import (
    CONTRATOS,
    FILA,
    catalogo,
)

_COMANDOS = {
    "SolicitarDiagnostico": "execucao",
    "DescartarDiagnostico": "execucao",
    "ReservarPecas": "execucao",
    "LiberarReserva": "execucao",
    "AgendarExecucao": "execucao",
    "CancelarExecucao": "execucao",
    "AnonimizarVeiculo": "execucao",
    "GerarOrcamento": "billing",
    "CancelarOrcamento": "billing",
    "SolicitarPagamento": "billing",
    "EstornarPagamento": "billing",
}
_RABBITMQ = CONTRATOS / "rabbitmq"


def _exemplo(tipo: str) -> dict[str, Any]:
    exemplo: dict[str, Any] = json.loads(
        (CONTRATOS / "exemplos" / f"{tipo}.json").read_text()
    )
    return exemplo


def _snake(nome: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", nome).lower()


def test_o_os_publica_so_os_comandos_do_catalogo() -> None:
    contratos = catalogo()

    assert contratos.publicados == set(_COMANDOS)
    for tipo, destino in _COMANDOS.items():
        assert contratos.produtor(tipo) == "os"
        rota = contratos.destino(tipo)
        assert rota.exchange == "pytstop.comandos"
        assert rota.routing_key == f"comando.{destino}.{_snake(tipo)}"


def test_o_os_consome_os_eventos_de_billing_e_execucao() -> None:
    contratos = catalogo()

    assert len(contratos.consumidos) == 23
    assert not contratos.consumidos & contratos.publicados
    for tipo in contratos.consumidos:
        assert contratos.produtor(tipo) in {"billing", "execucao"}


@pytest.mark.parametrize("tipo", sorted(set(_COMANDOS) | catalogo().consumidos))
def test_todo_tipo_produzido_ou_consumido_tem_schema_e_o_exemplo_valida(
    tipo: str,
) -> None:
    assert (CONTRATOS / "schemas" / f"{tipo}.schema.json").is_file()
    catalogo().validar(_exemplo(tipo))


def test_leitor_tolerante_aceita_campo_novo_em_qualquer_nivel() -> None:
    exemplo = _exemplo("OrcamentoGerado")
    exemplo["campo_novo"] = 1
    exemplo["dados"]["campo_novo"] = "x"
    exemplo["dados"]["linhas"][0]["campo_novo"] = True

    catalogo().validar(exemplo)


@pytest.mark.parametrize(
    ("mudanca", "caminho", "regra"),
    [
        pytest.param({"versao": 2}, "$.versao", "const", id="versao-desconhecida"),
        pytest.param(
            {"origem": "billing-service"}, "$.origem", "const", id="origem-errada"
        ),
        pytest.param(
            {"ocorrido_em": "2026-13-45T12:00:00Z"},
            "$.ocorrido_em",
            "format",
            id="data-invalida",
        ),
        pytest.param(
            {"correlation_id": "123"}, "$.correlation_id", "format", id="uuid-invalido"
        ),
    ],
)
def test_envelope_fora_do_contrato_diz_onde_sem_o_valor(
    mudanca: dict[str, Any], caminho: str, regra: str
) -> None:
    envelope = {**_exemplo("SolicitarDiagnostico"), **mudanca}
    contratos = catalogo()

    with pytest.raises(ContratoInvalidoError) as erro:
        contratos.validar(envelope)

    assert (erro.value.caminho, erro.value.regra) == (caminho, regra)
    for valor in mudanca.values():
        assert str(valor) not in str(erro.value)


def test_placa_invalida_nao_aparece_no_erro() -> None:
    envelope = _exemplo("SolicitarDiagnostico")
    envelope["dados"]["veiculo"]["placa"] = "ABC-1234"
    contratos = catalogo()

    with pytest.raises(ContratoInvalidoError) as erro:
        contratos.validar(envelope)

    assert erro.value.caminho == "$.dados.veiculo.placa"
    assert "ABC-1234" not in str(erro.value)


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(None, id="nulo"),
        pytest.param([], id="lista"),
        pytest.param("x", id="texto"),
        pytest.param({"tipo": "Inexistente"}, id="tipo-fora-do-catalogo"),
    ],
)
def test_tipo_desconhecido_reprova(envelope: object) -> None:
    contratos = catalogo()

    with pytest.raises(ContratoInvalidoError, match="tipo desconhecido"):
        contratos.validar(envelope)


class _TipoItem(StrEnum):
    SERVICO = "servico"
    PECA = "peca"


def test_envelope_do_comando_converte_uuid_e_enum_e_sai_valido() -> None:
    from datetime import UTC, datetime

    ordem_id = uuid4()
    envelope = catalogo().montar_envelope(
        "GerarOrcamento",
        {
            "ordem_id": ordem_id,
            "itens": (
                {"tipo": _TipoItem.SERVICO, "codigo": "SRV-01", "quantidade": 1},
                {"tipo": _TipoItem.PECA, "codigo": "PEC-01", "quantidade": 4},
            ),
        },
        correlation_id=ordem_id,
        causation_id=None,
        ocorrido_em=datetime.now(UTC),
    )

    assert envelope["dados"] == {
        "ordem_id": str(ordem_id),
        "itens": [
            {"tipo": "servico", "codigo": "SRV-01", "quantidade": 1},
            {"tipo": "peca", "codigo": "PEC-01", "quantidade": 4},
        ],
    }
    assert (envelope["versao"], envelope["origem"]) == (1, "os-service")
    assert envelope["causation_id"] is None


def test_o_os_nao_monta_envelope_de_evento() -> None:
    from datetime import UTC, datetime

    contratos = catalogo()
    dados = _exemplo("PecasReservadas")["dados"]
    correlation_id = uuid4()
    agora = datetime.now(UTC)

    with pytest.raises(ContratoInvalidoError, match="nao publica"):
        contratos.montar_envelope(
            "PecasReservadas",
            dados,
            correlation_id=correlation_id,
            causation_id=None,
            ocorrido_em=agora,
        )


def test_cada_nivel_de_retry_do_consumidor_tem_fila_ttl_e_permissao() -> None:
    # O consumidor publica a copia em pytstop.retry com a routing key do nivel;
    # a topologia tem de rotea-la para uma fila com esse TTL, que devolve a
    # mensagem para os.eventos, e o usuario `os` tem de poder usar a chave.
    definitions = json.loads((_RABBITMQ / "definitions.json").read_text())
    permissoes = json.loads((_RABBITMQ / "permissoes.json").read_text())
    filas = {fila["name"]: fila for fila in definitions["queues"]}
    (escrita,) = [
        p["write"]
        for p in permissoes["topic_permissions"]
        if (p["user"], p["exchange"]) == ("os", "pytstop.retry")
    ]
    (politica,) = [
        p
        for p in definitions["policies"]
        if re.search(p["pattern"], f"{FILA}.retry.1s")
    ]

    for nivel in NIVEIS_DE_RETRY:
        nome = f"{FILA}.retry.{nivel}"
        ttl_ms = int(nivel.removesuffix("s")) * 1000
        assert filas[nome]["arguments"] == {
            "x-queue-type": "quorum",
            "x-message-ttl": ttl_ms,
        }
        assert {
            "source": "pytstop.retry",
            "vhost": "/",
            "destination": nome,
            "destination_type": "queue",
            "routing_key": nome,
            "arguments": {},
        } in definitions["bindings"]
        assert re.search(escrita, nome)
        assert re.search(politica["pattern"], nome)
    assert politica["definition"]["dead-letter-exchange"] == ""
    assert politica["definition"]["dead-letter-routing-key"] == FILA
    assert not re.search(escrita, FILA)
