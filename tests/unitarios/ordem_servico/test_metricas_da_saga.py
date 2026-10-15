"""Fatos da saga e a regra do commit nas metricas (sem banco)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from prometheus_client import REGISTRY
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
from src.ordem_servico.aplicacao.saga.tabela_da_saga import COMANDOS_COM_PRAZO
from src.ordem_servico.infraestrutura import metricas_da_saga
from tests.subprocesso import executar_python


def test_fato_sem_metrica_levanta_antes_do_commit() -> None:
    # Um fato novo da saga sem a observacao dele falharia em silencio no
    # after_commit; aqui levanta antes, com a transacao ainda desfazivel.
    sessao = Session()

    with pytest.raises(TypeError, match="DomainEvent"):
        metricas_da_saga.anotar(sessao, [DomainEvent(agregado_id=uuid4())])

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
