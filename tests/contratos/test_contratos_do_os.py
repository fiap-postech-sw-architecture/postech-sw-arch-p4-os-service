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


# Um campo de cada formato que o `$` do pattern deixava aceitar com quebra de
# linha no fim: o envelope, as datas e horas, o dinheiro, a URL, o codigo dos
# itens, o SKU da reserva e a placa.
_COM_QUEBRA_DE_LINHA = [
    ("SolicitarDiagnostico", ("ocorrido_em",)),
    ("SolicitarDiagnostico", ("dados", "veiculo", "placa")),
    ("DiagnosticoIniciado", ("dados", "iniciado_em")),
    ("DiagnosticoConcluido", ("dados", "concluido_em")),
    ("DiagnosticoConcluido", ("dados", "itens", 0, "codigo")),
    ("GerarOrcamento", ("dados", "itens", 0, "codigo")),
    ("GeracaoDeOrcamentoFalhou", ("dados", "codigos_invalidos", 0)),
    ("OrcamentoGerado", ("dados", "total")),
    ("OrcamentoGerado", ("dados", "valido_ate")),
    ("OrcamentoGerado", ("dados", "link_decisao")),
    ("OrcamentoGerado", ("dados", "linhas", 0, "codigo")),
    ("OrcamentoAprovado", ("dados", "decidido_em")),
    ("OrcamentoRecusado", ("dados", "decidido_em")),
    ("PagamentoSolicitado", ("dados", "valor")),
    ("PagamentoSolicitado", ("dados", "expira_em")),
    ("PagamentoSolicitado", ("dados", "checkout_url")),
    ("PagamentoConfirmado", ("dados", "valor")),
    ("PagamentoConfirmado", ("dados", "confirmado_em")),
    ("PagamentoCancelado", ("dados", "cancelado_em")),
    ("PagamentoEstornado", ("dados", "estornado_em")),
    ("ReservarPecas", ("dados", "pecas", 0, "sku")),
    ("ReservaDePecasFalhou", ("dados", "faltantes", 0, "sku")),
    ("ExecucaoIniciada", ("dados", "iniciada_em")),
    ("ExecucaoFinalizada", ("dados", "finalizada_em")),
    ("ExecucaoFinalizada", ("dados", "pecas_consumidas", 0, "sku")),
]


def _alvo(exemplo: dict[str, Any], caminho: tuple[str | int, ...]) -> Any:
    no: Any = exemplo
    for passo in caminho[:-1]:
        no = no[passo]
    return no


@pytest.mark.parametrize(
    ("tipo", "caminho"),
    _COM_QUEBRA_DE_LINHA,
    ids=[
        f"{tipo}.{'.'.join(map(str, caminho))}"
        for tipo, caminho in _COM_QUEBRA_DE_LINHA
    ],
)
def test_quebra_de_linha_no_fim_do_valor_e_recusada(
    tipo: str, caminho: tuple[str | int, ...]
) -> None:
    """O ``$`` do pattern aceita ``\\n`` no fim e o ``format`` do jsonschema
    tambem: os patterns terminam em ``(?![\\s\\S])``. A recusa tem de ser do
    campo, e nao de outro erro qualquer do exemplo."""
    exemplo = _exemplo(tipo)
    _alvo(exemplo, caminho)[caminho[-1]] += "\n"
    esperado = "$" + "".join(
        f"[{p}]" if isinstance(p, int) else f".{p}" for p in caminho
    )

    with pytest.raises(ContratoInvalidoError) as erro:
        catalogo().validar(exemplo)

    assert (erro.value.caminho, erro.value.regra) == (esperado, "pattern")


def _patterns(no: Any) -> list[str]:
    """Todo ``pattern`` do schema, em qualquer profundidade."""
    if isinstance(no, dict):
        achados = [no["pattern"]] if isinstance(no.get("pattern"), str) else []
        return achados + [p for valor in no.values() for p in _patterns(valor)]
    if isinstance(no, list):
        return [p for valor in no for p in _patterns(valor)]
    return []


