"""Os prazos da saga contra o PostgreSQL: a consulta, a transacao e as replicas.

A saga e a outbox sao gravadas pelo codigo real; o instante do ciclo e do banco
sao controlados (o ``agora`` da consulta e o ``entregue_em`` de cada linha), de
modo que 12 minutos de espera cabem num teste. O RabbitMQ real entra em
``tests/integracao/mensageria/test_prazos_com_broker.py``.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY, CollectorRegistry
from sqlalchemy import text

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.database import BANCO_FORA
from src.compartilhado.infraestrutura.outbox_mapping import gravar_comando
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.modelo import (
    Envio,
    EtapaSaga,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.saga.vencer_prazos import (
    ResultadoDoCiclo,
    VencerPrazos,
)
from src.ordem_servico.aplicacao.use_cases import RetomarCompensacao
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.infraestrutura.coletor_da_api import ColetorDaApi
from src.ordem_servico.infraestrutura.repository import SagaSQLAlchemyRepository
from src.ordem_servico.infraestrutura.transacao_dos_prazos import (
    TransacaoDosPrazosSQLAlchemy,
)
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
    outbox_recusando_no_commit,
)
from tests.unitarios.fakes import PresencaFake

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

T0 = datetime(2026, 10, 13, 12, tzinfo=UTC)
PRAZO = timedelta(seconds=120)
MAXIMO = 5
_MARCOS = MarcosDaOrdem(
    diagnostico_iniciado=True, checkout_aberto=False, encerrada=False
)
# Envio entregue, pendente ou morto na outbox, e quando foi entregue.
type Envios = Sequence[tuple[str, datetime | None]]


def _entregue(quando: datetime) -> tuple[str, datetime | None]:
    return "entregue", quando


PENDENTE = ("pendente", None)
MORTO = ("dead", None)


def _semear(
    sess: Session,
    envios: Envios,
    *,
    prazo_em: datetime | None = T0 - timedelta(minutes=1),
    enviado_em: datetime = T0 - timedelta(minutes=20),
) -> UUID:
    """Uma OS e a saga em ``aguardando_orcamento``, com o ``GerarOrcamento`` enviado.

    ``envios`` e o estado de cada linha da outbox: o original e, depois, os
    reenvios; o ultimo e o que decide o vencimento. ``prazo_em`` e o
    ``prazo_resposta_em`` gravado pela saga (``None``: a saga sem prazo).
    """
    cliente = criar_cliente_com_veiculo(sess)
    ordem_id = criar_ordem_recebida(
        sess, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
    ).id
    sagas = SagaSQLAlchemyRepository(sess)
    saga = Saga.iniciar(
        ordem_id,
        envio=Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=uuid4()),
        ator="atendente-teste",
        agora=enviado_em - timedelta(minutes=10),
    )
    sagas.salvar(saga)
    concluido = evento("DiagnosticoConcluido", ordem_id)
    dados = {"ordem_id": str(ordem_id), "itens": itens_do_diagnostico(concluido.dados)}
    ids = [
        gravar_comando(
            sess,
            Comando.GERAR_ORCAMENTO,
            dados,
            correlation_id=ordem_id,
            causation_id=None,
        )
        for _ in envios
    ]
    saga.avancar(
        concluido,
        _MARCOS,
        agora=enviado_em,
        ator="consumidor",
        envio=Envio(
            tipo=Comando.GERAR_ORCAMENTO,
            id=ids[0],
            dados=dados,
            prazo_resposta_em=enviado_em + PRAZO,
        ),
    )
    for numero, mensagem_id in enumerate(ids[1:], start=1):
        quando = enviado_em + numero * PRAZO
        saga.reenviar(
            Envio(
                tipo=Comando.GERAR_ORCAMENTO,
                id=mensagem_id,
                dados=dados,
                prazo_resposta_em=quando + PRAZO,
            ),
            agora=quando,
            ator="prazos",
            maximo=MAXIMO,
        )
    sagas.salvar(saga)
    for mensagem_id, (status, entregue_em) in zip(ids, envios, strict=True):
        sess.execute(
            text(
                "UPDATE outbox SET status = :status, entregue_em = :entregue_em "
                "WHERE mensagem_id = :id"
            ),
            {"status": status, "entregue_em": entregue_em, "id": mensagem_id},
        )
    sess.execute(
        text("UPDATE sagas SET prazo_resposta_em = :prazo WHERE ordem_id = :id"),
        {"prazo": prazo_em, "id": ordem_id},
    )
    return ordem_id


def _semear_e_comitar(
    session_factory: sessionmaker[Session], envios: Envios, **campos: object
) -> UUID:
    with session_factory() as sess:
        ordem_id = _semear(sess, envios, **campos)
        sess.commit()
    return ordem_id


def _corromper_o_ultimo_id(
    session_factory: sessionmaker[Session], ordem_id: UUID, mensagem_ids: str
) -> None:
    """Grava ``mensagem_ids`` (JSON) na saga: o dado que nao e uuid, por exemplo."""
    with session_factory() as sess:
        sess.execute(
            text(
                "UPDATE sagas SET comando_em_voo = "
                "jsonb_set(comando_em_voo, '{mensagem_ids}', CAST(:ids AS jsonb)) "
                "WHERE ordem_id = :id"
            ),
            {"ids": mensagem_ids, "id": ordem_id},
        )
        sess.commit()


# Linhas com `mensagem_ids` que nao terminam num uuid: nenhuma pode derrubar a
# consulta do ciclo nem a do gauge, e nenhuma casa com linha da outbox.
IDS_INVALIDOS = [
    pytest.param('["nao-e-uuid"]', id="texto"),
    pytest.param('[""]', id="vazio"),
    pytest.param("[]", id="lista-vazia"),
    pytest.param("[123]", id="numero"),
    pytest.param("[null]", id="nulo"),
    pytest.param('["12345678-1234-1234-1234-12345678901z"]', id="quase-uuid"),
]


def _sem_esperar_lock(sess: Session) -> Session:
    """Desiste do lock em 2 s: sem ``SKIP LOCKED`` o teste falha em vez de pendurar.

    As sessoes dos testes de lock rodam na mesma thread; uma que esperasse o lock
    de outra nao voltaria, e o timeout por sinal do pytest nao interrompe a
    chamada bloqueante do driver.
    """
    sess.execute(text("SET LOCAL lock_timeout = '2s'"))
    return sess


def _reivindicar(
    session_factory: sessionmaker[Session],
    agora: datetime = T0,
    ignorar: Sequence[UUID] = (),
) -> UUID | None:
    with session_factory() as sess:
        saga = SagaSQLAlchemyRepository(sess).reivindicar_vencida(
            agora, PRAZO, ignorar=ignorar
        )
        return saga.ordem_id if saga else None


class TestConsultaDasVencidas:
    def test_vence_com_o_envio_entregue_ha_exatamente_o_prazo(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, [_entregue(T0 - PRAZO)])

        assert _reivindicar(session_factory) == ordem_id

    @pytest.mark.parametrize(
        ("envios", "prazo_em"),
        [
            pytest.param([PENDENTE], T0 - timedelta(minutes=1), id="pendente"),
            pytest.param([MORTO], T0 - timedelta(minutes=1), id="dead"),
            # Linha que nao esta entregue nao vence, ainda que traga uma hora de
            # entrega (a condicao e o status, nao a hora): a guarda e explicita.
            pytest.param(
                [("pendente", T0 - timedelta(hours=1))],
                T0 - timedelta(minutes=1),
                id="pendente-com-hora-de-entrega",
            ),
            pytest.param(
                [("dead", T0 - timedelta(hours=1))],
                T0 - timedelta(minutes=1),
                id="dead-com-hora-de-entrega",
            ),
            pytest.param(
                [_entregue(T0 - PRAZO + timedelta(seconds=1))],
                T0 - timedelta(minutes=1),
                id="entregue-ha-menos-que-o-prazo",
            ),
            pytest.param(
                [_entregue(T0 - timedelta(hours=1))],
                T0 + timedelta(seconds=1),
                id="prazo-gravado-no-futuro",
            ),
            pytest.param(
                [_entregue(T0 - timedelta(hours=1))], None, id="saga-sem-prazo"
            ),
        ],
    )
    def test_nao_vence(
        self,
        session_factory: sessionmaker[Session],
        envios: Envios,
        prazo_em: datetime | None,
    ) -> None:
        _semear_e_comitar(session_factory, envios, prazo_em=prazo_em)

        assert _reivindicar(session_factory) is None

    @pytest.mark.parametrize(
        ("envios", "vence"),
        [
            pytest.param(
                [_entregue(T0 - timedelta(hours=1)), PENDENTE],
                False,
                id="reenvio-ainda-na-outbox",
            ),
            pytest.param(
                [_entregue(T0 - timedelta(hours=1)), MORTO],
                False,
                id="reenvio-morto",
            ),
            pytest.param(
                [MORTO, _entregue(T0 - timedelta(minutes=3))],
                True,
                id="reenvio-entregue-original-morto",
            ),
            pytest.param(
                [
                    _entregue(T0 - timedelta(hours=1)),
                    _entregue(T0 - timedelta(seconds=5)),
                ],
                False,
                id="reenvio-entregue-ha-pouco",
            ),
        ],
    )
    def test_o_ultimo_envio_e_o_que_decide(
        self, session_factory: sessionmaker[Session], envios: Envios, vence: bool
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, envios)

        assert _reivindicar(session_factory) == (ordem_id if vence else None)

    def test_a_mais_antiga_primeiro_e_as_ignoradas_ficam_de_fora(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        entregue = [_entregue(T0 - timedelta(hours=1))]
        recente = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=1)
        )
        antiga = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=3)
        )
        meio = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=2)
        )

        primeira = _reivindicar(session_factory)
        segunda = _reivindicar(session_factory, ignorar=[antiga])
        terceira = _reivindicar(session_factory, ignorar=[antiga, meio])
        nenhuma = _reivindicar(session_factory, ignorar=[antiga, meio, recente])

        assert (primeira, segunda, terceira, nenhuma) == (antiga, meio, recente, None)

    @pytest.mark.parametrize("ids", IDS_INVALIDOS)
    def test_dado_invalido_no_ultimo_envio_nao_derruba_a_consulta(
        self, session_factory: sessionmaker[Session], ids: str
    ) -> None:
        entregue = [_entregue(T0 - timedelta(hours=1))]
        estragada = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=5)
        )
        boa = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=1)
        )
        _corromper_o_ultimo_id(session_factory, estragada, ids)

        # A estragada, de prazo mais antigo, nao casa com linha e a consulta segue.
        assert _reivindicar(session_factory) == boa

    def test_o_uuid_em_maiuscula_casa_com_a_linha_da_outbox(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, [_entregue(T0 - PRAZO)])
        with session_factory() as sess:
            (ids,) = sess.execute(
                text(
                    "SELECT comando_em_voo->'mensagem_ids' FROM sagas "
                    "WHERE ordem_id = :id"
                ),
                {"id": ordem_id},
            ).one()
        _corromper_o_ultimo_id(session_factory, ordem_id, f'["{ids[-1].upper()}"]')

        assert _reivindicar(session_factory) == ordem_id

    def test_vence_com_o_prazo_gravado_exatamente_agora(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(
            session_factory, [_entregue(T0 - timedelta(hours=1))], prazo_em=T0
        )

        assert _reivindicar(session_factory) == ordem_id

    def test_a_saga_vem_carregada_e_travada_para_a_transacao(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, [_entregue(T0 - PRAZO)])

        with session_factory() as sess, session_factory() as outra:
            saga = SagaSQLAlchemyRepository(sess).reivindicar_vencida(T0, PRAZO)

            assert saga is not None
            assert (saga.etapa, saga.reenvios, saga.tipo_do_comando_em_voo) == (
                EtapaSaga.AGUARDANDO_ORCAMENTO,
                0,
                "GerarOrcamento",
            )
            assert saga.ordem_id == ordem_id
            # Travada: outra transacao nao a pega nem espera por ela.
            assert (
                SagaSQLAlchemyRepository(_sem_esperar_lock(outra)).reivindicar_vencida(
                    T0, PRAZO
                )
                is None
            )

    def test_a_travada_por_outra_transacao_e_pulada_sem_esperar(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        entregue = [_entregue(T0 - timedelta(hours=1))]
        primeira = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=2)
        )
        segunda = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=1)
        )

        with session_factory() as a, session_factory() as b, session_factory() as c:
            for sessao in (a, b, c):
                _sem_esperar_lock(sessao)
            da_a = SagaSQLAlchemyRepository(a).reivindicar_vencida(T0, PRAZO)
            da_b = SagaSQLAlchemyRepository(b).reivindicar_vencida(T0, PRAZO)
            da_c = SagaSQLAlchemyRepository(c).reivindicar_vencida(T0, PRAZO)
            assert da_a is not None
            assert da_b is not None
            assert (da_a.ordem_id, da_b.ordem_id) == (primeira, segunda)
            assert da_c is None
            a.rollback()
            # Solta a primeira: a terceira transacao a reivindica.
            _sem_esperar_lock(c)
            da_c = SagaSQLAlchemyRepository(c).reivindicar_vencida(T0, PRAZO)
            assert da_c is not None
            assert da_c.ordem_id == primeira


def _contador(nome: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(nome, labels) or 0.0


class _TransacaoNoRelogioDoTeste(TransacaoDosPrazosSQLAlchemy):
    """A transacao real, com o relogio do banco no tempo do teste (``T0``).

    O dado do teste anda no tempo dele, e o ``clock_timestamp()`` do banco de
    verdade, hoje, destoaria do ``agora`` injetado e o ciclo nao agiria.
    """

    def __init__(self, session_factory: sessionmaker[Session], agora: datetime) -> None:
        super().__init__(session_factory)
        self._agora = agora

    def agora_do_banco(self) -> datetime:
        return self._agora


def _prazos(
    session_factory: sessionmaker[Session],
    agora: datetime,
    *,
    ha_consumidor: bool = True,
    maximo: int = MAXIMO,
) -> VencerPrazos:
    return VencerPrazos(
        abrir=lambda: _TransacaoNoRelogioDoTeste(session_factory, agora),
        presenca=PresencaFake(ha_consumidor=ha_consumidor),
        contexto=ContextoDeEnvio(PRAZO, lambda: agora),
        max_reenvios=maximo,
        banco_fora=BANCO_FORA,
    )


def _linhas_da_outbox(engine: Engine, ordem_id: UUID) -> list[dict[str, object]]:
    with engine.connect() as conexao:
        linhas = conexao.execute(
            text(
                "SELECT mensagem_id, status, envelope FROM outbox "
                "WHERE correlation_id = :id ORDER BY id"
            ),
            {"id": ordem_id},
        ).all()
    return [
        {"id": str(m), "status": s, "tipo": e["tipo"], "dados": e["dados"], "e": e}
        for m, s, e in linhas
    ]


def _saga(session_factory: sessionmaker[Session], ordem_id: UUID) -> Saga:
    with session_factory() as sess:
        saga = SagaSQLAlchemyRepository(sess).obter(ordem_id)
    assert saga is not None
    return saga


class TestCicloNoBanco:
    def test_reenvio_grava_saga_e_outbox_na_mesma_transacao(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, [_entregue(T0 - PRAZO)])
        antes = _contador("pytstop_saga_reenvios_total", comando="GerarOrcamento")
        original = _linhas_da_outbox(engine, ordem_id)[-1]

        resultado = _prazos(session_factory, T0).executar()

        assert resultado == ResultadoDoCiclo(reenviadas=1)
        saga = _saga(session_factory, ordem_id)
        linhas = _linhas_da_outbox(engine, ordem_id)
        reenvio = linhas[-1]
        # Linha nova, pendente, com o mesmo tipo e os mesmos dados do original,
        # id novo e sem causa; a saga guarda os dois ids e o prazo novo.
        assert len(linhas) == 2
        assert (reenvio["status"], reenvio["tipo"], reenvio["dados"]) == (
            "pendente",
            "GerarOrcamento",
            original["dados"],
        )
        assert reenvio["e"]["causation_id"] is None
        assert reenvio["id"] != original["id"]
        em_voo = saga.comando_em_voo
        assert em_voo is not None
        assert em_voo["mensagem_ids"] == [original["id"], reenvio["id"]]
        assert (saga.reenvios, saga.prazo_resposta_em) == (1, T0 + PRAZO)
        assert saga.passos[-1]["gatilho"] == "reenvio"
        assert saga.passos[-1]["ator"] == "prazos"
        assert (
            _contador("pytstop_saga_reenvios_total", comando="GerarOrcamento")
            == antes + 1
        )

    def test_entregue_ha_menos_que_o_prazo_o_ciclo_espera_o_que_falta(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        # O relay atrasou o envio: o `prazo_resposta_em` da saga e de antes da
        # entrega, mas o participante so teve 110 s do prazo de 120 s.
        ordem_id = _semear_e_comitar(
            session_factory,
            [_entregue(T0 - PRAZO + timedelta(seconds=10))],
            prazo_em=T0 - timedelta(minutes=1),
        )

        assert _prazos(session_factory, T0).executar() == ResultadoDoCiclo()
        assert _saga(session_factory, ordem_id).reenvios == 0
        assert len(_linhas_da_outbox(engine, ordem_id)) == 1
        # Passados os 10 s que faltam, vence.
        agora = T0 + timedelta(seconds=10)
        assert _prazos(session_factory, agora).executar() == ResultadoDoCiclo(
            reenviadas=1
        )

    def test_falha_no_commit_desfaz_o_reenvio_e_nao_conta(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, [_entregue(T0 - PRAZO)])
        antes = _contador("pytstop_saga_reenvios_total", comando="GerarOrcamento")
        linhas_antes = _linhas_da_outbox(engine, ordem_id)

        # O banco recusa o commit (nao e banco fora): a saga fica de fora do ciclo
        # e a transacao desfaz tudo, o comando e a saga juntos.
        with outbox_recusando_no_commit(engine):
            resultado = _prazos(session_factory, T0).executar()

        assert resultado == ResultadoDoCiclo(recusadas=1)

        saga = _saga(session_factory, ordem_id)
        assert (saga.reenvios, saga.versao) == (0, 2)
        assert _linhas_da_outbox(engine, ordem_id) == linhas_antes
        assert (
            _contador("pytstop_saga_reenvios_total", comando="GerarOrcamento") == antes
        )
        # A saga segue candidata: o ciclo seguinte a trata.
        assert _prazos(session_factory, T0).executar() == ResultadoDoCiclo(reenviadas=1)

    def test_esgotado_em_etapa_linear_inicia_a_compensacao_na_mesma_transacao(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        envios = [_entregue(T0 - timedelta(hours=1))] * (MAXIMO + 1)
        ordem_id = _semear_e_comitar(session_factory, envios)
        esgotados = _contador(
            "pytstop_saga_prazos_esgotados_total", comando="GerarOrcamento"
        )
        compensacoes = _contador(
            "pytstop_saga_compensacoes_total", motivo="prazo_tecnico"
        )

        assert _prazos(session_factory, T0).executar() == ResultadoDoCiclo(esgotadas=1)

        saga = _saga(session_factory, ordem_id)
        assert (saga.etapa, saga.motivo, saga.reenvios) == (
            EtapaSaga.COMPENSANDO,
            "prazo_tecnico",
            0,
        )
        assert saga.plano_compensacao == ("CancelarOrcamento", "DescartarDiagnostico")
        compensacao = _linhas_da_outbox(engine, ordem_id)[-1]
        assert (compensacao["status"], compensacao["tipo"]) == (
            "pendente",
            "CancelarOrcamento",
        )
        assert compensacao["dados"]["motivo"] == "prazo_tecnico"
        assert saga.passos[-1]["gatilho"] == "prazo_esgotado"
        assert (
            _contador("pytstop_saga_prazos_esgotados_total", comando="GerarOrcamento")
            == esgotados + 1
        )
        assert (
            _contador("pytstop_saga_compensacoes_total", motivo="prazo_tecnico")
            == compensacoes + 1
        )

    def test_saga_com_dado_invalido_na_frente_nao_trava_as_outras(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        entregue = [_entregue(T0 - timedelta(hours=1))]
        estragada = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=5)
        )
        boa = _semear_e_comitar(
            session_factory, entregue, prazo_em=T0 - timedelta(minutes=1)
        )
        _corromper_o_ultimo_id(session_factory, estragada, '["nao-e-uuid"]')

        resultado = _prazos(session_factory, T0).executar()

        assert resultado == ResultadoDoCiclo(reenviadas=1)
        assert _saga(session_factory, boa).reenvios == 1
        assert _saga(session_factory, estragada).reenvios == 0

    def test_nada_vence_sem_consumidor_nem_com_o_envio_pendente(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        sem_consumidor = _semear_e_comitar(
            session_factory, [_entregue(T0 - timedelta(hours=1))]
        )
        pendente = _semear_e_comitar(session_factory, [PENDENTE])

        sem = _prazos(session_factory, T0, ha_consumidor=False).executar()
        com = _prazos(session_factory, T0).executar()

        assert sem == ResultadoDoCiclo(pausado=True)
        assert com == ResultadoDoCiclo(reenviadas=1)
        assert _saga(session_factory, pendente).reenvios == 0
        assert len(_linhas_da_outbox(engine, pendente)) == 1
        assert _saga(session_factory, sem_consumidor).reenvios == 1

    def test_o_consumidor_que_leu_antes_perde_na_versao_e_rele(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, [_entregue(T0 - PRAZO)])
        with session_factory() as consumidor:
            lida = SagaSQLAlchemyRepository(consumidor).obter(ordem_id)
            assert lida is not None

            assert _prazos(session_factory, T0).executar() == ResultadoDoCiclo(
                reenviadas=1
            )
            lida.avancar(
                evento("OrcamentoGerado", ordem_id),
                _MARCOS,
                agora=T0 + timedelta(seconds=1),
                ator="consumidor",
            )
            with pytest.raises(ConflitoDeConcorrenciaException):
                SagaSQLAlchemyRepository(consumidor).salvar(lida)
            consumidor.rollback()

            # Volta pela retry, rele a saga ja reenviada e a resposta ao comando
            # original continua valida: a etapa avanca e o prazo some.
            relida = SagaSQLAlchemyRepository(consumidor).obter(ordem_id)
            assert relida is not None
            assert relida.reenvios == 1
            relida.avancar(
                evento("OrcamentoGerado", ordem_id),
                _MARCOS,
                agora=T0 + timedelta(seconds=2),
                ator="consumidor",
            )
            SagaSQLAlchemyRepository(consumidor).salvar(relida)
            consumidor.commit()

        final = _saga(session_factory, ordem_id)
        assert (final.etapa, final.prazo_resposta_em) == (
            EtapaSaga.AGUARDANDO_DECISAO,
            None,
        )

    def test_a_saga_que_o_consumidor_esta_gravando_fica_para_ele(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        ordem_id = _semear_e_comitar(session_factory, [_entregue(T0 - PRAZO)])
        with session_factory() as consumidor:
            saga = SagaSQLAlchemyRepository(consumidor).obter(ordem_id)
            assert saga is not None
            saga.avancar(
                evento("OrcamentoGerado", ordem_id),
                _MARCOS,
                agora=T0 + timedelta(seconds=1),
                ator="consumidor",
            )
            SagaSQLAlchemyRepository(consumidor).salvar(saga)  # UPDATE sem commit

            durante = _prazos(session_factory, T0).executar()
            consumidor.commit()

        # O ciclo pulou a saga travada; com a resposta comitada, ela nao tem prazo.
        assert durante == ResultadoDoCiclo()
        assert _prazos(session_factory, T0).executar() == ResultadoDoCiclo()
        assert _saga(session_factory, ordem_id).reenvios == 0


class TestRelogioDoBanco:
    """O relogio do processo contra o ``clock_timestamp()`` do banco de verdade."""

    def test_agora_do_banco_e_o_clock_timestamp_com_fuso(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with TransacaoDosPrazosSQLAlchemy(session_factory) as transacao:
            banco = transacao.agora_do_banco()

        assert banco.tzinfo is not None
        assert abs(banco - datetime.now(UTC)) < timedelta(seconds=5)

    @pytest.mark.parametrize(
        ("desvio", "entregue_ha"),
        [
            # Adiantado 100 s: o envio entregue ha 20 s pareceria vencido.
            pytest.param(timedelta(seconds=100), timedelta(seconds=20), id="adiantado"),
            # Atrasado 100 s: a saga vencida de verdade nao seria achada.
            pytest.param(
                -timedelta(seconds=100), timedelta(seconds=200), id="atrasado"
            ),
        ],
    )
    def test_relogio_do_processo_destoando_do_banco_nao_age(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        desvio: timedelta,
        entregue_ha: timedelta,
    ) -> None:
        agora = datetime.now(UTC)
        ordem_id = _semear_e_comitar(
            session_factory,
            [_entregue(agora - entregue_ha)],
            prazo_em=agora - timedelta(seconds=80),
            enviado_em=agora - entregue_ha,
        )
        ciclo = VencerPrazos(
            abrir=lambda: TransacaoDosPrazosSQLAlchemy(session_factory),
            presenca=PresencaFake(),
            contexto=ContextoDeEnvio(PRAZO, lambda: datetime.now(UTC) + desvio),
            max_reenvios=MAXIMO,
            banco_fora=BANCO_FORA,
        )

        resultado = ciclo.executar()

        assert resultado == ResultadoDoCiclo(pausado=True)
        assert _saga(session_factory, ordem_id).reenvios == 0
        assert len(_linhas_da_outbox(engine, ordem_id)) == 1

    def test_com_o_relogio_certo_a_mesma_saga_e_reenviada(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        agora = datetime.now(UTC)
        ordem_id = _semear_e_comitar(
            session_factory,
            [_entregue(agora - timedelta(seconds=200))],
            prazo_em=agora - timedelta(seconds=80),
            enviado_em=agora - timedelta(seconds=200),
        )
        ciclo = VencerPrazos(
            abrir=lambda: TransacaoDosPrazosSQLAlchemy(session_factory),
            presenca=PresencaFake(),
            contexto=ContextoDeEnvio(PRAZO, lambda: datetime.now(UTC)),
            max_reenvios=MAXIMO,
            banco_fora=BANCO_FORA,
        )

        assert ciclo.executar() == ResultadoDoCiclo(reenviadas=1)
        assert len(_linhas_da_outbox(engine, ordem_id)) == 2


def test_duas_replicas_do_prazos_nunca_tratam_a_mesma_saga(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    entregue = [_entregue(T0 - timedelta(hours=1))]
    ordens = [_semear_e_comitar(session_factory, entregue) for _ in range(12)]
    barreira = threading.Barrier(2)
    resultados: list[ResultadoDoCiclo] = []
    erros: list[BaseException] = []

    def replica() -> None:
        try:
            barreira.wait(timeout=10)
            resultados.append(_prazos(session_factory, T0).executar())
        except BaseException as exc:  # o teste confere os erros
            erros.append(exc)

    threads = [threading.Thread(target=replica) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert erros == []
    assert not any(t.is_alive() for t in threads)
    # Cada saga reenviada uma vez, por uma das duas replicas.
    assert sum(r.reenviadas for r in resultados) == 12
    for ordem_id in ordens:
        assert _saga(session_factory, ordem_id).reenvios == 1
        assert len(_linhas_da_outbox(engine, ordem_id)) == 2


def _gauge(engine: Engine) -> float:
    registro = CollectorRegistry()
    registro.register(ColetorDaApi(engine.connect, lambda: PRAZO))
    valor = registro.get_sample_value("pytstop_saga_prazo_vencido_segundos")
    assert valor is not None
    assert registro.get_sample_value("pytstop_saga_coletor_disponivel") == 1
    return valor


class TestGaugeDoPrazoVencido:
    """``pytstop_saga_prazo_vencido_segundos``: so o que o ``prazos`` trataria."""

    def _agora(self, session_factory: sessionmaker[Session]) -> datetime:
        with session_factory() as sess:
            agora: datetime = sess.execute(text("SELECT now()")).scalar_one()
        return agora

    def test_zero_sem_saga_com_prazo(self, engine: Engine) -> None:
        assert _gauge(engine) == 0

    def test_mede_o_atraso_desde_a_entrega_mais_o_prazo(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        agora = self._agora(session_factory)
        # Entregue ha 120 s + 300 s: o prazo venceu ha 300 s.
        _semear_e_comitar(
            session_factory,
            [_entregue(agora - PRAZO - timedelta(seconds=300))],
            prazo_em=agora - timedelta(seconds=200),
        )
        # Venceu ha 60 s: nao e o maior.
        _semear_e_comitar(
            session_factory,
            [_entregue(agora - PRAZO - timedelta(seconds=60))],
            prazo_em=agora - timedelta(seconds=60),
        )

        assert 300 <= _gauge(engine) <= 330

    @pytest.mark.parametrize(
        "envios",
        [
            pytest.param([PENDENTE], id="comando-pendente-na-outbox"),
            pytest.param([MORTO], id="comando-morto"),
            pytest.param(
                [("pendente", datetime(2026, 1, 1, tzinfo=UTC))],
                id="pendente-com-hora-de-entrega",
            ),
            pytest.param(
                [("dead", datetime(2026, 1, 1, tzinfo=UTC))],
                id="dead-com-hora-de-entrega",
            ),
        ],
    )
    def test_pendente_ou_morto_nao_conta(
        self, engine: Engine, session_factory: sessionmaker[Session], envios: Envios
    ) -> None:
        agora = self._agora(session_factory)
        # A saga tem o prazo gravado vencido, mas o envio nao saiu da outbox.
        _semear_e_comitar(session_factory, envios, prazo_em=agora - timedelta(hours=1))

        assert _gauge(engine) == 0

    def test_prazo_gravado_no_futuro_ainda_nao_conta(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        # O `prazos` so reivindica a saga depois do `prazo_resposta_em` que ela
        # gravou: o gauge mede o que ele trataria, e nao mais que isso.
        agora = self._agora(session_factory)
        _semear_e_comitar(
            session_factory,
            [_entregue(agora - timedelta(hours=1))],
            prazo_em=agora + timedelta(minutes=1),
        )

        assert _gauge(engine) == 0

    @pytest.mark.parametrize("ids", IDS_INVALIDOS)
    def test_dado_invalido_no_ultimo_envio_nao_derruba_o_coletor(
        self, engine: Engine, session_factory: sessionmaker[Session], ids: str
    ) -> None:
        agora = self._agora(session_factory)
        estragada = _semear_e_comitar(
            session_factory,
            [_entregue(agora - timedelta(hours=1))],
            prazo_em=agora - timedelta(minutes=5),
        )
        _corromper_o_ultimo_id(session_factory, estragada, ids)
        _semear_e_comitar(
            session_factory,
            [_entregue(agora - PRAZO - timedelta(seconds=90))],
            prazo_em=agora - timedelta(minutes=1),
        )

        # O gauge segue disponivel (o helper confere) e mede a saga boa.
        assert 90 <= _gauge(engine) <= 120

    def test_o_ultimo_envio_decide_e_a_saga_parada_nao_conta(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        agora = self._agora(session_factory)
        longe = agora - timedelta(hours=1)
        # Reenvio ainda pendente: o original entregue ha uma hora nao vale.
        _semear_e_comitar(session_factory, [_entregue(longe), PENDENTE], prazo_em=longe)
        # Sem prazo (falha_na_compensacao): quem acusa e o gauge de etapa.
        _semear_e_comitar(session_factory, [_entregue(longe)], prazo_em=None)

        assert _gauge(engine) == 0

    def test_mede_o_atraso_mesmo_sem_broker_nem_consumidor(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        # O coletor so le o banco: com o consumidor do OS fora (e o `prazos` em
        # pausa) o alerta continua vendo o prazo vencido.
        agora = self._agora(session_factory)
        _semear_e_comitar(
            session_factory,
            [_entregue(agora - PRAZO - timedelta(seconds=90))],
            prazo_em=agora - timedelta(seconds=90),
        )

        assert 90 <= _gauge(engine) <= 120


def test_doze_minutos_com_os_padroes_ate_a_compensacao_e_a_falha_e_a_retomada(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Relogio controlado: o comando original e cinco reenvios cobrem 12 minutos.

    O ciclo roda a cada 30 s (o padrao) e o relay entrega cada comando na hora em
    que ele nasce. Ninguem responde: a compensacao comeca depois de 720 s e, ela
    tambem sem resposta, a saga para em ``falha_na_compensacao`` 720 s depois; a
    retomada a poe de volta em ``compensando``.
    """
    inicio = T0
    ordem_id = _semear_e_comitar(
        session_factory,
        [_entregue(inicio)],
        prazo_em=inicio + PRAZO,
        enviado_em=inicio,
    )
    etapas: list[tuple[int, str]] = []

    def entregar_pendentes(quando: datetime) -> None:
        with session_factory() as sess:
            sess.execute(
                text(
                    "UPDATE outbox SET status = 'entregue', entregue_em = :quando "
                    "WHERE status = 'pendente'"
                ),
                {"quando": quando},
            )
            sess.commit()

    segundo = 0
    while segundo <= 3 * 720:
        agora = inicio + timedelta(seconds=segundo)
        _prazos(session_factory, agora).executar()
        entregar_pendentes(agora)
        saga = _saga(session_factory, ordem_id)
        etapas.append((segundo, f"{saga.etapa.value}/{saga.reenvios}"))
        if saga.etapa is EtapaSaga.FALHA_NA_COMPENSACAO:
            break
        segundo += 30

    primeira_compensacao = next(s for s, e in etapas if e.startswith("compensando"))
    falha = next(s for s, e in etapas if e.startswith("falha_na_compensacao"))
    saga = _saga(session_factory, ordem_id)
    # Reenvios em 120, 240, ..., 600 s; esgotado em 720 s (um ciclo de tolerancia).
    assert 720 <= primeira_compensacao <= 720 + 30
    assert 2 * 720 <= falha <= 2 * 720 + 30
    assert (saga.falha, saga.reenvios) == ("reenvios_esgotados", MAXIMO)
    gatilhos = [p["gatilho"] for p in saga.passos]
    assert gatilhos.count("reenvio") == 2 * MAXIMO
    assert gatilhos.count("prazo_esgotado") == 2
    em_voo = saga.comando_em_voo
    assert em_voo is not None
    assert len(em_voo["mensagem_ids"]) == MAXIMO + 1

    # O operador retoma: a pendente sai de novo, com id novo, e a saga volta.
    depois = inicio + timedelta(seconds=falha + 60)
    with session_factory() as sess:
        retomar = RetomarCompensacao(
            SQLAlchemyUnitOfWork(lambda: sess),
            SagaSQLAlchemyRepository(sess),
            ContextoDeEnvio(PRAZO, lambda: depois),
        )
        dto = retomar.executar(ordem_id, ator=ATOR_ATENDENTE)
    retomada = _saga(session_factory, ordem_id)
    assert (dto.etapa, dto.falha, dto.reenvios) == ("compensando", None, 0)
    assert retomada.etapa is EtapaSaga.COMPENSANDO
    assert retomada.passos[-1]["gatilho"] == "retomada"
    ids = (retomada.comando_em_voo or {}).get("mensagem_ids", [])
    assert len(ids) == MAXIMO + 2
    assert retomada.prazo_resposta_em == depois + PRAZO
    assert _linhas_da_outbox(engine, ordem_id)[-1]["id"] == ids[-1]
