"""Fatos da saga e a regra do commit nas metricas (sem banco)."""

from __future__ import annotations

import math
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, Histogram
from sqlalchemy.orm import Session

from src.compartilhado.dominio.events import DomainEvent
from src.ordem_servico.aplicacao.saga.modelo import (
    CompensacaoIniciadaEvent,
    MotivoCompensacao,
)
from src.ordem_servico.aplicacao.saga.prazos import (
    ComandoReenviadoEvent,
    PrazoEsgotadoEvent,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    COMANDOS_COM_PRAZO,
    ETAPAS_NAO_FINAIS,
)
from src.ordem_servico.infraestrutura import metricas_da_saga
from tests.subprocesso import executar_python


def test_fato_sem_metrica_levanta_antes_do_commit() -> None:
    # Um fato novo da saga sem a observacao dele falharia em silencio no
    # after_commit; aqui levanta antes, com a transacao ainda desfazivel.
    sessao = Session()

    fatos = [DomainEvent(agregado_id=uuid4())]
    with pytest.raises(TypeError, match="DomainEvent"):
        metricas_da_saga.anotar(sessao, fatos)

    assert sessao.info == {}


def _compensacoes(motivo: str) -> float | None:
    return REGISTRY.get_sample_value(
        "pytstop_saga_compensacoes_total", {"motivo": motivo}
    )


def test_compensacoes_tem_a_serie_de_cada_motivo_e_contam_no_commit() -> None:
    # Series de rotulo fechado nascem com o processo: o increase() do painel
    # nao perde a primeira compensacao. O fato so vira metrica no commit.
    assert all(_compensacoes(m.value) is not None for m in MotivoCompensacao)
    antes = _compensacoes("reserva_falhou")
    sessao = Session()
    fato = CompensacaoIniciadaEvent(
        agregado_id=uuid4(), motivo=MotivoCompensacao.RESERVA_FALHOU
    )

    metricas_da_saga.anotar(sessao, [fato])
    assert _compensacoes("reserva_falhou") == antes
    sessao.commit()

    assert _compensacoes("reserva_falhou") == (antes or 0) + 1


def _contador(nome: str, comando: str) -> float | None:
    return REGISTRY.get_sample_value(nome, {"comando": comando})


@pytest.mark.parametrize(
    ("nome", "fato"),
    [
        pytest.param(
            "pytstop_saga_reenvios_total", ComandoReenviadoEvent, id="reenvio"
        ),
        pytest.param(
            "pytstop_saga_prazos_esgotados_total", PrazoEsgotadoEvent, id="esgotado"
        ),
    ],
)
def test_reenvios_e_prazos_esgotados_tem_a_serie_de_cada_comando_e_contam_no_commit(
    nome: str, fato: type[DomainEvent]
) -> None:
    # As nove series nascem em zero com o processo; o fato so conta no commit e
    # so na serie do comando dele.
    assert {c.value for c in COMANDOS_COM_PRAZO} == {
        "GerarOrcamento",
        "ReservarPecas",
        "SolicitarPagamento",
        "AgendarExecucao",
        "CancelarExecucao",
        "EstornarPagamento",
        "LiberarReserva",
        "CancelarOrcamento",
        "DescartarDiagnostico",
    }
    assert all(_contador(nome, c.value) is not None for c in COMANDOS_COM_PRAZO)
    antes = _contador(nome, "LiberarReserva") or 0
    outro = _contador(nome, "GerarOrcamento")
    sessao = Session()

    metricas_da_saga.anotar(
        sessao,
        [fato(agregado_id=uuid4(), comando="LiberarReserva")],
    )
    assert _contador(nome, "LiberarReserva") == antes
    sessao.commit()

    assert _contador(nome, "LiberarReserva") == antes + 1
    assert _contador(nome, "GerarOrcamento") == outro


def test_rollback_descarta_o_reenvio_anotado() -> None:
    antes = _contador("pytstop_saga_reenvios_total", "ReservarPecas")
    sessao = Session()
    sessao.begin()
    metricas_da_saga.anotar(
        sessao, [ComandoReenviadoEvent(agregado_id=uuid4(), comando="ReservarPecas")]
    )

    sessao.rollback()

    assert _contador("pytstop_saga_reenvios_total", "ReservarPecas") == antes


def test_a_os_criada_conta_depois_do_commit_sem_carregar_as_metricas_da_saga() -> None:
    # A regra "conta depois do commit" mora em `metricas_apos_commit`: a metrica da
    # OS criada nao herda as series da saga (nem a proxima metrica de sessao).
    carregados = executar_python(
        "import sys\n"
        "import src.ordem_servico.infraestrutura.metrics\n"
        "print(sorted(m for m in sys.modules if 'infraestrutura.metricas' in m))"
    )

    assert "metricas_apos_commit" in carregados
    assert "metricas_da_saga" not in carregados


