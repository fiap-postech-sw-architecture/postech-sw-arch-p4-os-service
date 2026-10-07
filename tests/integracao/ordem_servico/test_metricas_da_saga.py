"""Metricas da saga contra o Postgres: contadores so no commit e gauges por consulta."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY, CollectorRegistry
from sqlalchemy import update

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.ordem_servico.aplicacao.saga.saga import (
    ETAPAS_NAO_FINAIS,
    Envio,
    EtapaSaga,
    Saga,
)
from src.ordem_servico.infraestrutura.mapping import sagas_table
from src.ordem_servico.infraestrutura.metricas_da_saga import ColetorDaSaga
from src.ordem_servico.infraestrutura.repository import SagaSQLAlchemyRepository
from tests.eventos import evento
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session, sessionmaker

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _amostra(nome: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(nome, labels) or 0.0


def _iniciadas() -> float:
    return _amostra("pytstop_saga_iniciadas_total")


def _duracoes(etapa: str) -> tuple[float, float]:
    return (
        _amostra("pytstop_saga_etapa_duracao_segundos_count", etapa=etapa),
        _amostra("pytstop_saga_etapa_duracao_segundos_sum", etapa=etapa),
    )


def _ordem_id(sess: Session) -> UUID:
    cliente = criar_cliente_com_veiculo(sess)
    return criar_ordem_recebida(
        sess, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
    ).id


def _nova_saga(sess: Session) -> Saga:
    return Saga.iniciar(
        _ordem_id(sess),
        envio=Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=uuid4()),
        ator="atendente-teste",
        agora=AGORA,
    )


def _concluir_diagnostico(saga: Saga, depois: timedelta) -> None:
    saga.avancar(
        evento("DiagnosticoConcluido", saga.ordem_id),
        agora=AGORA + depois,
        ator="consumidor",
    )


def test_contadores_e_duracao_so_contam_no_commit(
    session_factory: sessionmaker[Session],
) -> None:
    iniciadas, (contagem, soma) = _iniciadas(), _duracoes("aguardando_diagnostico")

    with session_factory() as sess:
        saga = _nova_saga(sess)
        SagaSQLAlchemyRepository(sess).salvar(saga)
        assert _iniciadas() == iniciadas  # ainda sem commit
        sess.commit()
    assert _iniciadas() == iniciadas + 1

    with session_factory() as sess:
        lida = SagaSQLAlchemyRepository(sess).obter(saga.ordem_id)
        assert lida is not None
        _concluir_diagnostico(lida, timedelta(minutes=10))
        SagaSQLAlchemyRepository(sess).salvar(lida)
        sess.commit()

    assert _duracoes("aguardando_diagnostico") == (contagem + 1, soma + 600)


def test_rollback_e_conflito_descartam_os_fatos(
    session_factory: sessionmaker[Session],
) -> None:
    iniciadas = _iniciadas()
    with session_factory() as sess:
        SagaSQLAlchemyRepository(sess).salvar(_nova_saga(sess))
        sess.rollback()
        # A sessao segue em uso: o commit seguinte nao leva o fato desfeito.
        sess.commit()
    with session_factory() as sess:
        SagaSQLAlchemyRepository(sess).salvar(_nova_saga(sess))
        # Fechada sem commit, como a do consumidor quando o commit falha.
    assert _iniciadas() == iniciadas

    with session_factory() as sess:
        saga = _nova_saga(sess)
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()
    iniciadas, duracoes = _iniciadas(), _duracoes("aguardando_diagnostico")
    with session_factory() as sessao_a, session_factory() as sessao_b:
        saga_a = SagaSQLAlchemyRepository(sessao_a).obter(saga.ordem_id)
        saga_b = SagaSQLAlchemyRepository(sessao_b).obter(saga.ordem_id)
        assert saga_a is not None
        assert saga_b is not None
        _concluir_diagnostico(saga_a, timedelta(minutes=1))
        _concluir_diagnostico(saga_b, timedelta(minutes=2))
        SagaSQLAlchemyRepository(sessao_a).salvar(saga_a)
        sessao_a.commit()
        with pytest.raises(ConflitoDeConcorrenciaException):
            SagaSQLAlchemyRepository(sessao_b).salvar(saga_b)
        # Fechada sem commit: o fato de B nao vira metrica.

    contagem, soma = duracoes
    assert _duracoes("aguardando_diagnostico") == (contagem + 1, soma + 60)
    assert _iniciadas() == iniciadas


def test_saga_concluida_conta_nas_finalizadas(
    session_factory: sessionmaker[Session],
) -> None:
    concluidas = _amostra("pytstop_saga_finalizadas_total", resultado="concluida")
    with session_factory() as sess:
        # Saga reidratada em em_execucao conclui com o ExecucaoFinalizada.
        saga = Saga(
            id=_ordem_id(sess),
            _etapa=EtapaSaga.EM_EXECUCAO,
            _iniciada_em=AGORA,
            _etapa_desde=AGORA,
            _atualizada_em=AGORA,
        )
        saga.avancar(
            evento("ExecucaoFinalizada", saga.ordem_id),
            agora=AGORA + timedelta(hours=2),
            ator="consumidor",
        )
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()

    assert (
        _amostra("pytstop_saga_finalizadas_total", resultado="concluida")
        == concluidas + 1
    )


def test_series_de_rotulo_fechado_existem_desde_o_boot() -> None:
    for resultado in ("concluida", "compensada"):
        assert (
            REGISTRY.get_sample_value(
                "pytstop_saga_finalizadas_total", {"resultado": resultado}
            )
            is not None
        )
    for etapa in ETAPAS_NAO_FINAIS:
        assert (
            REGISTRY.get_sample_value(
                "pytstop_saga_etapa_duracao_segundos_count", {"etapa": etapa.value}
            )
            is not None
        )


def _gravar_em(sess: Session, etapa: EtapaSaga, desde: datetime) -> UUID:
    saga = _nova_saga(sess)
    SagaSQLAlchemyRepository(sess).salvar(saga)
    sess.execute(
        update(sagas_table)
        .where(sagas_table.c.ordem_id == saga.ordem_id)
        .values(etapa=etapa, etapa_desde=desde)
    )
    return saga.ordem_id


def test_coletor_conta_as_ativas_e_a_mais_antiga_por_etapa(
    session_factory: sessionmaker[Session],
) -> None:
    agora = datetime.now(UTC)
    with session_factory() as sess:
        _gravar_em(sess, EtapaSaga.AGUARDANDO_DECISAO, agora - timedelta(hours=3))
        _gravar_em(sess, EtapaSaga.AGUARDANDO_DECISAO, agora - timedelta(hours=1))
        _gravar_em(sess, EtapaSaga.FALHA_NA_COMPENSACAO, agora - timedelta(minutes=5))
        _gravar_em(sess, EtapaSaga.CONCLUIDA, agora - timedelta(days=9))
        sess.commit()
    registro = CollectorRegistry()
    registro.register(ColetorDaSaga(session_factory))

    def gauge(nome: str, etapa: str) -> float | None:
        return registro.get_sample_value(nome, {"etapa": etapa})

    assert gauge("pytstop_saga_ativas", "aguardando_decisao") == 2
    assert gauge("pytstop_saga_ativas", "falha_na_compensacao") == 1
    # Etapa sem saga sai com zero; etapa final nao sai.
    assert gauge("pytstop_saga_ativas", "em_execucao") == 0
    assert gauge("pytstop_saga_ativas", "concluida") is None
    antiga = gauge("pytstop_saga_etapa_mais_antiga_segundos", "aguardando_decisao")
    assert antiga is not None
    assert 3 * 3600 - 60 <= antiga <= 3 * 3600 + 60
    assert gauge("pytstop_saga_etapa_mais_antiga_segundos", "em_execucao") == 0


def test_coletor_sem_banco_omite_os_gauges() -> None:
    def sem_sessao() -> Session:
        msg = "Session factory nao configurada"
        raise RuntimeError(msg)

    registro = CollectorRegistry()
    registro.register(ColetorDaSaga(sem_sessao))

    assert (
        registro.get_sample_value("pytstop_saga_ativas", {"etapa": "em_execucao"})
        is None
    )
