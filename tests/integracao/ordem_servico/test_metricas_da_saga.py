"""Metricas da saga contra o Postgres: contadores so no commit e gauges por consulta."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import structlog
from prometheus_client import REGISTRY, CollectorRegistry
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError
from structlog.testing import capture_logs

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.database import criar_engine_de_metricas
from src.ordem_servico.aplicacao.saga.modelo import (
    Envio,
    EtapaSaga,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import ETAPAS_NAO_FINAIS
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.infraestrutura import metricas_da_saga as modulo_metricas
from src.ordem_servico.infraestrutura.mapping import sagas_table
from src.ordem_servico.infraestrutura.metricas_da_saga import ColetorDaSaga
from src.ordem_servico.infraestrutura.repository import SagaSQLAlchemyRepository
from tests.eventos import evento
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
    sagas_recusando_no_commit,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection, Engine
    from sqlalchemy.orm import Session, sessionmaker

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)
# Marcos da OS com que cada evento usado aqui se classifica para processar.
_MARCOS = MarcosDaOrdem(
    diagnostico_iniciado=True, checkout_aberto=False, encerrada=False
)
_MARCOS_DA_EXECUCAO = MarcosDaOrdem(
    diagnostico_iniciado=True, checkout_aberto=True, encerrada=False
)


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
    concluido = evento("DiagnosticoConcluido", saga.ordem_id)
    saga.avancar(
        concluido,
        _MARCOS,
        agora=AGORA + depois,
        ator="consumidor",
        envio=Envio(
            tipo=Comando.GERAR_ORCAMENTO,
            id=uuid4(),
            dados={
                "ordem_id": str(saga.ordem_id),
                "itens": itens_do_diagnostico(concluido.dados),
            },
            prazo_resposta_em=AGORA + depois + timedelta(minutes=2),
        ),
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
            _MARCOS_DA_EXECUCAO,
            agora=AGORA + timedelta(hours=2),
            ator="consumidor",
        )
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()

    assert (
        _amostra("pytstop_saga_finalizadas_total", resultado="concluida")
        == concluidas + 1
    )


def test_commit_que_falha_nao_conta_a_saga_concluida(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as sess:
        saga = Saga(
            id=_ordem_id(sess),
            _etapa=EtapaSaga.EM_EXECUCAO,
            _iniciada_em=AGORA,
            _etapa_desde=AGORA,
            _atualizada_em=AGORA,
        )
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()
    concluidas = _amostra("pytstop_saga_finalizadas_total", resultado="concluida")
    duracoes = _duracoes("em_execucao")

    # O trigger deferido so falha no COMMIT, depois do UPDATE da saga: contar
    # antes dele (before_commit) contaria uma saga que nao concluiu.
    with sagas_recusando_no_commit(engine), session_factory() as sess:
        lida = SagaSQLAlchemyRepository(sess).obter(saga.ordem_id)
        assert lida is not None
        lida.avancar(
            evento("ExecucaoFinalizada", lida.ordem_id),
            _MARCOS_DA_EXECUCAO,
            agora=AGORA + timedelta(hours=2),
            ator="consumidor",
        )
        SagaSQLAlchemyRepository(sess).salvar(lida)
        with pytest.raises(DBAPIError):
            sess.commit()

    assert (
        _amostra("pytstop_saga_finalizadas_total", resultado="concluida") == concluidas
    )
    assert _duracoes("em_execucao") == duracoes


def test_duracao_e_contada_desde_a_entrada_na_etapa(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as sess:
        saga = _nova_saga(sess)
        SagaSQLAlchemyRepository(sess).salvar(saga)
        _concluir_diagnostico(saga, timedelta(minutes=10))
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()
    contagem, soma = _duracoes("aguardando_orcamento")

    with session_factory() as sess:
        lida = SagaSQLAlchemyRepository(sess).obter(saga.ordem_id)
        assert lida is not None
        lida.avancar(
            evento("OrcamentoGerado", lida.ordem_id),
            _MARCOS,
            agora=AGORA + timedelta(minutes=25),
            ator="consumidor",
        )
        SagaSQLAlchemyRepository(sess).salvar(lida)
        sess.commit()

    # 15 min em aguardando_orcamento (de 10 a 25), nao 25 desde a abertura.
    assert _duracoes("aguardando_orcamento") == (contagem + 1, soma + 900)


def test_salvar_a_mesma_saga_duas_vezes_conta_o_fato_uma_vez(
    session_factory: sessionmaker[Session],
) -> None:
    iniciadas = _iniciadas()

    with session_factory() as sess:
        saga = _nova_saga(sess)
        SagaSQLAlchemyRepository(sess).salvar(saga)
        SagaSQLAlchemyRepository(sess).salvar(saga)
        sess.commit()

    assert _iniciadas() == iniciadas + 1


def test_savepoint_liberado_nao_conta_antes_do_commit_da_raiz(
    session_factory: sessionmaker[Session],
) -> None:
    iniciadas = _iniciadas()

    with session_factory() as sess:
        with sess.begin_nested():
            SagaSQLAlchemyRepository(sess).salvar(_nova_saga(sess))
        # O savepoint foi liberado, mas a transacao raiz ainda pode cair.
        assert _iniciadas() == iniciadas
        sess.rollback()
    assert _iniciadas() == iniciadas

    with session_factory() as sess:
        with sess.begin_nested():
            SagaSQLAlchemyRepository(sess).salvar(_nova_saga(sess))
        sess.commit()
    assert _iniciadas() == iniciadas + 1


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
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as sess:
        # O relogio do banco, o mesmo do now() da consulta do coletor.
        agora = sess.execute(text("SELECT now()")).scalar_one()
        _gravar_em(sess, EtapaSaga.AGUARDANDO_DECISAO, agora - timedelta(hours=3))
        _gravar_em(sess, EtapaSaga.AGUARDANDO_DECISAO, agora - timedelta(hours=1))
        _gravar_em(sess, EtapaSaga.FALHA_NA_COMPENSACAO, agora - timedelta(minutes=5))
        _gravar_em(sess, EtapaSaga.CONCLUIDA, agora - timedelta(days=9))
        sess.commit()
    registro = _registro(ColetorDaSaga(engine.connect))

    def gauge(nome: str, etapa: str) -> float | None:
        return registro.get_sample_value(nome, {"etapa": etapa})

    assert gauge("pytstop_saga_ativas", "aguardando_decisao") == 2
    assert gauge("pytstop_saga_ativas", "falha_na_compensacao") == 1
    # Etapa sem saga sai com zero; etapa final nao sai.
    assert gauge("pytstop_saga_ativas", "em_execucao") == 0
    assert gauge("pytstop_saga_ativas", "concluida") is None
    antiga = gauge("pytstop_saga_etapa_mais_antiga_segundos", "aguardando_decisao")
    assert antiga is not None
    # A consulta roda segundos depois do now() lido acima.
    assert 3 * 3600 <= antiga <= 3 * 3600 + 30
    assert gauge("pytstop_saga_etapa_mais_antiga_segundos", "em_execucao") == 0
    assert registro.get_sample_value("pytstop_saga_coletor_disponivel") == 1


def test_etapa_de_outra_versao_fica_fora_sem_derrubar_a_raspagem(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as sess:
        ordem_id = _gravar_em(sess, EtapaSaga.AGUARDANDO_DECISAO, AGORA)
        sess.execute(
            text(
                "UPDATE sagas SET etapa = 'etapa_de_outra_versao' WHERE ordem_id = :id"
            ),
            {"id": ordem_id},
        )
        sess.commit()

    registro = _registro(ColetorDaSaga(engine.connect))

    assert registro.get_sample_value("pytstop_saga_coletor_disponivel") == 1
    assert (
        registro.get_sample_value(
            "pytstop_saga_ativas", {"etapa": "aguardando_decisao"}
        )
        == 0
    )


def _registro(coletor: ColetorDaSaga) -> CollectorRegistry:
    registro = CollectorRegistry()
    registro.register(coletor)
    return registro


def _falhas() -> float:
    return _amostra("pytstop_saga_coletor_falhas_total")


def _raspar(coletor: ColetorDaSaga) -> dict[str, list[float]]:
    """Uma raspagem: os valores de cada familia que o coletor publicou."""
    return {f.name: [a.value for a in f.samples] for f in coletor.collect()}


def _so_indisponivel(raspagem: dict[str, list[float]]) -> None:
    # Gauges ausentes, nunca zero (zero esconderia uma saga parada), e a serie
    # de disponibilidade em 0 para o alerta.
    assert raspagem == {"pytstop_saga_coletor_disponivel": [0]}


def test_coletor_antes_do_boot_marca_o_coletor_indisponivel() -> None:
    def sem_conexao() -> Connection:
        msg = "Engine de metricas nao configurada"
        raise RuntimeError(msg)

    falhas = _falhas()

    _so_indisponivel(_raspar(ColetorDaSaga(sem_conexao)))
    assert _falhas() == falhas + 1


def test_banco_fora_do_ar_marca_o_coletor_indisponivel_e_loga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo_metricas, "_log", structlog.get_logger())
    # Porta sem servidor: a conexao e recusada (OperationalError, do SQLAlchemy).
    fora = criar_engine_de_metricas("postgresql://os:os@127.0.0.1:1/os")
    falhas = _falhas()

    with capture_logs() as logs:
        _so_indisponivel(_raspar(ColetorDaSaga(fora.connect)))

    assert _falhas() == falhas + 1
    assert {
        "event": "saga gauges unavailable",
        "log_level": "warning",
        "erro": "OperationalError",
    } in logs
    fora.dispose()


def test_tabela_travada_esgota_o_prazo_do_coletor_em_segundos(
    engine: Engine,
) -> None:
    coletor = ColetorDaSaga(
        criar_engine_de_metricas(
            engine.url.render_as_string(hide_password=False)
        ).connect
    )
    with engine.connect() as trava, trava.begin():
        trava.execute(text("LOCK TABLE sagas IN ACCESS EXCLUSIVE MODE"))
        inicio = time.monotonic()
        raspagem = _raspar(coletor)
        decorrido = time.monotonic() - inicio

    # O lock_timeout (1 s) corta a espera, bem antes do prazo da raspagem.
    _so_indisponivel(raspagem)
    assert decorrido < 5


def test_coletor_nao_espera_a_conexao_ocupada_alem_do_prazo(engine: Engine) -> None:
    metricas = criar_engine_de_metricas(
        engine.url.render_as_string(hide_password=False)
    )
    with metricas.connect():
        # A unica conexao da engine das metricas esta em uso: o coletor espera
        # o pool_timeout (1 s) e desiste.
        inicio = time.monotonic()
        raspagem = _raspar(ColetorDaSaga(metricas.connect))
        decorrido = time.monotonic() - inicio

    _so_indisponivel(raspagem)
    assert decorrido < 5
    metricas.dispose()
