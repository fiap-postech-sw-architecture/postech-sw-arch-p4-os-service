"""Outbox transacional: so comandos, gravados pela UoW no commit do efeito."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text

from src.compartilhado.aplicacao.mensageria import ContratoInvalidoError
from src.compartilhado.infraestrutura import outbox_mapping
from src.compartilhado.infraestrutura.mensageria.contratos import (
    CONTRATOS,
    catalogo,
)
from src.compartilhado.infraestrutura.outbox_mapping import (
    apagar_em_lotes,
    registrar_processada,
)
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.integracao.seed_helpers import criar_cliente_com_veiculo
from tests.rastreamento import traceparent

if TYPE_CHECKING:
    from typing import Any

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

    from tests.rastreamento import Rastreador


def _dados_de_solicitar_diagnostico(ordem_id: object) -> dict[str, Any]:
    exemplo = json.loads((CONTRATOS / "exemplos/SolicitarDiagnostico.json").read_text())
    dados: dict[str, Any] = exemplo["dados"]
    return {**dados, "ordem_id": ordem_id}


def _abrir(session: Session) -> OrdemDeServico:
    cliente = criar_cliente_com_veiculo(session)
    return OrdemDeServico.abrir(
        cliente_id=cliente.id,
        veiculo_id=cliente.veiculos[0].id,
        descricao_problema="Pneu careca",
    )


def _linhas(session: Session) -> list[Any]:
    return list(
        session.execute(
            text(
                "SELECT mensagem_id, correlation_id, exchange, routing_key, envelope, "
                "traceparent, tracestate, status, tentativas FROM outbox ORDER BY id"
            )
        ).all()
    )


def test_tabelas_da_mensageria_existem(session: Session) -> None:
    nomes = set(inspect(session.connection()).get_table_names())
    assert {"outbox", "mensagens_processadas"} <= nomes


def test_eventos_internos_da_os_nao_vao_para_a_outbox(session: Session) -> None:
    # O OS so publica comandos (RFC-004 secao 5.3): abertura e transicao ficam
    # no historico, nao no broker.
    repo = OrdemDeServicoSQLAlchemyRepository(session=session)
    ordem = _abrir(session)

    with SQLAlchemyUnitOfWork(session_factory=lambda: session) as uow:
        repo.salvar(ordem)
        ordem.registrar_diagnostico_iniciado()
        repo.salvar(ordem)
        uow.commit()

    assert _linhas(session) == []


def test_publicar_comando_grava_o_envelope_no_mesmo_commit(session: Session) -> None:
    repo = OrdemDeServicoSQLAlchemyRepository(session=session)
    ordem = _abrir(session)
    causa = uuid4()

    with SQLAlchemyUnitOfWork(session_factory=lambda: session) as uow:
        repo.salvar(ordem)
        mensagem_id = uow.publicar_comando(
            "SolicitarDiagnostico",
            _dados_de_solicitar_diagnostico(ordem.id),
            correlation_id=ordem.id,
            causation_id=causa,
        )
        uow.commit()

    (linha,) = _linhas(session)
    assert linha.mensagem_id == mensagem_id
    assert linha.correlation_id == ordem.id
    assert (linha.exchange, linha.routing_key) == (
        "pytstop.comandos",
        "comando.execucao.solicitar_diagnostico",
    )
    assert (linha.status, linha.tentativas) == ("pendente", 0)
    # Fora de um span nao ha contexto a propagar: o relay abre um trace novo.
    assert (linha.traceparent, linha.tracestate) == (None, None)
    envelope = linha.envelope
    catalogo().validar(envelope)
    assert envelope["id"] == str(mensagem_id)
    assert envelope["tipo"] == "SolicitarDiagnostico"
    assert envelope["versao"] == 1
    assert envelope["origem"] == "os-service"
    assert envelope["correlation_id"] == str(ordem.id)
    assert envelope["causation_id"] == str(causa)
    assert envelope["dados"]["ordem_id"] == str(ordem.id)


def test_publicar_comando_grava_o_contexto_do_span_corrente(
    session: Session, rastreador: Rastreador
) -> None:
    ordem_id = uuid4()

    with (
        rastreador.tracer.start_as_current_span("POST /ordens-de-servico"),
        SQLAlchemyUnitOfWork(session_factory=lambda: session) as uow,
    ):
        uow.publicar_comando(
            "SolicitarDiagnostico",
            _dados_de_solicitar_diagnostico(ordem_id),
            correlation_id=ordem_id,
        )
        uow.commit()

    (span,) = rastreador.spans()
    (linha,) = _linhas(session)
    assert linha.traceparent == traceparent(span)
    assert linha.envelope["causation_id"] is None


def test_rollback_descarta_o_comando(session: Session) -> None:
    ordem_id = uuid4()

    with (
        pytest.raises(RuntimeError, match="falhou depois de publicar"),
        SQLAlchemyUnitOfWork(session_factory=lambda: session) as uow,
    ):
        uow.publicar_comando(
            "SolicitarDiagnostico",
            _dados_de_solicitar_diagnostico(ordem_id),
            correlation_id=ordem_id,
        )
        raise RuntimeError("falhou depois de publicar")

    assert _linhas(session) == []


@pytest.mark.parametrize(
    ("tipo", "dados", "motivo"),
    [
        pytest.param(
            "DiagnosticoIniciado",
            {"ordem_id": str(uuid4())},
            "tipo que o OS Service nao publica",
            id="evento-de-outro-servico",
        ),
        pytest.param(
            "SolicitarDiagnostico",
            {"ordem_id": str(uuid4())},
            "envelope fora do contrato",
            id="dados-incompletos",
        ),
    ],
)
def test_comando_fora_do_contrato_e_recusado_sem_gravar(
    session: Session, tipo: str, dados: dict[str, Any], motivo: str
) -> None:
    with (
        SQLAlchemyUnitOfWork(session_factory=lambda: session) as uow,
        pytest.raises(ContratoInvalidoError) as erro,
    ):
        uow.publicar_comando(tipo, dados, correlation_id=uuid4())

    assert erro.value.motivo == motivo
    assert _linhas(session) == []


def test_mensagem_processada_so_e_registrada_uma_vez(session: Session) -> None:
    mensagem_id = uuid4()

    assert registrar_processada(session, mensagem_id) is True
    assert registrar_processada(session, mensagem_id) is False
    total = session.execute(
        text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
        {"id": mensagem_id},
    ).scalar_one()
    assert total == 1


def _processadas_antigas(engine: Engine) -> int:
    with engine.connect() as conexao:
        total: int = conexao.execute(
            text(
                "SELECT count(*) FROM mensagens_processadas "
                "WHERE processada_em < now() - interval '30 days'"
            )
        ).scalar_one()
    return total


@pytest.mark.usefixtures("session_factory")
def test_limpeza_apaga_em_lotes_e_atende_o_broker_entre_eles(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_mapping, "LOTE_DE_LIMPEZA", 2)
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "INSERT INTO mensagens_processadas (mensagem_id, processada_em) "
                "SELECT gen_random_uuid(), now() - interval '31 days' "
                "FROM generate_series(1, 5)"
            )
        )
    restantes: list[int] = []
    limpeza = text(
        "DELETE FROM mensagens_processadas WHERE mensagem_id IN ("
        "SELECT mensagem_id FROM mensagens_processadas "
        "WHERE processada_em < now() - interval '30 days' LIMIT :lote)"
    )

    apagadas = apagar_em_lotes(
        engine.begin,
        limpeza,
        entre_lotes=lambda: restantes.append(_processadas_antigas(engine)),
    )

    # Tres lotes (2, 2 e 1), um commit cada, e o broker atendido entre eles.
    assert apagadas == 5
    assert restantes == [3, 1]


@pytest.mark.parametrize(
    ("tracestate", "gravado"),
    [
        # Membros de ate 256 caracteres no valor (limite do W3C).
        pytest.param("a1=" + "x" * 253 + ",a2=" + "x" * 252, True, id="512"),
        pytest.param("a1=" + "x" * 253 + ",a2=" + "x" * 253, False, id="513"),
        pytest.param(
            ",".join(f"v{i}=" + "x" * 200 for i in range(10)), False, id="2040"
        ),
    ],
)
def test_tracestate_de_ate_512_caracteres_entra_na_outbox_e_o_maior_fica_fora(
    session: Session, rastreador: Rastreador, tracestate: str, gravado: bool
) -> None:
    # O contexto da requisicao HTTP vem do propagador do SDK, sem teto: acima
    # de 512 o W3C deixa descartar o tracestate, e o contexto segue pelo
    # traceparent.
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    pai = TraceContextTextMapPropagator().extract(
        {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": tracestate,
        }
    )
    ordem_id = uuid4()

    with (
        rastreador.tracer.start_as_current_span("POST", context=pai),
        SQLAlchemyUnitOfWork(session_factory=lambda: session) as uow,
    ):
        uow.publicar_comando(
            "SolicitarDiagnostico",
            _dados_de_solicitar_diagnostico(ordem_id),
            correlation_id=ordem_id,
        )
        uow.commit()

    (linha,) = _linhas(session)
    assert linha.traceparent.split("-")[1] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert linha.tracestate == (tracestate if gravado else None)
