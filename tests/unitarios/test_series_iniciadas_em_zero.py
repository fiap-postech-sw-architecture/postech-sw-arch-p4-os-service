"""O que cada processo expoe no ``/metrics`` logo depois do boot, sem trafego.

Toda serie de rotulo fechado nasce com zero: sem isso o ``increase()`` dos
paineis perde o primeiro evento de cada serie. Cada teste roda o boot num
interpretador novo (``tests/subprocesso.py``), porque o registro do
``prometheus_client`` e global ao processo e as series de outros testes o sujam.
"""

from __future__ import annotations

import math

import pytest
from prometheus_client.parser import text_string_to_metric_families

from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.ordem_servico.aplicacao.saga.modelo import MotivoCompensacao
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    COMANDOS_COM_PRAZO,
    ETAPAS_FINAIS,
    ETAPAS_NAO_FINAIS,
)
from tests.subprocesso import executar_python

# Escritos a mao, de proposito: o oraculo nao e o proprio codigo.
RESULTADOS = ("processada", "duplicada", "ignorada", "retry", "dlq")

type Serie = tuple[str, tuple[tuple[str, str], ...]]

_BOOT_DO_RELAY = """
import pathlib, tempfile
import pika
from prometheus_client import generate_latest
from sqlalchemy import create_engine
import src.relay  # o que o processo importa
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay

Relay(
    engine=create_engine("postgresql://u:p@127.0.0.1:1/db"),
    parametros=pika.URLParameters("amqp://os:x@127.0.0.1:1/%2F"),
    tracer=None,
    config=ConfigRelay(diretorio_de_saude=pathlib.Path(tempfile.mkdtemp())),
)
print(generate_latest().decode())
"""

_BOOT_DO_CONSUMIDOR = """
import pathlib, tempfile
import pika
from prometheus_client import generate_latest
import src.consumidor  # o que o processo importa (a saga e as metricas dela)
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConfigConsumidor,
    Consumidor,
)

Consumidor(
    session_factory=None,
    parametros=pika.URLParameters("amqp://os:x@127.0.0.1:1/%2F"),
    despachante={},
    tracer=None,
    config=ConfigConsumidor(diretorio_de_saude=pathlib.Path(tempfile.mkdtemp())),
)
print(generate_latest().decode())
"""

_BOOT_DA_API = """
from prometheus_client import generate_latest
import src.main

print(generate_latest().decode())
"""

_BOOT_DO_PRAZOS = """
from prometheus_client import generate_latest
import src.prazos

print(generate_latest().decode())
"""


# O boot de quem nao serve HTTP: so importa o modulo do processo.
_BOOT_SEM_HTTP = """
import sys
from prometheus_client import generate_latest
import src.{processo}

print("starlette" in sys.modules)
print(generate_latest().decode())
"""


def _series(texto: str) -> dict[Serie, float]:
    return {
        (amostra.name, tuple(sorted(amostra.labels.items()))): amostra.value
        for familia in text_string_to_metric_families(texto)
        for amostra in familia.samples
    }


def _rotulos(series: dict[Serie, float], nome: str, rotulo: str) -> set[str]:
    return {dict(r)[rotulo] for (n, r) in series if n == nome}


def _familias(texto: str) -> set[str]:
    return {familia.name for familia in text_string_to_metric_families(texto)}


@pytest.fixture(scope="module")
def relay() -> dict[Serie, float]:
    return _series(executar_python(_BOOT_DO_RELAY))


@pytest.fixture(scope="module")
def consumidor() -> dict[Serie, float]:
    return _series(executar_python(_BOOT_DO_CONSUMIDOR))


@pytest.fixture(scope="module")
def api() -> str:
    return executar_python(_BOOT_DA_API)


@pytest.fixture(scope="module")
def prazos() -> str:
    return executar_python(_BOOT_DO_PRAZOS)


class TestRelay:
    def test_cada_tipo_publicado_nasce_com_zero(
        self, relay: dict[Serie, float]
    ) -> None:
        nome = "pytstop_mensagens_publicadas_total"
        # Os tipos do OS no catalogo, e so eles: nenhum de outro produtor.
        assert _rotulos(relay, nome, "tipo") == set(catalogo().publicados)
        assert {v for (n, _), v in relay.items() if n == nome} == {0.0}

    def test_reconexoes_nascem_com_zero_so_no_componente_do_processo(
        self, relay: dict[Serie, float]
    ) -> None:
        nome = "pytstop_reconexoes_ao_broker_total"
        assert _rotulos(relay, nome, "componente") == {"relay"}
        assert relay[(nome, (("componente", "relay"),))] == 0.0

    def test_gauges_da_outbox_existem_e_sem_o_banco_valem_nan(
        self, relay: dict[Serie, float]
    ) -> None:
        # NaN e o banco fora do alcance dele; o numero e o banco de pe.
        assert math.isnan(relay[("outbox_pendentes", ())])
        assert math.isnan(relay[("outbox_dead", ())])

    def test_relay_nao_expoe_o_que_e_da_api_nem_do_consumidor(
        self, relay: dict[Serie, float]
    ) -> None:
        nomes = {n for (n, _) in relay}
        assert not {n for n in nomes if n.startswith("pytstop_saga_")}
        assert "pytstop_mensagens_consumidas_total" not in nomes
        assert "pytstop_outbox_mais_antiga_segundos" not in nomes


