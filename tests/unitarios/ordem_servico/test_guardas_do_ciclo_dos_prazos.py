"""As guardas do ciclo dos prazos que o deixam sem agir (RFC-004 secao 4.6).

O relogio do processo fora do relogio do banco, a fila ``os.eventos`` sem
consumidor ou com respostas esperando, e o teto de sagas por ciclo. Cada guarda e
provada com a saga vencida e o ciclo intacto: nenhum comando, nenhum commit, a
saga como estava.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import structlog
from structlog.testing import capture_logs

from src.ordem_servico.aplicacao.saga import vencer_prazos
from src.ordem_servico.aplicacao.saga.vencer_prazos import (
    DESVIO_MAXIMO_DO_RELOGIO,
    SAGAS_POR_CICLO,
    ResultadoDoCiclo,
)
from tests.unitarios.ordem_servico.cenario_da_saga import CenarioDaSaga
from tests.unitarios.ordem_servico.ciclo_dos_prazos import Prazos


@pytest.fixture(autouse=True)
def _logger_do_modulo_sem_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # O logger de modulo fica em cache depois do primeiro uso (outro teste da
    # suite), e o `capture_logs` nao o alcancaria.
    monkeypatch.setattr(vencer_prazos, "_log", structlog.get_logger())


def _vencida() -> Prazos:
    prazos = Prazos(CenarioDaSaga.em("aguardando_orcamento"))
    prazos.relogio.agora = prazos.saga.prazo_resposta_em + timedelta(seconds=30)
    return prazos


def _intacto(prazos: Prazos, antes: tuple[object, ...]) -> None:
    saga = prazos.saga
    assert (saga.etapa, saga.reenvios, saga.passos) == antes
    assert (prazos.comandos, prazos.transacao.commits) == ([], 0)


class TestRelogioForaDoBanco:
    @pytest.mark.parametrize(
        "adiantamento",
        [
            pytest.param(timedelta(seconds=100), id="adiantado-100-s"),
            pytest.param(-timedelta(seconds=100), id="atrasado-100-s"),
            pytest.param(
                DESVIO_MAXIMO_DO_RELOGIO + timedelta(milliseconds=1),
                id="logo-acima-do-teto",
            ),
        ],
    )
    def test_o_ciclo_nao_reenvia_nem_esgota(self, adiantamento: timedelta) -> None:
        prazos = _vencida()
        prazos.adiantamento = adiantamento
        saga = prazos.saga
        antes = (saga.etapa, saga.reenvios, saga.passos)

        with capture_logs() as logs:
            resultado = prazos.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo(pausado=True)
        _intacto(prazos, antes)
        (erro,) = [e for e in logs if e["log_level"] == "error"]
        assert erro["event"] == "clock skew too large: saga deadlines skipped"
        assert erro["desvio_s"] == pytest.approx(
            abs(adiantamento.total_seconds()), abs=0.01
        )
        assert erro["limite_s"] == DESVIO_MAXIMO_DO_RELOGIO.total_seconds()

    def test_esgotado_tambem_espera_o_relogio_voltar(self) -> None:
        prazos = _vencida()
        prazos.saga._reenvios = 5
        prazos.adiantamento = timedelta(seconds=100)
        antes = (prazos.saga.etapa, prazos.saga.reenvios, prazos.saga.passos)

        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(pausado=True)

        _intacto(prazos, antes)

    @pytest.mark.parametrize(
        "adiantamento",
        [
            pytest.param(timedelta(0), id="sem-desvio"),
            pytest.param(timedelta(seconds=4), id="adiantado-4-s"),
            pytest.param(-timedelta(seconds=4), id="atrasado-4-s"),
            pytest.param(DESVIO_MAXIMO_DO_RELOGIO, id="exatamente-no-teto"),
        ],
    )
    def test_dentro_do_teto_o_ciclo_age(self, adiantamento: timedelta) -> None:
        prazos = _vencida()
        prazos.adiantamento = adiantamento

        with capture_logs() as logs:
            resultado = prazos.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo(reenviadas=1)
        assert [e for e in logs if e["log_level"] == "error"] == []

    def test_a_saga_segue_de_onde_estava_quando_o_relogio_volta(self) -> None:
        prazos = _vencida()
        prazos.adiantamento = timedelta(seconds=100)
        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(pausado=True)

        prazos.adiantamento = timedelta(0)

        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(reenviadas=1)

    def test_o_relogio_do_banco_e_lido_uma_vez_por_ciclo(self) -> None:
        prazos = _vencida()

        prazos.caso_de_uso.executar()

        assert prazos.transacao.leituras_do_relogio == 1


def _varias(quantas: int) -> Prazos:
    """``quantas`` sagas vencidas, nos mesmos repositorios."""
    cenarios = [CenarioDaSaga.em("aguardando_orcamento") for _ in range(quantas)]
    primeiro = cenarios[0]
    for outro in cenarios[1:]:
        primeiro.ordens.ordens.update(outro.ordens.ordens)
        primeiro.sagas.sagas.update(outro.sagas.sagas)
    prazos = Prazos(primeiro)
    prazos.relogio.agora = max(
        s.prazo_resposta_em for s in primeiro.sagas.sagas.values()
    ) + timedelta(seconds=30)
    return prazos


class TestFilaDoConsumidor:
    def test_com_mensagens_prontas_na_fila_nada_e_reenviado_nem_esgotado(
        self,
    ) -> None:
        prazos = _vencida()
        prazos.saga._reenvios = 5
        prazos.presenca.prontas = 12
        antes = (prazos.saga.etapa, prazos.saga.reenvios, prazos.saga.passos)

        with capture_logs() as logs:
            resultado = prazos.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo(pausado=True)
        _intacto(prazos, antes)
        (aviso,) = [e for e in logs if e["event"] == "saga deadlines paused"]
        assert (aviso["log_level"], aviso["motivo"], aviso["prontas"]) == (
            "warning",
            "respostas_na_fila",
            12,
        )

    def test_a_saga_segue_quando_o_consumidor_drena_a_fila(self) -> None:
        prazos = _vencida()
        prazos.presenca.prontas = 3
        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(pausado=True)

        prazos.presenca.prontas = 0

        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(reenviadas=1)

    def test_sem_o_broker_nada_e_reenviado(self) -> None:
        prazos = _vencida()
        prazos.presenca.broker_fora = True

        with capture_logs() as logs:
            resultado = prazos.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo(pausado=True)
        assert prazos.comandos == []
        assert [e["motivo"] for e in logs if e["event"] == "saga deadlines paused"] == [
            "broker_fora"
        ]

    def test_o_consumidor_volta_com_a_fila_cheia_e_300_vencidas_esperam(self) -> None:
        prazos = _varias(300)
        prazos.presenca.prontas = 40
        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(pausado=True)
        assert (prazos.comandos, prazos.transacao.commits) == ([], 0)

        # A fila esvazia: o ciclo trata o teto e o seguinte segue dali.
        prazos.presenca.prontas = 0
        resultados = [prazos.caso_de_uso.executar() for _ in range(3)]

        assert resultados == [ResultadoDoCiclo(reenviadas=SAGAS_POR_CICLO)] * 3
        assert len(prazos.comandos) == 300
        assert SAGAS_POR_CICLO == 100

    def test_o_ciclo_trata_no_maximo_100_sagas_e_a_cada_uma_confere_a_fila(
        self,
    ) -> None:
        prazos = _varias(130)

        primeiro = prazos.caso_de_uso.executar()

        assert primeiro == ResultadoDoCiclo(reenviadas=SAGAS_POR_CICLO)
        assert prazos.presenca.consultas == SAGAS_POR_CICLO
        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(reenviadas=30)

    def test_a_fila_que_muda_no_meio_do_ciclo_interrompe_o_resto(self) -> None:
        prazos = _varias(10)
        # Os tres primeiros passam; a quarta consulta ja encontra a fila cheia.
        prazos.presenca.cai_na_consulta = 4

        resultado = prazos.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo(pausado=True, reenviadas=3)
        assert len(prazos.comandos) == 3
        assert prazos.transacao.commits == 3
