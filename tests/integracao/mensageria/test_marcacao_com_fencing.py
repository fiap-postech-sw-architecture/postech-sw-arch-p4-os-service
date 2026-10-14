"""A marcacao com fencing, vista pela outbox e pela fila de e-mails.

As duas filas usam o mesmo ``MarcacaoComFencing``: cada teste roda nas duas. O
fencing so grava com a linha ``pendente`` e com o lease que a replica gravou; o
desfecho ``entregue`` limpa o ``ultimo_erro`` da tentativa anterior.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS
from src.compartilhado.infraestrutura.mensageria.outbox import Outbox
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.fila_de_notificacoes import FilaDeNotificacoes
from src.ordem_servico.infraestrutura.notificacoes_mapping import (
    NotificacoesSQLAlchemy,
)
from tests.integracao.broker import esperar_ate
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    criar_ordem_recebida,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integracao

_LEASE = timedelta(seconds=60)
# Atrasos curtos: a nova tentativa fica vencida em instantes.
_ATRASOS = (0.05, 0.05, 0.05, 0.05)


class Alvo(Protocol):
    """O que o teste precisa de cada fila."""

    def gravar(self) -> None: ...

    def reivindicar(self) -> Any: ...  # a linha de cada fila

    def marcar_entregue(self, linha: Any) -> bool: ...

    def registrar_falha(self, linha: Any, motivo: str) -> str: ...

    def levar_a(self, tentativas: int) -> None: ...

    def estado(self) -> tuple[str, int, str | None]: ...


class AlvoOutbox:
    def __init__(self, engine: Engine, session_factory: sessionmaker[Session]) -> None:
        self._engine = engine
        self._session_factory = session_factory
        self._outbox = Outbox(engine, atrasos=_ATRASOS)

    def gravar(self) -> None:
        exemplo = json.loads(
            (CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text()
        )
        ordem_id = uuid4()
        with SQLAlchemyUnitOfWork(self._session_factory) as uow:
            uow.publicar_comando(
                Comando.SOLICITAR_DIAGNOSTICO,
                {**exemplo["dados"], "ordem_id": ordem_id},
                correlation_id=ordem_id,
            )
            uow.commit()

    def reivindicar(self) -> Any:
        linhas = self._outbox.reivindicar(1, _LEASE)
        return linhas[0] if linhas else None

    def marcar_entregue(self, linha: Any) -> bool:
        return self._outbox.marcar_entregue(linha)

    def registrar_falha(self, linha: Any, motivo: str) -> str:
        return self._outbox.registrar_falha(linha, motivo)

    def levar_a(self, tentativas: int) -> None:
        with self._engine.begin() as conexao:
            conexao.execute(
                text("UPDATE outbox SET tentativas = :n"), {"n": tentativas}
            )

    def estado(self) -> tuple[str, int, str | None]:
        with self._engine.connect() as conexao:
            linha = conexao.execute(
                text("SELECT status, tentativas, ultimo_erro FROM outbox")
            ).one()
        return (linha.status, linha.tentativas, linha.ultimo_erro)


class AlvoNotificacoes:
    def __init__(self, engine: Engine, session_factory: sessionmaker[Session]) -> None:
        self._engine = engine
        self._session_factory = session_factory
        self._fila = FilaDeNotificacoes(engine, atrasos=_ATRASOS)

    def gravar(self) -> None:
        with self._session_factory() as sessao:
            cliente = criar_cliente_com_veiculo(sessao)
            ordem = criar_ordem_recebida(
                sessao, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
            )
            sessao.commit()
            NotificacoesSQLAlchemy(sessao).registrar(
                Notificacao(
                    ordem.id, ModeloDeNotificacao.SITUACAO, StatusOrdem.EM_DIAGNOSTICO
                )
            )
            sessao.commit()

    def reivindicar(self) -> Any:
        return self._fila.reivindicar(_LEASE)

    def marcar_entregue(self, linha: Any) -> bool:
        return self._fila.marcar_entregue(linha)

    def registrar_falha(self, linha: Any, motivo: str) -> str:
        return self._fila.registrar_falha(linha, motivo)

    def levar_a(self, tentativas: int) -> None:
        with self._engine.begin() as conexao:
            conexao.execute(
                text("UPDATE notificacoes SET tentativas = :n"), {"n": tentativas}
            )

    def estado(self) -> tuple[str, int, str | None]:
        with self._engine.connect() as conexao:
            linha = conexao.execute(
                text("SELECT situacao, tentativas, ultimo_erro FROM notificacoes")
            ).one()
        return (linha.situacao, linha.tentativas, linha.ultimo_erro)


@pytest.fixture(params=["outbox", "notificacoes"])
def alvo(
    request: pytest.FixtureRequest,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Alvo:
    fila: Alvo = (
        AlvoOutbox(engine, session_factory)
        if request.param == "outbox"
        else AlvoNotificacoes(engine, session_factory)
    )
    fila.gravar()
    return fila


def test_entregue_so_vale_uma_vez(alvo: Alvo) -> None:
    linha = alvo.reivindicar()

    assert alvo.marcar_entregue(linha) is True
    # Com o mesmo token, mas sem a linha `pendente`: nada a gravar.
    assert alvo.marcar_entregue(linha) is False
    assert alvo.estado() == ("entregue", 0, None)


@pytest.mark.parametrize(
    "tentativas",
    [pytest.param(0, id="nova-tentativa"), pytest.param(4, id="dead")],
)
def test_falha_depois_de_entregue_com_o_mesmo_token_e_perdida(
    alvo: Alvo, tentativas: int
) -> None:
    alvo.levar_a(tentativas)
    linha = alvo.reivindicar()
    assert alvo.marcar_entregue(linha) is True

    # O token (o fim do lease) segue igual, mas a linha ja nao e `pendente`: a
    # falha de outra copia nao reabre nem mata uma linha entregue.
    assert alvo.registrar_falha(linha, "falha tardia") == "perdida"
    assert alvo.estado() == ("entregue", tentativas, None)


def test_entrega_depois_de_uma_falha_limpa_o_ultimo_erro(alvo: Alvo) -> None:
    primeira = alvo.reivindicar()
    assert alvo.registrar_falha(primeira, "falha ao enviar") == "nova_tentativa"
    assert alvo.estado() == ("pendente", 1, "falha ao enviar")

    # O atraso e curto: a linha volta a ser reivindicada com outro lease.
    segunda = esperar_ate(alvo.reivindicar, prazo_s=10)
    assert alvo.marcar_entregue(segunda) is True

    assert alvo.estado() == ("entregue", 1, None)