class TestConsumidor:
    def test_cada_tipo_e_resultado_nasce_com_zero(
        self, consumidor: dict[Serie, float]
    ) -> None:
        nome = "pytstop_mensagens_consumidas_total"
        esperado = {
            (tipo, resultado)
            for tipo in catalogo().consumidos | {"desconhecido"}
            for resultado in RESULTADOS
        }
        observado = {
            (dict(r)["tipo"], dict(r)["resultado"])
            for (n, r) in consumidor
            if n == nome
        }
        assert observado == esperado
        assert {v for (n, _), v in consumidor.items() if n == nome} == {0.0}

    def test_reconexoes_nascem_com_zero_so_no_componente_do_processo(
        self, consumidor: dict[Serie, float]
    ) -> None:
        nome = "pytstop_reconexoes_ao_broker_total"
        assert _rotulos(consumidor, nome, "componente") == {"consumidor"}
        assert consumidor[(nome, (("componente", "consumidor"),))] == 0.0

    def test_series_da_saga_que_o_consumidor_emite_nascem_com_zero(
        self, consumidor: dict[Serie, float]
    ) -> None:
        finalizadas = "pytstop_saga_finalizadas_total"
        assert _rotulos(consumidor, finalizadas, "resultado") == {
            e.value for e in ETAPAS_FINAIS
        }
        compensacoes = "pytstop_saga_compensacoes_total"
        assert _rotulos(consumidor, compensacoes, "motivo") == {
            m.value for m in MotivoCompensacao
        }
        duracao = "pytstop_saga_etapa_duracao_segundos_count"
        assert _rotulos(consumidor, duracao, "etapa") == {
            e.value for e in ETAPAS_NAO_FINAIS
        }
        assert consumidor[("pytstop_saga_iniciadas_total", ())] == 0.0


class TestPrazos:
    def test_reenvios_e_prazos_esgotados_nascem_com_zero_por_comando(
        self, prazos: str
    ) -> None:
        series = _series(prazos)
        comandos = {c.value for c in COMANDOS_COM_PRAZO}
        for nome in (
            "pytstop_saga_reenvios_total",
            "pytstop_saga_prazos_esgotados_total",
        ):
            assert _rotulos(series, nome, "comando") == comandos
            assert {v for (n, _), v in series.items() if n == nome} == {0.0}

    def test_ultimo_ciclo_existe_antes_do_primeiro_ciclo(self, prazos: str) -> None:
        series = _series(prazos)

        assert series[("pytstop_prazos_ultimo_ciclo_timestamp_seconds", ())] == 0.0


class TestProcessosSemHttp:
    @pytest.mark.parametrize("processo", ["relay", "consumidor", "prazos"])
    def test_nao_carrega_o_starlette_nem_expoe_a_metrica_http(
        self, processo: str
    ) -> None:
        # O rotulo da rota que o span usa mora num modulo proprio: importar o
        # `observability` nao arrasta o middleware nem registra o histograma HTTP
        # que so a API serve.
        carregou, _, texto = executar_python(
            _BOOT_SEM_HTTP.format(processo=processo)
        ).partition("\n")

        assert carregou == "False"
        assert "http_request_duration_seconds" not in _familias(texto)


class TestApi:
    def test_contadores_e_histogramas_da_api_existem(self, api: str) -> None:
        series = _series(api)

        assert series[("pytstop_os_criadas_total", ())] == 0.0
        assert series[("pytstop_saga_iniciadas_total", ())] == 0.0
        assert "http_request_duration_seconds" in _familias(api)

    def test_a_api_nao_expoe_o_que_e_dos_outros_processos(self, api: str) -> None:
        nomes = {n for (n, _) in _series(api)}
        assert "pytstop_mensagens_publicadas_total" not in nomes
        assert "pytstop_mensagens_consumidas_total" not in nomes
        assert "pytstop_prazos_ultimo_ciclo_timestamp_seconds" not in nomes
        assert "outbox_pendentes" not in nomes

    def test_a_metrica_de_duracao_por_status_saiu(self, api: str) -> None:
        # Substituida pelo gauge do coletor (RF-027): o histograma so media as
        # transicoes feitas pela API e nao reproduzia a conta do historico.
        assert not [
            n for (n, _) in _series(api) if n.startswith("pytstop_os_duracao_status")
        ]