def test_todo_pattern_dos_schemas_termina_em_fim_de_texto_sem_quebra_de_linha() -> None:
    """O ``$`` aceita ``\\n`` no fim do valor; ``(?![\\s\\S])`` so casa no fim do
    texto. Cobre os ``pattern`` que os exemplos acima nao exercitam (o tipo do
    envelope, por exemplo)."""
    patterns = [
        p
        for schema in sorted((CONTRATOS / "schemas").glob("*.schema.json"))
        for p in _patterns(json.loads(schema.read_text(encoding="utf-8")))
    ]

    assert len(patterns) >= 26, "nenhum pattern lido: o teste nao prova nada"
    assert [p for p in patterns if not p.endswith(r"(?![\s\S])")] == []


# O codigo dos itens do diagnostico e do GerarOrcamento aceita mais que o SKU do
# ReservarPecas (so o formato do Billing: maiusculas e digitos separados por
# hifens, ate 50 caracteres). O OS monta o ReservarPecas com os codigos do
# diagnostico, entao o comando que ele grava pode ser recusado pelo contrato.
_FORA_DO_FORMATO_DO_BILLING = [
    "pec_vela.2",
    "pec-vela",
    "PEC_VELA",
    "PEC.VELA",
    "PEC--VELA",
    "PEC-VELA-",
]


@pytest.mark.parametrize("codigo", _FORA_DO_FORMATO_DO_BILLING)
def test_codigo_fora_do_formato_do_billing_passa_no_diagnostico_e_no_orcamento(
    codigo: str,
) -> None:
    diagnostico = _exemplo("DiagnosticoConcluido")
    diagnostico["dados"]["itens"][1]["codigo"] = codigo
    gerar = _exemplo("GerarOrcamento")
    gerar["dados"]["itens"][1]["codigo"] = codigo

    catalogo().validar(diagnostico)
    catalogo().validar(gerar)


@pytest.mark.parametrize("sku", _FORA_DO_FORMATO_DO_BILLING)
def test_sku_fora_do_formato_do_billing_e_recusado_ao_montar_o_reservar_pecas(
    sku: str,
) -> None:
    from datetime import UTC, datetime

    ordem_id = uuid4()

    with pytest.raises(ContratoInvalidoError) as erro:
        catalogo().montar_envelope(
            "ReservarPecas",
            {"ordem_id": ordem_id, "pecas": [{"sku": sku, "quantidade": 1}]},
            correlation_id=ordem_id,
            causation_id=None,
            ocorrido_em=datetime.now(UTC),
        )

    assert (erro.value.caminho, erro.value.regra) == (
        "$.dados.pecas[0].sku",
        "pattern",
    )


@pytest.mark.parametrize("sku", ["PEC-OLEO-5W30", "A", "9", "A" * 50])
def test_sku_no_formato_do_billing_e_aceito_ate_50_caracteres(sku: str) -> None:
    from datetime import UTC, datetime

    ordem_id = uuid4()

    envelope = catalogo().montar_envelope(
        "ReservarPecas",
        {"ordem_id": ordem_id, "pecas": [{"sku": sku, "quantidade": 1}]},
        correlation_id=ordem_id,
        causation_id=None,
        ocorrido_em=datetime.now(UTC),
    )

    assert envelope["dados"]["pecas"] == [{"sku": sku, "quantidade": 1}]


def test_sku_de_51_caracteres_e_recusado() -> None:
    from datetime import UTC, datetime

    ordem_id = uuid4()

    with pytest.raises(ContratoInvalidoError) as erro:
        catalogo().montar_envelope(
            "ReservarPecas",
            {"ordem_id": ordem_id, "pecas": [{"sku": "A" * 51, "quantidade": 1}]},
            correlation_id=ordem_id,
            causation_id=None,
            ocorrido_em=datetime.now(UTC),
        )

    assert erro.value.regra == "maxLength"


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