DURACAO = "pytstop_saga_etapa_duracao_segundos"
# 50 ms a 7 dias: a resposta automatica leva de milissegundos a poucos segundos, o prazo
# tecnico e de 120 s e a espera humana chega a dias.
LIMITES_DA_DURACAO = (
    "0.05",
    "0.1",
    "0.25",
    "0.5",
    "1.0",
    "2.5",
    "5.0",
    "10.0",
    "30.0",
    "60.0",
    "120.0",
    "300.0",
    "600.0",
    "3600.0",
    "14400.0",
    "86400.0",
    "259200.0",
    "604800.0",
    "+Inf",
)
# Os baldes de antes: o primeiro, de 5 s, engolia toda etapa automatica.
BALDES_ANTIGOS = (5, 30, 120, 600, 3600, 14400, 86400, 259200, 604800)


def _limites(etapa: str) -> set[str]:
    return {
        amostra.labels["le"]
        for familia in REGISTRY.collect()
        if familia.name == DURACAO
        for amostra in familia.samples
        if amostra.name.endswith("_bucket") and amostra.labels["etapa"] == etapa
    }


@pytest.mark.parametrize("etapa", [e.value for e in ETAPAS_NAO_FINAIS])
def test_baldes_da_duracao_vao_de_50_ms_a_7_dias_em_toda_etapa(etapa: str) -> None:
    assert _limites(etapa) == set(LIMITES_DA_DURACAO)


def _p95(baldes: tuple[float, ...] | list[float], observacoes: list[float]) -> float:
    """O ``histogram_quantile(0.95, ...)`` do Prometheus sobre um histograma novo.

    Acha o primeiro balde cujo total cumulativo alcanca 95% das observacoes e
    interpola linearmente entre o limite de baixo (0 no primeiro balde) e o de cima.
    Acima do ultimo balde finito devolve esse limite.
    """
    registro = CollectorRegistry()
    histograma = Histogram("etapa", "teste", buckets=baldes, registry=registro)
    for segundos in observacoes:
        histograma.observe(segundos)
    cumulativos = sorted(
        (float(amostra.labels["le"]), amostra.value)
        for familia in registro.collect()
        for amostra in familia.samples
        if amostra.name == "etapa_bucket"
    )
    posto = 0.95 * cumulativos[-1][1]
    limite_de_baixo, contagem_de_baixo = 0.0, 0.0
    for limite, contagem in cumulativos:
        if contagem >= posto:
            if math.isinf(limite):
                return limite_de_baixo
            alcance = (posto - contagem_de_baixo) / (contagem - contagem_de_baixo)
            return limite_de_baixo + (limite - limite_de_baixo) * alcance
        limite_de_baixo, contagem_de_baixo = limite, contagem
    raise AssertionError("o balde +Inf sempre alcanca o posto")  # pragma: no cover


def _baldes_finitos() -> list[float]:
    return sorted(float(le) for le in _limites("aguardando_reserva") if le != "+Inf")


# Etapa que responde sozinha, de 200 a 580 ms (o painel de referencia mostrava 4,75 s).
ETAPA_AUTOMATICA = [0.2 + 0.02 * i for i in range(20)]


def test_o_balde_de_5_s_dava_p95_de_4_75_s_a_etapa_de_meio_segundo() -> None:
    """Controle: reproduz o numero do painel (0,95 x 5 s) com os baldes de antes."""
    assert _p95(BALDES_ANTIGOS, ETAPA_AUTOMATICA) == pytest.approx(4.75)
    assert _p95(BALDES_ANTIGOS, [0.5]) == pytest.approx(4.75)


@pytest.mark.parametrize(
    "observacoes",
    [ETAPA_AUTOMATICA, [0.5], [0.06]],
    ids=["vinte etapas", "uma etapa", "etapa de 60 ms"],
)
def test_etapa_automatica_nao_sai_com_percentil_de_segundos(
    observacoes: list[float],
) -> None:
    assert _p95(_baldes_finitos(), observacoes) <= 1.0


def test_espera_humana_e_o_prazo_tecnico_seguem_separados_pelos_baldes() -> None:
    baldes = _baldes_finitos()

    # 25 s (o consumidor do Billing parado por 20 s) fica entre 10 s e 30 s.
    assert 10 < _p95(baldes, [25.0] * 10) <= 30
    # 90 s passa dos 60 s e fica dentro do prazo tecnico de 120 s.
    assert 60 < _p95(baldes, [90.0] * 10) <= 120
    # 2 dias de espera humana ficam abaixo de 3 dias.
    assert 86400 < _p95(baldes, [172800.0] * 10) <= 259200
