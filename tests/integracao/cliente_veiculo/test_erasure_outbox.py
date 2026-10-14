"""O erasure LGPD alcanca a outbox e avisa a Execucao (RFC-004 secoes 5.3 e 8).

Num commit so: o cliente e os veiculos anonimizados, o texto livre das OS, a
placa e a descricao que ficaram nos comandos da outbox e um ``AnonimizarVeiculo``
por veiculo. A linha anonimizada continua valida no contrato, entao o relay a
publica como qualquer outra.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from sqlalchemy import text

from src.cliente_veiculo.dominio.exceptions import ClienteNaoEncontradoException
from src.cliente_veiculo.infraestrutura.adapters import (
    DESCRICAO_NA_OUTBOX,
    PLACA_NA_OUTBOX,
)
from src.cliente_veiculo.interfaces.dependencies import obter_excluir_dados
from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException
from src.compartilhado.dominio.placa import Placa
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, catalogo
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.interfaces.dependencies import obter_abrir_ordem
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.seed_helpers import (
    criar_cliente_com_veiculo,
    outbox_recusando_no_commit,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

pytestmark = pytest.mark.integracao

_PLACA = "QZX7W42"
_SQL_ORCAMENTO = text(
    "SELECT envelope FROM outbox WHERE envelope ->> 'tipo' = 'GerarOrcamento'"
)
_DESCRICAO = "Barulho estranho vindo do motor"
_ATENDENTE = "4c2f8f8e-1b8e-4d5a-9a9e-3f1d2c7b6a55"


class Titular:
    """Um cliente com dois veiculos e uma OS aberta pelo caso de uso real."""

    def __init__(self, session_factory: sessionmaker[Session], placa: str) -> None:
        with session_factory() as sessao:
            cliente = criar_cliente_com_veiculo(sessao, placa=placa)
            cliente.adicionar_veiculo(
                placa=Placa(valor=f"{placa[:3]}8H99"),
                marca="VW",
                modelo="Gol",
                ano=2018,
            )
            sessao.commit()
            self.cliente_id: UUID = cliente.id
            self.veiculos: list[UUID] = [v.id for v in cliente.veiculos]
        with session_factory() as sessao:
            self.ordem_id: UUID = (
                obter_abrir_ordem(sessao)
                .executar(
                    AbrirOrdemDTO(
                        cliente_id=self.cliente_id,
                        veiculo_id=self.veiculos[0],
                        descricao_problema=_DESCRICAO,
                        ator=_ATENDENTE,
                    )
                )
                .id
            )


def _encerrar(engine: Engine, ordem_id: UUID) -> None:
    """A OS sai do estado ativo (o erasure so vale sem OS ativa)."""
    with engine.begin() as conexao:
        conexao.execute(
            text("UPDATE ordens_de_servico SET status = 'cancelada' WHERE id = :id"),
            {"id": ordem_id},
        )


def _envelope_do_diagnostico(engine: Engine, ordem_id: UUID) -> dict[str, Any]:
    with engine.connect() as conexao:
        envelope: dict[str, Any] = conexao.execute(
            text(
                "SELECT envelope FROM outbox WHERE correlation_id = :id "
                "AND envelope ->> 'tipo' = 'SolicitarDiagnostico'"
            ),
            {"id": ordem_id},
        ).scalar_one()
    return envelope


def _excluir(session_factory: sessionmaker[Session], cliente_id: UUID) -> None:
    with session_factory() as sessao:
        obter_excluir_dados(sessao).executar(cliente_id)


def _comandos_de_anonimizacao(engine: Engine) -> list[Any]:
    with engine.connect() as conexao:
        return list(
            conexao.execute(
                text(
                    "SELECT exchange, routing_key, correlation_id, envelope "
                    "FROM outbox WHERE envelope ->> 'tipo' = 'AnonimizarVeiculo' "
                    "ORDER BY id"
                )
            ).all()
        )


def test_um_anonimizar_veiculo_por_veiculo_na_transacao_do_erasure(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    titular = Titular(session_factory, _PLACA)
    _encerrar(engine, titular.ordem_id)

    _excluir(session_factory, titular.cliente_id)

    comandos = _comandos_de_anonimizacao(engine)
    assert sorted(c.correlation_id for c in comandos) == sorted(titular.veiculos)
    for comando in comandos:
        assert (comando.exchange, comando.routing_key) == (
            "pytstop.comandos",
            "comando.execucao.anonimizar_veiculo",
        )
        envelope = comando.envelope
        catalogo().validar(envelope)
        # Fora da saga: o veiculo e o correlation_id, sem causa e sem placa.
        assert envelope["causation_id"] is None
        assert envelope["dados"] == {"veiculo_id": str(comando.correlation_id)}
        assert envelope["origem"] == "os-service"
        assert _PLACA not in json.dumps(envelope)


def test_erasure_recusado_nao_envia_comando_nem_toca_a_outbox(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    titular = Titular(session_factory, _PLACA)  # OS ativa: o erasure e recusado

    with pytest.raises(ViolacaoRegraDeNegocioException):
        _excluir(session_factory, titular.cliente_id)

    assert _comandos_de_anonimizacao(engine) == []
    dados = _envelope_do_diagnostico(engine, titular.ordem_id)["dados"]
    assert (dados["veiculo"]["placa"], dados["descricao_problema"]) == (
        _PLACA,
        _DESCRICAO,
    )


def test_cliente_inexistente_nao_envia_comando(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ClienteNaoEncontradoException):
        _excluir(session_factory, UUID(int=7))

    assert _comandos_de_anonimizacao(engine) == []


def test_falha_no_commit_desfaz_comandos_cliente_e_outbox(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    titular = Titular(session_factory, _PLACA)
    _encerrar(engine, titular.ordem_id)

    with outbox_recusando_no_commit(engine), pytest.raises(Exception, match="outbox"):
        _excluir(session_factory, titular.cliente_id)

    # Nada saiu pela metade: nenhum comando, a placa original na outbox e o
    # cliente ainda ativo.
    assert _comandos_de_anonimizacao(engine) == []
    dados = _envelope_do_diagnostico(engine, titular.ordem_id)["dados"]
    assert dados["veiculo"]["placa"] == _PLACA
    with engine.connect() as conexao:
        ativo = conexao.execute(
            text("SELECT ativo FROM clientes WHERE id = :id"),
            {"id": titular.cliente_id},
        ).scalar_one()
    assert ativo is True


@pytest.mark.parametrize("situacao", ["pendente", "entregue", "dead"])
def test_outbox_do_cliente_perde_a_placa_e_a_descricao_em_qualquer_situacao(
    engine: Engine, session_factory: sessionmaker[Session], situacao: str
) -> None:
    titular = Titular(session_factory, _PLACA)
    _encerrar(engine, titular.ordem_id)
    with engine.begin() as conexao:
        conexao.execute(
            text("UPDATE outbox SET status = :s WHERE correlation_id = :id"),
            {"s": situacao, "id": titular.ordem_id},
        )

    _excluir(session_factory, titular.cliente_id)

    envelope = _envelope_do_diagnostico(engine, titular.ordem_id)
    dados = envelope["dados"]
    assert dados["veiculo"]["placa"] == PLACA_NA_OUTBOX == "AAA0A00"
    assert dados["descricao_problema"] == DESCRICAO_NA_OUTBOX == "[removido]"
    # Marca, modelo e ano nao identificam o titular e ficam; os ids tambem.
    assert (dados["veiculo"]["marca"], dados["veiculo"]["modelo"]) == ("Fiat", "Uno")
    assert dados["ordem_id"] == str(titular.ordem_id)
    # O marcador respeita o schema: o relay confere o envelope antes de publicar.
    catalogo().validar(envelope)
    with engine.connect() as conexao:
        status = conexao.execute(
            text("SELECT status FROM outbox WHERE correlation_id = :id"),
            {"id": titular.ordem_id},
        ).scalar_one()
    assert status == situacao


def test_comando_de_outro_tipo_da_mesma_os_nao_e_tocado(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    titular = Titular(session_factory, _PLACA)
    exemplo = json.loads((CONTRATOS / "exemplos/GerarOrcamento.json").read_text())
    with SQLAlchemyUnitOfWork(session_factory) as uow:
        uow.publicar_comando(
            "GerarOrcamento",
            {**exemplo["dados"], "ordem_id": titular.ordem_id},
            correlation_id=titular.ordem_id,
        )
        uow.commit()
    _encerrar(engine, titular.ordem_id)
    with engine.connect() as conexao:
        antes = conexao.execute(_SQL_ORCAMENTO).scalar_one()

    _excluir(session_factory, titular.cliente_id)

    with engine.connect() as conexao:
        depois = conexao.execute(_SQL_ORCAMENTO).scalar_one()
    # Sem placa nem descricao a trocar: nada ganha campo novo.
    assert depois == antes
    catalogo().validar(depois)


def test_outbox_de_outro_cliente_nao_e_tocada(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    alvo = Titular(session_factory, _PLACA)
    outro = Titular(session_factory, "KLM1A23")
    _encerrar(engine, alvo.ordem_id)

    _excluir(session_factory, alvo.cliente_id)

    dados = _envelope_do_diagnostico(engine, outro.ordem_id)["dados"]
    assert (dados["veiculo"]["placa"], dados["descricao_problema"]) == (
        "KLM1A23",
        _DESCRICAO,
    )
    # Os comandos saem so dos veiculos do cliente apagado.
    assert {c.correlation_id for c in _comandos_de_anonimizacao(engine)} == set(
        alvo.veiculos
    )


def test_relay_publica_a_linha_anonimizada_e_os_comandos_de_anonimizacao(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    titular = Titular(session_factory, _PLACA)
    _encerrar(engine, titular.ordem_id)
    # A linha do diagnostico segue pendente (relay parado): o erasure a alcanca.
    _excluir(session_factory, titular.cliente_id)
    relay = Relay(
        engine=engine,
        parametros=broker.parametros("os"),
        tracer=rastreador.tracer,
        config=ConfigRelay(poll_s=0.1, diretorio_de_saude=tmp_path),
    )

    with EmSegundoPlano(relay):
        esperar_ate(lambda: broker.contar("execucao.comandos") == 3)
        publicadas = [
            json.loads(corpo) for _, corpo in broker.pegar_todas("execucao.comandos")
        ]

    (diagnostico,) = [e for e in publicadas if e["tipo"] == "SolicitarDiagnostico"]
    assert diagnostico["dados"]["veiculo"]["placa"] == "AAA0A00"
    assert diagnostico["dados"]["descricao_problema"] == "[removido]"
    anonimizacoes = [e for e in publicadas if e["tipo"] == "AnonimizarVeiculo"]
    assert sorted(e["dados"]["veiculo_id"] for e in anonimizacoes) == sorted(
        str(v) for v in titular.veiculos
    )
    with engine.connect() as conexao:
        assert (
            conexao.execute(
                text("SELECT count(*) FROM outbox WHERE status <> 'entregue'")
            ).scalar_one()
            == 0
        )
