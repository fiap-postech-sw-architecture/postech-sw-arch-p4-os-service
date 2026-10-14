"""A tabela ``notificacoes`` com o PostgreSQL real: gravacao, claim e tentativas.

O claim e o da outbox com o e-mail no lugar do AMQP: lease com fencing, ordem
por OS (so entre e-mails), backoff ate ``dead`` e retencao (RFC-004 secao 7.2).
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.fila_de_notificacoes import FilaDeNotificacoes
from src.ordem_servico.infraestrutura.notificacoes_mapping import (
    NotificacoesSQLAlchemy,
)
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
)

if TYPE_CHECKING:
    from typing import Any

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integracao

_LEASE = timedelta(seconds=60)


def _ordem(session_factory: sessionmaker[Session]) -> UUID:
    with session_factory() as sessao:
        cliente = criar_cliente_com_veiculo(sessao)
        ordem = criar_ordem_recebida(
            sessao, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
        )
        sessao.commit()
        return ordem.id


def _gravar(
    session_factory: sessionmaker[Session],
    ordem_id: UUID,
    modelo: ModeloDeNotificacao = ModeloDeNotificacao.SITUACAO,
    status: StatusOrdem = StatusOrdem.EM_DIAGNOSTICO,
    motivo: str | None = None,
) -> None:
    with session_factory() as sessao:
        NotificacoesSQLAlchemy(sessao).registrar(
            Notificacao(ordem_id, modelo, status, motivo)
        )
        sessao.commit()


def _linhas(engine: Engine) -> list[Any]:
    with engine.connect() as conexao:
        return list(
            conexao.execute(
                text(
                    "SELECT id, ordem_id, modelo, status_da_ordem, motivo, situacao, "
                    "tentativas, ultimo_erro, entregue_em FROM notificacoes ORDER BY id"
                )
            ).all()
        )


def test_registrar_grava_o_pedido_pendente_sem_dado_pessoal(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _ordem(session_factory)

    _gravar(
        session_factory,
        ordem_id,
        ModeloDeNotificacao.CANCELAMENTO,
        StatusOrdem.CANCELADA,
        "orcamento_recusado",
    )

    (linha,) = _linhas(engine)
    assert (
        linha.ordem_id,
        linha.modelo,
        linha.status_da_ordem,
        linha.motivo,
        linha.situacao,
        linha.tentativas,
        linha.ultimo_erro,
        linha.entregue_em,
    ) == (
        ordem_id,
        "cancelamento",
        "cancelada",
        "orcamento_recusado",
        "pendente",
        0,
        None,
        None,
    )


def test_pedido_de_uma_transacao_desfeita_nao_existe(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _ordem(session_factory)

    with session_factory() as sessao:
        NotificacoesSQLAlchemy(sessao).registrar(
            Notificacao(ordem_id, ModeloDeNotificacao.SITUACAO, StatusOrdem.ENTREGUE)
        )
        sessao.rollback()

    assert _linhas(engine) == []


def test_pedido_de_uma_os_inexistente_e_recusado_pelo_banco(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(IntegrityError), session_factory() as sessao:
        NotificacoesSQLAlchemy(sessao).registrar(
            Notificacao(uuid4(), ModeloDeNotificacao.SITUACAO, StatusOrdem.ENTREGUE)
        )
        sessao.commit()

    assert _linhas(engine) == []


def test_reivindica_a_linha_mais_antiga_e_nao_a_entrega_de_novo_dentro_do_lease(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _ordem(session_factory)
    _gravar(session_factory, ordem_id, status=StatusOrdem.EM_DIAGNOSTICO)
    fila = FilaDeNotificacoes(engine)

    primeira = fila.reivindicar(_LEASE)

    assert primeira is not None
    assert primeira.notificacao == Notificacao(
        ordem_id, ModeloDeNotificacao.SITUACAO, StatusOrdem.EM_DIAGNOSTICO
    )
    assert fila.reivindicar(_LEASE) is None


def test_marcar_entregue_so_vale_com_o_lease_da_replica(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _ordem(session_factory)
    _gravar(session_factory, ordem_id)
    fila = FilaDeNotificacoes(engine)
    lenta = fila.reivindicar(timedelta(milliseconds=200))
    assert lenta is not None
    esperar = threading.Event()
    esperar.wait(0.4)
    outra = fila.reivindicar(_LEASE)

    assert outra is not None
    # A replica lenta perdeu a linha: nada do que ela grava vale (fencing).
    assert fila.marcar_entregue(lenta) is False
    assert fila.registrar_falha(lenta, "x") == "perdida"
    assert fila.marcar_entregue(outra) is True
    (linha,) = _linhas(engine)
    assert (linha.situacao, linha.tentativas, linha.entregue_em is not None) == (
        "entregue",
        0,
        True,
    )


def test_ordem_por_os_so_vale_entre_os_e_mails_da_mesma_os(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    os_a, os_b = _ordem(session_factory), _ordem(session_factory)
    _gravar(session_factory, os_a, status=StatusOrdem.EM_DIAGNOSTICO)
    _gravar(session_factory, os_a, status=StatusOrdem.AGUARDANDO_EXECUCAO)
    _gravar(session_factory, os_b, status=StatusOrdem.EM_DIAGNOSTICO)
    fila = FilaDeNotificacoes(engine)

    primeira = fila.reivindicar(_LEASE)
    assert primeira is not None
    assert fila.registrar_falha(primeira, "falha") == "nova_tentativa"
    # O primeiro da OS A esta em backoff: o segundo da A espera, o da B segue.
    segunda = fila.reivindicar(_LEASE)
    assert segunda is not None
    assert segunda.notificacao.ordem_id == os_b
    assert fila.reivindicar(_LEASE) is None


def test_atrasos_e_dead_na_quinta_falha_sem_bloquear_os_seguintes(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _ordem(session_factory)
    _gravar(session_factory, ordem_id, status=StatusOrdem.EM_DIAGNOSTICO)
    _gravar(session_factory, ordem_id, status=StatusOrdem.AGUARDANDO_EXECUCAO)
    # Atrasos curtos: o ciclo inteiro cabe no teste.
    fila = FilaDeNotificacoes(engine, atrasos=(0.05, 0.05, 0.05, 0.05))
    desfechos = []
    for _ in range(5):
        linha = None
        for _ in range(100):
            linha = fila.reivindicar(_LEASE)
            if linha is not None:
                break
            threading.Event().wait(0.02)
        assert linha is not None
        assert linha.notificacao.status is StatusOrdem.EM_DIAGNOSTICO
        desfechos.append(fila.registrar_falha(linha, "falha ao enviar o e-mail (X)"))

    assert desfechos == ["nova_tentativa"] * 4 + ["dead"]
    primeira, segunda = _linhas(engine)
    assert (primeira.situacao, primeira.tentativas, primeira.ultimo_erro) == (
        "dead",
        5,
        "falha ao enviar o e-mail (X)",
    )
    # `dead` nao bloqueia: o seguinte da mesma OS sai.
    seguinte = fila.reivindicar(_LEASE)
    assert seguinte is not None
    assert seguinte.notificacao.status is StatusOrdem.AGUARDANDO_EXECUCAO
    assert segunda.situacao == "pendente"


def test_pedido_de_modelo_desconhecido_espera_o_notificador_da_versao_nova(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    os_a, os_b = _ordem(session_factory), _ordem(session_factory)
    _gravar(session_factory, os_a)
    _gravar(session_factory, os_a, status=StatusOrdem.AGUARDANDO_EXECUCAO)
    _gravar(session_factory, os_b)
    with engine.begin() as conexao:
        # Um modelo que so a versao seguinte conhece, no primeiro pedido da OS A.
        conexao.execute(
            text(
                "UPDATE notificacoes SET modelo = 'futuro' "
                "WHERE ordem_id = :id AND status_da_ordem = 'em_diagnostico'"
            ),
            {"id": os_a},
        )
    fila = FilaDeNotificacoes(engine)

    linha = fila.reivindicar(_LEASE)

    # A versao antiga nao quebra nem toma o pedido: o da OS B segue, e o seguinte
    # da OS A espera o que ficou pendente (a ordem por OS).
    assert linha is not None
    assert linha.notificacao.ordem_id == os_b
    assert fila.reivindicar(_LEASE) is None
    assert sorted(linha.situacao for linha in _linhas(engine)) == ["pendente"] * 3


def test_linha_travada_por_outra_replica_nao_trava_o_claim(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    os_a, os_b = _ordem(session_factory), _ordem(session_factory)
    _gravar(session_factory, os_a)
    _gravar(session_factory, os_b)
    reivindicadas: list[Any] = []

    with engine.connect() as outra, outra.begin():
        # Outra replica no meio do claim da linha mais antiga.
        outra.execute(
            text("SELECT id FROM notificacoes ORDER BY id LIMIT 1 FOR UPDATE")
        )
        claim = threading.Thread(
            target=lambda: reivindicadas.append(
                FilaDeNotificacoes(engine).reivindicar(_LEASE)
            )
        )
        claim.start()
        claim.join(timeout=10)
        # Sem o SKIP LOCKED o claim esperaria o lock da outra replica.
        travou = claim.is_alive()
    claim.join(timeout=10)

    assert not travou
    (linha,) = reivindicadas
    assert linha is not None
    assert linha.notificacao.ordem_id == os_b


def test_duas_replicas_nunca_pegam_a_mesma_linha(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordens = [_ordem(session_factory) for _ in range(6)]
    for ordem_id in ordens:
        _gravar(session_factory, ordem_id)
    fila = FilaDeNotificacoes(engine)
    pegas: list[int] = []
    trava = threading.Lock()
    largada = threading.Barrier(2)

    def replica() -> None:
        largada.wait()
        while (linha := fila.reivindicar(_LEASE)) is not None:
            with trava:
                pegas.append(linha.id)

    threads = [threading.Thread(target=replica) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(pegas) == sorted(set(pegas))
    assert len(pegas) == 6


def test_limpeza_apaga_so_as_entregues_antigas_e_as_dead_antigas(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ordem_id = _ordem(session_factory)
    for _ in range(6):
        _gravar(session_factory, ordem_id)
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "UPDATE notificacoes SET situacao = 'entregue', "
                "entregue_em = now() - interval '8 days' WHERE id % 6 = 1"
            )
        )
        conexao.execute(
            text(
                "UPDATE notificacoes SET situacao = 'entregue', "
                "entregue_em = now() - interval '6 days' WHERE id % 6 = 2"
            )
        )
        conexao.execute(
            text(
                "UPDATE notificacoes SET situacao = 'dead', "
                "proxima_tentativa_em = now() - interval '31 days' WHERE id % 6 = 3"
            )
        )
        conexao.execute(
            text(
                "UPDATE notificacoes SET situacao = 'dead', "
                "proxima_tentativa_em = now() - interval '29 days' WHERE id % 6 = 4"
            )
        )
        conexao.execute(
            text(
                "UPDATE notificacoes SET "
                "proxima_tentativa_em = now() - interval '40 days' WHERE id % 6 = 5"
            )
        )

    apagadas = FilaDeNotificacoes(engine).limpar(entre_lotes=lambda: None)

    assert apagadas == 2
    restantes = sorted((linha.id % 6, linha.situacao) for linha in _linhas(engine))
    assert restantes == [
        (0, "pendente"),
        (2, "entregue"),
        (4, "dead"),
        (5, "pendente"),
    ]
