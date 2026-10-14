"""E-mail ao cliente de ponta a ponta: saga, outbox, PostgreSQL, RabbitMQ e Mailpit.

Relay, consumidor e notificador reais em threads; os participantes respondem pelo
broker. Um e-mail por mudanca de situacao, o texto do orcamento e do pagamento
com os links que o Billing mandou, o cancelamento com o rotulo do motivo e nada
de dado pessoal nem link no log, nos spans e nas metricas. SMTP fora nao atrasa
comando nem resposta da saga (RFC-004 secao 7.2; ADR-036).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from prometheus_client import generate_latest
from sqlalchemy import text

from src.ordem_servico.infraestrutura.email_adapter import SmtpEmailAdapter
from src.ordem_servico.infraestrutura.notificador import (
    ConfigNotificador,
    Notificador,
)
from src.ordem_servico.interfaces.dependencies import (
    obter_cancelar_ordem,
    obter_registrar_entrega,
)
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.mensageria.test_saga_com_broker import (
    Atendimento,
    _consumidor,
    _relay,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.integracao.mailpit import Mailpit
    from tests.rastreamento import Rastreador, Saidas

pytestmark = pytest.mark.integracao

_ATENDENTE = "4c2f8f8e-1b8e-4d5a-9a9e-3f1d2c7b6a55"
_EMAIL = "maria.silva@cliente.com"
_CONTATO = f"Maria Silva - {_EMAIL} / (11) 99999-0000"
_ATRASOS = (0.05, 0.05, 0.05, 0.05)


def _exemplo(tipo: str) -> dict[str, Any]:
    from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS

    dados: dict[str, Any] = json.loads(
        (CONTRATOS / "exemplos" / f"{tipo}.json").read_text()
    )["dados"]
    return dados


def _dar_email_ao_cliente(engine: Engine, ordem_id: Any) -> None:
    """O cliente do cenario nasce so com telefone: aqui ganha o e-mail."""
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "UPDATE clientes SET nome = 'Maria Silva', contato = :contato "
                "WHERE id = (SELECT cliente_id FROM ordens_de_servico WHERE id = :id)"
            ),
            {"contato": _CONTATO, "id": ordem_id},
        )


def _notificador(engine: Engine, pasta: Path, *, host: str, porta: int) -> Notificador:
    return Notificador(
        engine=engine,
        email=SmtpEmailAdapter(host, porta, "oficina@pytstop.local"),
        config=ConfigNotificador(
            poll_s=0.1, atrasos_s=_ATRASOS, diretorio_de_saude=pasta
        ),
    )


@pytest.fixture
def processos(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> Iterator[Atendimento]:
    """Relay e consumidor reais; o notificador cada teste liga com o SMTP dele."""
    relay = _relay(engine, broker, rastreador, tmp_path)
    consumidor = _consumidor(session_factory, broker, rastreador, tmp_path)
    with EmSegundoPlano(relay), EmSegundoPlano(consumidor):
        esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
        yield Atendimento(engine, session_factory, broker, rastreador)


def _assuntos(mailpit: Mailpit, quantidade: int) -> list[str]:
    """Os assuntos, na ordem em que os e-mails chegaram ao Mailpit."""
    mensagens = sorted(
        mailpit.esperar(quantidade, prazo_s=30), key=lambda m: m["Created"]
    )
    return [m["Subject"].split(": ", 1)[1] for m in mensagens]


def test_um_email_por_mudanca_de_situacao_do_abrir_ate_a_entrega(
    processos: Atendimento,
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    rastreador: Rastreador,
    saidas: Saidas,
    tmp_path: Path,
) -> None:
    ordem_id = processos.abrir()
    _dar_email_ao_cliente(engine, ordem_id)
    notificador = _notificador(
        engine, tmp_path, host="127.0.0.1", porta=mailpit.porta_smtp
    )

    with EmSegundoPlano(notificador):
        processos.ate_aguardando_agendamento()
        processos.responder("AgendarExecucao", "ExecucaoAgendada")
        processos.esperar_passo("ExecucaoAgendada")
        processos.responder("AgendarExecucao", "ExecucaoIniciada")
        processos.esperar_passo("ExecucaoIniciada")
        processos.responder("AgendarExecucao", "ExecucaoFinalizada")
        esperar_ate(lambda: processos.etapa() == "concluida")
        with session_factory() as sess:
            obter_registrar_entrega(sess).executar(ordem_id, ator=_ATENDENTE)
        assuntos = _assuntos(mailpit, 7)

    # RECEBIDA (a abertura) e AGUARDANDO_PAGAMENTO (o checkout vem depois) nao
    # tem e-mail; o PagamentoSolicitado tem o do checkout.
    assert assuntos == [
        "Em diagnóstico",
        "orçamento para a sua decisão",
        "pagamento",
        "Aguardando execução",
        "Em execução",
        "Finalizada",
        "Entregue",
    ]
    mensagens = {m["Subject"].split(": ", 1)[1]: m for m in mailpit.mensagens()}
    assert len(mensagens) == 7
    assert [d["Address"] for d in mensagens["Entregue"]["To"]] == [_EMAIL]
    link = _exemplo("OrcamentoGerado")["link_decisao"]
    checkout = _exemplo("PagamentoSolicitado")["checkout_url"]
    assert link in mailpit.texto(mensagens["orçamento para a sua decisão"])
    assert checkout in mailpit.texto(mensagens["pagamento"])
    with engine.connect() as conexao:
        situacoes = conexao.execute(
            text("SELECT DISTINCT situacao FROM notificacoes")
        ).all()
    assert [s[0] for s in situacoes] == ["entregue"]
    # Nada de endereco, nome nem link pelo log, pelos spans e pelas metricas.
    publico = saidas.texto(rastreador) + generate_latest().decode()
    for dado in (_EMAIL, "Maria Silva", "(11) 99999", link, checkout):
        assert dado not in publico


def test_smtp_fora_nao_atrasa_a_saga_nem_o_relay(
    processos: Atendimento,
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
) -> None:
    ordem_id = processos.abrir()
    _dar_email_ao_cliente(engine, ordem_id)
    # Porta fechada: o SMTP nao responde, e cada e-mail falha e volta.
    sem_smtp = _notificador(engine, tmp_path, host="127.0.0.1", porta=1)

    with EmSegundoPlano(sem_smtp):
        processos.ate_aguardando_agendamento()
        processos.responder("AgendarExecucao", "ExecucaoAgendada")
        processos.responder("AgendarExecucao", "ExecucaoIniciada")
        processos.responder("AgendarExecucao", "ExecucaoFinalizada")
        esperar_ate(lambda: processos.etapa() == "concluida")
        with session_factory() as sess:
            entregue = obter_registrar_entrega(sess).executar(ordem_id, ator=_ATENDENTE)
        esperar_ate(lambda: _tentativas(engine) > 0)

    # A saga andou ate o fim, e todo comando saiu da outbox.
    assert (entregue.status, entregue.etapa) == ("entregue", "concluida")
    with engine.connect() as conexao:
        pendentes = conexao.execute(
            text("SELECT count(*) FROM outbox WHERE status <> 'entregue'")
        ).scalar_one()
    assert pendentes == 0
    assert mailpit.mensagens() == []


def _tentativas(engine: Engine) -> int:
    with engine.connect() as conexao:
        total: int = conexao.execute(
            text("SELECT coalesce(sum(tentativas), 0) FROM notificacoes")
        ).scalar_one()
    return total


def test_cancelamento_envia_o_email_com_o_rotulo_e_sem_o_texto_do_atendente(
    processos: Atendimento,
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
) -> None:
    ordem_id = processos.abrir()
    _dar_email_ao_cliente(engine, ordem_id)
    notificador = _notificador(
        engine, tmp_path, host="127.0.0.1", porta=mailpit.porta_smtp
    )

    with EmSegundoPlano(notificador):
        processos.receber("SolicitarDiagnostico")
        processos.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
        processos.esperar_passo("DiagnosticoIniciado")
        processos.responder("SolicitarDiagnostico", "DiagnosticoConcluido")
        processos.receber("GerarOrcamento")
        processos.responder("GerarOrcamento", "OrcamentoGerado")
        processos.esperar_passo("OrcamentoGerado")
        with session_factory() as sess:
            obter_cancelar_ordem(sess).executar(
                ordem_id, "Cliente vendeu o carro para o vizinho", ator=_ATENDENTE
            )
        processos.receber("CancelarOrcamento")
        processos.responder("CancelarOrcamento", "OrcamentoCancelado")
        processos.receber("DescartarDiagnostico")
        processos.responder("DescartarDiagnostico", "DiagnosticoDescartado")
        esperar_ate(lambda: processos.etapa() == "compensada")
        assuntos = _assuntos(mailpit, 3)

    assert assuntos == ["Em diagnóstico", "orçamento para a sua decisão", "cancelada"]
    (cancelada,) = [
        m for m in mailpit.mensagens() if m["Subject"].endswith(": cancelada")
    ]
    corpo = mailpit.texto(cancelada)
    assert "Motivo: Cancelamento solicitado pelo atendente." in corpo
    assert "vizinho" not in corpo
