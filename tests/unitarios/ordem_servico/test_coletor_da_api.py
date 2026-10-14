"""A mecanica do coletor da API, sem banco: prazo, uma leitura por vez e registro.

O que o coletor le do PostgreSQL esta em ``tests/integracao/ordem_servico/``; aqui
a leitura e trocada por uma que o teste prende, solta ou faz falhar, porque o que
se prova e que a raspagem do ``/metrics`` nunca pendura.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest
import structlog
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from src.ordem_servico.aplicacao.dtos import MetricasDasOrdens
from src.ordem_servico.infraestrutura import coletor_da_api
from src.ordem_servico.infraestrutura.coletor_da_api import (
    ColetorDaApi,
    desregistrar_coletor,
    registrar_coletor,
)
from src.ordem_servico.infraestrutura.metricas_da_saga import (
    FALHAS_DO_COLETOR,
    LeituraDaSaga,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Connection

    from src.ordem_servico.infraestrutura.coletor_da_api import _Leitura

_NOMES = {
    "pytstop_saga_ativas",
    "pytstop_saga_etapa_mais_antiga_segundos",
    "pytstop_saga_prazo_vencido_segundos",
    "pytstop_saga_coletor_disponivel",
    "pytstop_outbox_mais_antiga_segundos",
    "pytstop_os_por_status",
    "pytstop_os_tempo_medio_status_segundos",
}
_SAGA = LeituraDaSaga(por_etapa={"em_execucao": (2, 30.0)}, atraso_do_prazo=0.0)
_ORDENS = MetricasDasOrdens(
    total=1,
    por_status={"recebida": 1},
    tempo_medio_por_status_segundos={"recebida": 12.5},
)


@pytest.fixture(autouse=True)
def _log_fresco(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coletor_da_api, "_log", structlog.get_logger("test_coletor"))


def _sem_conexao() -> Connection:
    pytest.fail("nenhuma conexao aberta neste teste")


def _coletor(**kwargs: object) -> ColetorDaApi:
    return ColetorDaApi(_sem_conexao, **kwargs)  # type: ignore[arg-type]  # kwargs do coletor


def _trocar_a_leitura(
    coletor: ColetorDaApi, leitura: Callable[[_Leitura], None]
) -> None:
    """A leitura do banco do coletor vira ``leitura`` (o resto da mecanica e real)."""
    coletor._ler_tudo = leitura  # type: ignore[method-assign,assignment]  # troca no teste


def _raspar(coletor: ColetorDaApi) -> dict[str, list[float]]:
    return {f.name: [a.value for a in f.samples] for f in coletor.collect()}


@pytest.fixture
def liberar() -> Iterator[threading.Event]:
    """Solta as leituras presas ao fim do teste: nenhuma thread fica pendurada."""
    evento = threading.Event()
    yield evento
    evento.set()


class TestDescribe:
    def test_o_registro_conhece_todos_os_nomes_sem_consultar_o_banco(self) -> None:
        registro = CollectorRegistry()
        registro.register(_coletor())

        assert set(registro._names_to_collectors) == _NOMES  # nomes do registro

    def test_dois_coletores_no_mesmo_registro_levantam(self) -> None:
        # O describe devolve os nomes: o registro recusa a serie duplicada, em vez
        # de a raspagem sair com a mesma serie duas vezes.
        registro = CollectorRegistry()
        registro.register(_coletor())

        with pytest.raises(ValueError, match="Duplicated timeseries"):
            registro.register(_coletor())


class TestRaspagem:
    def test_com_tudo_lido_publica_as_familias_e_a_disponibilidade_em_1(self) -> None:
        coletor = _coletor()

        def ler(leitura: _Leitura) -> None:
            leitura.saga, leitura.outbox, leitura.ordens = _SAGA, 42.0, _ORDENS

        _trocar_a_leitura(coletor, ler)

        raspagem = _raspar(coletor)

        assert set(raspagem) == _NOMES
        assert raspagem["pytstop_saga_coletor_disponivel"] == [1]
        assert raspagem["pytstop_outbox_mais_antiga_segundos"] == [42.0]

    def test_so_o_que_foi_lido_sai_e_o_resto_fica_ausente(self) -> None:
        coletor = _coletor()

        def ler(leitura: _Leitura) -> None:
            leitura.saga, leitura.ordens = _SAGA, _ORDENS

        _trocar_a_leitura(coletor, ler)

        raspagem = _raspar(coletor)

        # Gauge que nao foi lido nunca vira zero.
        assert "pytstop_outbox_mais_antiga_segundos" not in raspagem
        assert "pytstop_os_por_status" in raspagem

    def test_sem_a_leitura_da_saga_so_a_disponibilidade_em_0_e_a_falha_conta(
        self,
    ) -> None:
        coletor = _coletor()

        def ler(leitura: _Leitura) -> None:
            leitura.outbox = 1.0

        _trocar_a_leitura(coletor, ler)
        falhas = FALHAS_DO_COLETOR._value.get()  # contador do cliente

        raspagem = _raspar(coletor)

        assert raspagem["pytstop_saga_coletor_disponivel"] == [0]
        assert "pytstop_saga_ativas" not in raspagem
        assert raspagem["pytstop_outbox_mais_antiga_segundos"] == [1.0]
        assert FALHAS_DO_COLETOR._value.get() == falhas + 1

    def test_excecao_na_leitura_nao_derruba_a_raspagem_e_devolve_a_vez(self) -> None:
        coletor = _coletor()

        def ler(_leitura: _Leitura) -> None:
            msg = "banco fora"
            raise OSError(msg)

        _trocar_a_leitura(coletor, ler)

        with capture_logs() as logs:
            primeira = _raspar(coletor)
            segunda = _raspar(coletor)

        assert primeira == segunda == {"pytstop_saga_coletor_disponivel": [0]}
        # A segunda raspagem tentou de novo: a vez voltou depois da falha.
        assert [log["motivo"] for log in logs] == ["OSError", "OSError"]

    def test_antes_do_boot_marca_o_coletor_indisponivel(self) -> None:
        def sem_engine() -> Connection:
            msg = "Engine de metricas nao configurada"
            raise RuntimeError(msg)

        coletor = ColetorDaApi(sem_engine)

        assert _raspar(coletor) == {"pytstop_saga_coletor_disponivel": [0]}


class TestPrazo:
    def test_leitura_presa_nao_pendura_a_raspagem(
        self, liberar: threading.Event
    ) -> None:
        coletor = _coletor(orcamento_s=0.3)

        def presa(leitura: _Leitura) -> None:
            leitura.saga = _SAGA
            liberar.wait(30)

        _trocar_a_leitura(coletor, presa)

        inicio = time.monotonic()
        with capture_logs() as logs:
            raspagem = _raspar(coletor)
        decorrido = time.monotonic() - inicio

        assert 0.3 <= decorrido < 3
        # O que ja estava lido sai; o que falta fica ausente.
        assert raspagem["pytstop_saga_coletor_disponivel"] == [1]
        assert "pytstop_outbox_mais_antiga_segundos" not in raspagem
        assert [log["motivo"] for log in logs] == ["prazo_esgotado"]

    def test_com_a_leitura_anterior_presa_a_raspagem_nem_tenta(
        self, liberar: threading.Event
    ) -> None:
        coletor = _coletor(orcamento_s=0.2)
        chamadas: list[int] = []

        def presa(_leitura: _Leitura) -> None:
            chamadas.append(1)
            liberar.wait(30)

        _trocar_a_leitura(coletor, presa)
        _raspar(coletor)  # a primeira fica presa depois do prazo

        inicio = time.monotonic()
        with capture_logs() as logs:
            raspagem = _raspar(coletor)
        decorrido = time.monotonic() - inicio

        # Uma thread presa por vez: a segunda raspagem sai na hora, sem gauges.
        assert decorrido < 0.15
        assert raspagem == {"pytstop_saga_coletor_disponivel": [0]}
        assert chamadas == [1]
        assert [log["motivo"] for log in logs] == ["raspagem_anterior_presa"]

    def test_solta_a_leitura_presa_a_proxima_raspagem_volta_a_ler(
        self, liberar: threading.Event
    ) -> None:
        coletor = _coletor(orcamento_s=0.2)
        livre = threading.Event()

        def ler(leitura: _Leitura) -> None:
            if not livre.is_set():
                liberar.wait(30)
                return
            leitura.saga, leitura.outbox, leitura.ordens = _SAGA, 5.0, _ORDENS

        _trocar_a_leitura(coletor, ler)
        _raspar(coletor)
        livre.set()
        liberar.set()
        # A thread presa termina e devolve a vez.
        for _ in range(100):
            if coletor._ocupado.acquire(blocking=False):
                coletor._ocupado.release()
                break
            time.sleep(0.02)

        assert _raspar(coletor)["pytstop_outbox_mais_antiga_segundos"] == [5.0]

    def test_raspagem_nao_espera_alem_do_necessario(self) -> None:
        coletor = _coletor(orcamento_s=30)

        def rapida(leitura: _Leitura) -> None:
            leitura.saga = _SAGA

        _trocar_a_leitura(coletor, rapida)

        inicio = time.monotonic()
        _raspar(coletor)

        # O prazo e um teto: a leitura pronta devolve a raspagem na hora.
        assert time.monotonic() - inicio < 2


class TestRegistro:
    @pytest.fixture
    def registro(self, monkeypatch: pytest.MonkeyPatch) -> CollectorRegistry:
        registro = CollectorRegistry()
        monkeypatch.setattr(coletor_da_api, "REGISTRY", registro)
        monkeypatch.setattr(coletor_da_api, "_coletor", None)
        return registro

    def test_registra_uma_vez_por_processo(self, registro: CollectorRegistry) -> None:
        primeiro = registrar_coletor(_sem_conexao)
        segundo = registrar_coletor(_sem_conexao)

        assert primeiro is not None
        assert segundo is None
        assert set(registro._names_to_collectors) == _NOMES

    def test_quem_registrou_tira_e_o_processo_pode_registrar_de_novo(
        self, registro: CollectorRegistry
    ) -> None:
        coletor = registrar_coletor(_sem_conexao)
        assert coletor is not None

        desregistrar_coletor(coletor)

        assert registro._names_to_collectors == {}
        assert registrar_coletor(_sem_conexao) is not None
