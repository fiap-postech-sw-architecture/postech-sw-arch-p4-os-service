"""Saga de ponta a ponta no OS: relay, consumidor, RabbitMQ e participantes falsos.

Os participantes leem ``billing.comandos`` e ``execucao.comandos`` (como admin) e
respondem como ``billing`` e ``execucao``, com o ``causation_id`` do comando que
responde (RFC-004 secao 5.2) e o contexto de trace dele; os passos humanos
(mecanico, cliente) saem do contexto do comando que os pos em espera (ADR-043).
As filas de retry do broker de teste tem TTL de 100 ms.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
import structlog
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from structlog.testing import capture_logs

from src.compartilhado.aplicacao.mensageria import Desfecho, FalhaPermanenteError
from src.compartilhado.infraestrutura import observability as observabilidade
from src.compartilhado.infraestrutura.mensageria import consumidor as modulo_consumidor
from src.consumidor import montar_despachante
from src.ordem_servico.interfaces.dependencies import (
    obter_cancelar_ordem,
    obter_obter_ordem,
    obter_obter_saga,
    obter_registrar_entrega,
)
from tests.eventos import envelope_de_evento
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.mensageria.apoio_da_saga import (
    ATENDENTE,
    Atendimento,
    amostra,
    consumidor_de_teste,
    relay_de_teste,
)
from tests.integracao.seed_helpers import (
    notificacoes_recusando_no_commit,
    outbox_recusando_no_commit,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from opentelemetry.sdk.trace import ReadableSpan
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador, Saidas

_DLQ = "os.eventos.dlq"
# As cinco compensacoes, na ordem da RFC-004 secao 4.4, e a resposta de cada uma.
_COMPENSACOES = {
    "CancelarExecucao": "ExecucaoCancelada",
    "EstornarPagamento": "PagamentoEstornado",
    "LiberarReserva": "ReservaLiberada",
    "CancelarOrcamento": "OrcamentoCancelado",
    "DescartarDiagnostico": "DiagnosticoDescartado",
}


# O evento cujo consumo grava cada comando seguinte na outbox.
_CAUSA = {
    "GerarOrcamento": "DiagnosticoConcluido",
    "ReservarPecas": "OrcamentoAprovado",
    "SolicitarPagamento": "PecasReservadas",
    "AgendarExecucao": "PagamentoConfirmado",
}


def _pai(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


def _id(rastreador: Rastreador, nome: str) -> int:
    (span,) = rastreador.spans(nome)
    return span.get_span_context().span_id


def _retries() -> float:
    return sum(
        amostra.value
        for metrica in REGISTRY.collect()
        if metrica.name == "pytstop_mensagens_consumidas"
        for amostra in metrica.samples
        if amostra.name.endswith("_total") and amostra.labels["resultado"] == "retry"
    )


def _consumidas(tipo: str, resultado: str) -> float:
    return amostra("pytstop_mensagens_consumidas_total", tipo=tipo, resultado=resultado)


@pytest.fixture
def atendimento(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> Iterator[Atendimento]:
    """Relay e consumidor reais em threads, com o despachante do processo."""
    relay = relay_de_teste(engine, broker, rastreador, tmp_path)
    consumidor = consumidor_de_teste(session_factory, broker, rastreador, tmp_path)
    with EmSegundoPlano(relay), EmSegundoPlano(consumidor):
        esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
        yield Atendimento(engine, session_factory, broker, rastreador)


def test_caminho_feliz_ate_a_entrega_num_trace_so(
    atendimento: Atendimento,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
) -> None:
    iniciadas = amostra("pytstop_saga_iniciadas_total")
    concluidas = amostra("pytstop_saga_finalizadas_total", resultado="concluida")
    retries = _retries()

    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")
    atendimento.esperar_passo("ExecucaoAgendada")
    atendimento.responder("AgendarExecucao", "ExecucaoIniciada")
    atendimento.esperar_passo("ExecucaoIniciada")
    atendimento.responder("AgendarExecucao", "ExecucaoFinalizada")
    esperar_ate(lambda: atendimento.etapa() == "concluida")
    with session_factory() as sess:
        entregue = obter_registrar_entrega(sess).executar(ordem_id, ator=ATENDENTE)

    assert (entregue.status, entregue.etapa) == ("entregue", "concluida")
    assert atendimento.gatilhos() == [
        "abertura",
        "DiagnosticoIniciado",
        "DiagnosticoConcluido",
        "OrcamentoGerado",
        "OrcamentoAprovado",
        "PecasReservadas",
        "PagamentoSolicitado",
        "PagamentoConfirmado",
        "ExecucaoAgendada",
        "ExecucaoIniciada",
        "ExecucaoFinalizada",
    ]
    assert [m.para for m in entregue.historico] == [
        "recebida",
        "em_diagnostico",
        "aguardando_aprovacao",
        "aguardando_pagamento",
        "aguardando_execucao",
        "em_execucao",
        "finalizada",
        "entregue",
    ]
    # Cada comando seguinte leva o id do evento que o causou; o primeiro, nulo.
    causas = {t: e["causation_id"] for t, (_, e) in atendimento.comandos.items()}
    passos = {p["comando"]: p["mensagem_id"] for p in entregue.passos if p["comando"]}
    assert causas == {
        "SolicitarDiagnostico": None,
        "GerarOrcamento": passos["GerarOrcamento"],
        "ReservarPecas": passos["ReservarPecas"],
        "SolicitarPagamento": passos["SolicitarPagamento"],
        "AgendarExecucao": passos["AgendarExecucao"],
    }
    assert broker.contar(_DLQ) == 0
    assert _retries() == retries
    # Um UPDATE da saga por evento: a abertura grava a versao 1 e cada um dos
    # dez eventos a sobe uma vez (o traceparent vai no mesmo UPDATE).
    with atendimento.engine.connect() as conexao:
        versao = conexao.execute(
            text("SELECT versao FROM sagas WHERE ordem_id = :id"), {"id": ordem_id}
        ).scalar_one()
    assert versao == 11
    assert amostra("pytstop_saga_iniciadas_total") == iniciadas + 1
    assert (
        amostra("pytstop_saga_finalizadas_total", resultado="concluida")
        == concluidas + 1
    )

    # Um trace da abertura aos comandos e respostas (RFC-004 secao 9). O span
    # do ultimo consumo termina depois do ack: espera por ele antes de ler.
    esperar_ate(lambda: rastreador.spans("process ExecucaoFinalizada"))
    (raiz,) = rastreador.spans("POST /api/v1/ordens-de-servico")
    trace_id = raiz.get_span_context().trace_id
    spans = rastreador.spans()
    assert {s.get_span_context().trace_id for s in spans} == {trace_id}
    # E uma arvore: o publish de cada comando e filho de quem o gravou na
    # outbox (a requisicao ou o consumo do evento que o causou), e o consumo
    # de cada evento, do participante que o publicou.
    pais = {s.name: _pai(s) for s in spans}
    assert pais["publish SolicitarDiagnostico"] == raiz.get_span_context().span_id
    for comando, causa in _CAUSA.items():
        assert pais[f"publish {comando}"] == _id(rastreador, f"process {causa}")
    for tipo in atendimento.gatilhos()[1:]:
        assert pais[f"process {tipo}"] == _id(rastreador, f"participante {tipo}")
    (consumo,) = rastreador.spans("process PecasReservadas")
    assert consumo.attributes is not None
    assert {
        k: v for k, v in consumo.attributes.items() if k.startswith("pytstop.saga")
    } == {
        "pytstop.saga.etapa": "aguardando_reserva",
        "pytstop.saga.etapa_nova": "aguardando_pagamento",
        "pytstop.saga.desfecho": "processada",
    }


def test_mesmo_evento_entregue_duas_vezes_gera_um_comando_so(
    atendimento: Atendimento, engine: Engine, broker: Broker
) -> None:
    # RN-028: a reentrega do mesmo id (o broker devolve a mensagem sem ack, o
    # participante republica o mesmo envelope) nao repete o efeito.
    atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
    atendimento.esperar_passo("DiagnosticoIniciado")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoConcluido")
    atendimento.receber("GerarOrcamento")
    atendimento.responder("GerarOrcamento", "OrcamentoGerado")
    atendimento.esperar_passo("OrcamentoGerado")
    atendimento.responder("GerarOrcamento", "OrcamentoAprovado")
    atendimento.receber("ReservarPecas")
    duplicadas = _consumidas("PecasReservadas", "duplicada")

    reservadas = atendimento.responder("ReservarPecas", "PecasReservadas")
    broker.publicar_evento(reservadas)
    esperar_ate(lambda: _consumidas("PecasReservadas", "duplicada") > duplicadas)

    with engine.connect() as conexao:
        pedidos = conexao.execute(
            text(
                "SELECT count(*) FROM outbox "
                "WHERE envelope ->> 'tipo' = 'SolicitarPagamento'"
            )
        ).scalar_one()
    assert pedidos == 1
    assert atendimento.gatilhos().count("PecasReservadas") == 1
    assert broker.contar(_DLQ) == 0


def test_placa_e_descricao_so_no_comando_da_execucao(
    saidas: Saidas,
    atendimento: Atendimento,
    engine: Engine,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
) -> None:
    # Valores sentinela no caminho real (abertura, relay, consumidor,
    # orquestrador, entrega): a placa e o texto livre so podem aparecer no
    # envelope do SolicitarDiagnostico, por contrato (RFC-004 secao 5.3). Os
    # logs vem da fixture `saidas`, que nao depende do stream em que o logging
    # foi configurado por outro teste.
    placa, descricao = "QZX7W42", "Sentinela 7f3a do problema relatado"
    ordem_id = atendimento.abrir(placa=placa, descricao=descricao)
    atendimento.ate_aguardando_agendamento()
    for tipo in ("ExecucaoAgendada", "ExecucaoIniciada"):
        atendimento.responder("AgendarExecucao", tipo)
        atendimento.esperar_passo(tipo)
    atendimento.responder("AgendarExecucao", "ExecucaoFinalizada")
    esperar_ate(lambda: atendimento.etapa() == "concluida")
    with session_factory() as sess:
        obter_registrar_entrega(sess).executar(ordem_id, ator=ATENDENTE)
        saga = obter_obter_saga(sess).executar(ordem_id)
    esperar_ate(lambda: rastreador.spans("process ExecucaoFinalizada"))

    # Os logs do caminho sairam (e foram capturados) neste teste.
    eventos = {evento["event"] for evento in saidas.eventos}
    assert {"saga started", "saga transition"} <= eventos
    textos = [saidas.texto(rastreador), generate_latest(REGISTRY).decode(), repr(saga)]
    with engine.connect() as conexao:
        for consulta in (
            "SELECT passos::text, comando_em_voo::text, itens::text FROM sagas",
            "SELECT * FROM mensagens_processadas",
            "SELECT * FROM historico_status_ordem",
            "SELECT ultimo_erro FROM outbox",
        ):
            textos += [str(linha) for linha in conexao.execute(text(consulta))]
        com_placa = conexao.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox WHERE envelope::text LIKE :p"),
            {"p": f"%{placa}%"},
        ).scalars()
        assert list(com_placa) == ["SolicitarDiagnostico"]
    vazamentos = [t for t in textos if placa in t or descricao in t]
    assert vazamentos == []


def test_execucao_iniciada_antes_da_agendada_volta_pela_retry_e_passa(
    atendimento: Atendimento, broker: Broker, rastreador: Rastreador
) -> None:
    atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    retries = _retries()

    # O inicio ultrapassa o agendamento (retry ou consumidores concorrentes):
    # primeiro na fila, ele chega antes ao consumidor (prefetch 1) e e
    # adiantado; publicados em seguida, nao dependem de tempo do teste.
    atendimento.responder("AgendarExecucao", "ExecucaoIniciada")
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")

    esperar_ate(lambda: atendimento.etapa() == "em_execucao")
    assert _retries() > retries
    assert broker.contar(_DLQ) == 0
    assert atendimento.gatilhos()[-2:] == ["ExecucaoAgendada", "ExecucaoIniciada"]
    adiantado = [
        s
        for s in rastreador.spans("process ExecucaoIniciada")
        if s.attributes and s.attributes.get("pytstop.saga.desfecho") == "adiantada"
    ]
    assert adiantado


def test_commit_recusado_depois_das_escritas_desfaz_os_saga_comando_e_registro(
    atendimento: Atendimento, engine: Engine, broker: Broker
) -> None:
    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_pagamento_com_checkout()
    antes = _estado(engine, ordem_id)
    escritas: list[str] = []

    def registrar(_conexao: Any, _cursor: Any, sql: str, *_: Any) -> None:
        escritas.append(sql.split("(")[0].split(" SET ")[0].strip())

    # O PagamentoConfirmado muda a OS (status, resumo, historico), a saga
    # (etapa, passo, comando em voo), grava o AgendarExecucao e o pedido de
    # e-mail da nova situacao; o trigger deferido so falha no COMMIT, depois de
    # tudo isso. Tentativa apos tentativa, a transacao inteira cai, ate a DLQ.
    with outbox_recusando_no_commit(engine):
        event.listen(engine, "before_cursor_execute", registrar)
        try:
            confirmado = atendimento.responder(
                "SolicitarPagamento", "PagamentoConfirmado"
            )
            esperar_ate(lambda: broker.contar(_DLQ) == 1)
        finally:
            event.remove(engine, "before_cursor_execute", registrar)

    # A falha veio depois das escritas pendentes de OS, saga, outbox e e-mail.
    assert {
        "UPDATE ordens_de_servico",
        "INSERT INTO historico_status_ordem",
        "UPDATE sagas",
        "INSERT INTO outbox",
        "INSERT INTO notificacoes",
    } <= set(escritas)
    assert _estado(engine, ordem_id) == antes
    with engine.connect() as conexao:
        registrada = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
            {"id": confirmado["id"]},
        ).scalar_one()
    assert registrada == 0


def test_entrega_com_commit_recusado_deixa_a_os_finalizada_e_sem_pedido_de_email(
    atendimento: Atendimento,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")
    atendimento.esperar_passo("ExecucaoAgendada")
    atendimento.responder("AgendarExecucao", "ExecucaoIniciada")
    atendimento.esperar_passo("ExecucaoIniciada")
    atendimento.responder("AgendarExecucao", "ExecucaoFinalizada")
    esperar_ate(lambda: atendimento.etapa() == "concluida")
    antes = _estado(engine, ordem_id)

    # O trigger deferido recusa o pedido de e-mail no COMMIT, depois de a OS ter
    # mudado para `entregue`: so a transacao unica desfaz as duas escritas.
    with notificacoes_recusando_no_commit(engine), session_factory() as sess:
        registrar = obter_registrar_entrega(sess)
        with pytest.raises(DBAPIError, match="notificacoes indisponivel"):
            registrar.executar(ordem_id, ator=ATENDENTE)

    assert _estado(engine, ordem_id) == antes
    with engine.connect() as conexao:
        status = conexao.execute(
            text("SELECT status FROM ordens_de_servico WHERE id = :id"),
            {"id": ordem_id},
        ).scalar_one()
    assert status == "finalizada"


def _estado(engine: Engine, ordem_id: UUID) -> tuple[object, ...]:
    """OS, historico, saga, comandos da outbox e pedidos de e-mail, do banco."""
    with engine.connect() as conexao:
        os_ = conexao.execute(
            text(
                "SELECT status, pagamento_status, versao FROM ordens_de_servico "
                "WHERE id = :id"
            ),
            {"id": ordem_id},
        ).one()
        historico = conexao.execute(
            text("SELECT count(*) FROM historico_status_ordem WHERE ordem_id = :id"),
            {"id": ordem_id},
        ).scalar_one()
        saga = conexao.execute(
            text(
                "SELECT etapa, passos, comando_em_voo, versao FROM sagas "
                "WHERE ordem_id = :id"
            ),
            {"id": ordem_id},
        ).one()
        comandos = conexao.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox ORDER BY id")
        ).scalars()
        emails = conexao.execute(
            text(
                "SELECT modelo, status_da_ordem FROM notificacoes "
                "WHERE ordem_id = :id ORDER BY id"
            ),
            {"id": ordem_id},
        ).all()
        return (
            tuple(os_),
            historico,
            tuple(saga),
            tuple(comandos),
            tuple(tuple(email) for email in emails),
        )


@pytest.mark.parametrize(
    ("caso", "motivo", "etapa"),
    [
        pytest.param("divergente", "ordem_id_divergente", None, id="divergente"),
        pytest.param("sem_saga", "saga_inexistente", None, id="sem-saga"),
        pytest.param(
            "encerrada", "ordem_encerrada", "aguardando_diagnostico", id="encerrada"
        ),
    ],
)
def test_recusa_vai_para_a_dlq_com_o_motivo_sem_efeito_nem_registro(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
    caso: str,
    motivo: str,
    etapa: str | None,
) -> None:
    ordem_id = atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    if caso == "encerrada":
        # OS encerrada com a saga viva, forcada por fora: o cancelamento passa
        # pela saga e nunca produz esse estado, e nenhum evento o toca.
        with engine.begin() as conexao:
            conexao.execute(
                text(
                    "UPDATE ordens_de_servico SET status = 'cancelada' WHERE id = :id"
                ),
                {"id": ordem_id},
            )
    gatilhos = atendimento.gatilhos()
    monkeypatch.setattr(modulo_consumidor, "_log", structlog.get_logger())

    with capture_logs() as logs:
        match caso:
            case "divergente":
                recusado = atendimento.responder(
                    "SolicitarDiagnostico", "DiagnosticoIniciado", ordem_id=str(uuid4())
                )
            case "sem_saga":
                recusado = envelope_de_evento("DiagnosticoIniciado")
                broker.publicar_evento(recusado)
            case _:
                recusado = atendimento.responder(
                    "SolicitarDiagnostico", "DiagnosticoIniciado"
                )
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    # O operador le o motivo no log e no span; o id fica livre para o redrive.
    assert [
        (log["event"], log.get("motivo")) for log in logs if "dlq" in log["event"]
    ] == [("message rejected to dlq", motivo)]
    with engine.connect() as conexao:
        registrada = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
            {"id": recusado["id"]},
        ).scalar_one()
    assert registrada == 0
    assert atendimento.gatilhos() == gatilhos
    (consumo,) = rastreador.spans(f"process {recusado['tipo']}")
    assert consumo.status.description is None
    assert consumo.attributes is not None
    assert consumo.attributes["pytstop.falha"] == motivo
    saga = {k: v for k, v in consumo.attributes.items() if k.startswith("pytstop.saga")}
    esperado = {"pytstop.saga.desfecho": "recusada"}
    if etapa is not None:
        esperado["pytstop.saga.etapa"] = etapa
    assert saga == esperado


def test_aprovacao_de_peca_fora_do_formato_do_billing_vai_para_a_dlq_e_a_saga_espera(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O diagnostico aceita o codigo mais permissivo que o ``GerarOrcamento``
    tambem aceita, mas o SKU do ``ReservarPecas`` e so o formato do Billing. Na
    pratica o caso nao chega aqui: o Billing responde ``GeracaoDeOrcamentoFalhou``
    ao codigo que nao esta na tabela dele (so tem maiusculas, digitos e hifens),
    e a saga compensa. O evento de aprovacao e forcado para ver o que o OS faz
    se ele vier mesmo assim: o comando nao e montado, nada vai para a outbox, a
    aprovacao vai para a DLQ e a saga fica onde estava, sem prazo correndo."""
    ordem_id = atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
    atendimento.esperar_passo("DiagnosticoIniciado")
    itens = [{"tipo": "peca", "codigo": "pec_vela.2", "quantidade": 1}]
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoConcluido", itens=itens)
    gerar = atendimento.receber("GerarOrcamento")
    assert gerar["dados"]["itens"] == itens
    atendimento.responder("GerarOrcamento", "OrcamentoGerado")
    atendimento.esperar_passo("OrcamentoGerado")
    antes = _estado(engine, ordem_id)
    monkeypatch.setattr(modulo_consumidor, "_log", structlog.get_logger())

    with capture_logs() as logs:
        aprovado = atendimento.responder("GerarOrcamento", "OrcamentoAprovado")
        esperar_ate(lambda: broker.contar(_DLQ) == 1)

    assert [
        (log["motivo"], log["erro"])
        for log in logs
        if log["event"] == "message rejected to dlq"
    ] == [("erro_no_handler", "ContratoInvalidoError")]
    assert _estado(engine, ordem_id) == antes
    assert atendimento.etapa() == "aguardando_decisao"
    assert "ReservarPecas" not in antes[3]
    with engine.connect() as conexao:
        registrada = conexao.execute(
            text("SELECT count(*) FROM mensagens_processadas WHERE mensagem_id = :id"),
            {"id": aprovado["id"]},
        ).scalar_one()
        prazo = conexao.execute(
            text("SELECT prazo_resposta_em FROM sagas WHERE ordem_id = :id"),
            {"id": ordem_id},
        ).scalar_one()
    assert registrada == 0
    assert prazo is None


def _compensacoes_na_outbox(engine: Engine) -> list[str]:
    with engine.connect() as conexao:
        tipos = conexao.execute(
            text("SELECT envelope ->> 'tipo' FROM outbox ORDER BY id")
        ).scalars()
        return [tipo for tipo in tipos if tipo in _COMPENSACOES]


def _cancelar(
    atendimento: Atendimento,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
    motivo: str = "Cliente vendeu o carro",
) -> None:
    """O atendente pede o cancelamento, no trace de uma requisicao propria."""
    monkeypatch.setattr(observabilidade, "_tracer_da_api", rastreador.tracer)
    with (
        rastreador.tracer.start_as_current_span("POST cancelamento"),
        session_factory() as sess,
    ):
        cancelada = obter_cancelar_ordem(sess).executar(
            atendimento.ordem_id, motivo, ator=ATENDENTE
        )
    assert cancelada.etapa == "compensando"


def test_cancelamento_depois_do_pagamento_compensa_uma_por_vez_na_ordem_da_rfc(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compensacoes = amostra("pytstop_saga_compensacoes_total", motivo="cancelamento")
    compensadas = amostra("pytstop_saga_finalizadas_total", resultado="compensada")
    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")
    atendimento.esperar_passo("ExecucaoAgendada")

    _cancelar(atendimento, session_factory, rastreador, monkeypatch)

    respostas: list[dict[str, Any]] = []
    for indice, (comando, resposta) in enumerate(_COMPENSACOES.items(), start=1):
        envelope = atendimento.receber(comando)
        # Uma por vez: a seguinte so sai depois da resposta desta.
        assert _compensacoes_na_outbox(engine) == list(_COMPENSACOES)[:indice]
        assert envelope["dados"]["motivo"] == "cancelamento"
        # A primeira nasce da requisicao (sem causa); as outras, da resposta
        # anterior (RFC-004 secao 5.2).
        causa = respostas[-1]["id"] if respostas else None
        assert envelope["causation_id"] == causa
        respostas.append(atendimento.responder(comando, resposta))
    esperar_ate(lambda: atendimento.etapa() == "compensada")

    with session_factory() as sess:
        ordem = obter_obter_ordem(sess).executar(ordem_id)
    assert (ordem.status, ordem.etapa) == ("cancelada", "compensada")
    ultima = ordem.historico[-1]
    assert (ultima.origem, ultima.motivo) == ("saga", "Cliente vendeu o carro")
    gatilhos = atendimento.gatilhos()
    assert gatilhos[-6:] == ["cancelamento_solicitado", *_COMPENSACOES.values()]
    assert broker.contar(_DLQ) == 0
    assert amostra(
        "pytstop_saga_compensacoes_total", motivo="cancelamento"
    ) == pytest.approx(compensacoes + 1)
    assert amostra(
        "pytstop_saga_finalizadas_total", resultado="compensada"
    ) == pytest.approx(compensadas + 1)

    # O trace da saga segue pelo cancelamento: o span da acao e filho do ultimo
    # contexto dela, com link para a requisicao, e o primeiro comando sai dele.
    esperar_ate(lambda: rastreador.spans("process DiagnosticoDescartado"))
    (raiz,) = rastreador.spans("POST /api/v1/ordens-de-servico")
    (requisicao,) = rastreador.spans("POST cancelamento")
    (acao,) = rastreador.spans("saga cancelamento")
    trace_id = raiz.get_span_context().trace_id
    assert acao.context.trace_id == trace_id
    assert _pai(acao) == _id(rastreador, "process ExecucaoAgendada")
    (link,) = acao.links
    assert link.context.span_id == requisicao.get_span_context().span_id
    assert _pai(rastreador.spans("publish CancelarExecucao")[0]) == (
        acao.get_span_context().span_id
    )
    assert {s.get_span_context().trace_id for s in rastreador.spans()} == {
        trace_id,
        requisicao.get_span_context().trace_id,
    }


def test_falta_de_peca_compensa_o_orcamento_e_o_diagnostico_e_cancela_a_os(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    session_factory: sessionmaker[Session],
) -> None:
    # Falha de negocio de ponta a ponta no broker real: a reserva falha, e a saga
    # desfaz o orcamento e o diagnostico, um por vez, cada um causado pela
    # resposta anterior (RFC-004 secoes 4.4 e 5.2).
    compensacoes = amostra("pytstop_saga_compensacoes_total", motivo="reserva_falhou")
    compensadas = amostra("pytstop_saga_finalizadas_total", resultado="compensada")
    ordem_id = atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
    atendimento.esperar_passo("DiagnosticoIniciado")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoConcluido")
    atendimento.receber("GerarOrcamento")
    atendimento.responder("GerarOrcamento", "OrcamentoGerado")
    atendimento.esperar_passo("OrcamentoGerado")
    atendimento.responder("GerarOrcamento", "OrcamentoAprovado")
    atendimento.receber("ReservarPecas")

    falta = atendimento.responder("ReservarPecas", "ReservaDePecasFalhou")

    cancelar = atendimento.receber("CancelarOrcamento")
    assert (cancelar["dados"]["motivo"], cancelar["causation_id"]) == (
        "reserva_falhou",
        falta["id"],
    )
    # Uma por vez: o diagnostico so e descartado depois do orcamento cancelado.
    assert _compensacoes_na_outbox(engine) == ["CancelarOrcamento"]
    cancelado = atendimento.responder("CancelarOrcamento", "OrcamentoCancelado")
    descartar = atendimento.receber("DescartarDiagnostico")
    assert (descartar["dados"]["motivo"], descartar["causation_id"]) == (
        "reserva_falhou",
        cancelado["id"],
    )
    assert _compensacoes_na_outbox(engine) == [
        "CancelarOrcamento",
        "DescartarDiagnostico",
    ]
    atendimento.responder("DescartarDiagnostico", "DiagnosticoDescartado")
    esperar_ate(lambda: atendimento.etapa() == "compensada")

    with session_factory() as sess:
        ordem = obter_obter_ordem(sess).executar(ordem_id)
    assert (ordem.status, ordem.etapa) == ("cancelada", "compensada")
    ultima = ordem.historico[-1]
    assert (ultima.origem, ultima.motivo) == ("saga", "Falta de peça para a reserva")
    assert atendimento.gatilhos()[-4:] == [
        "OrcamentoAprovado",
        "ReservaDePecasFalhou",
        "OrcamentoCancelado",
        "DiagnosticoDescartado",
    ]
    assert broker.contar(_DLQ) == 0
    assert amostra(
        "pytstop_saga_compensacoes_total", motivo="reserva_falhou"
    ) == pytest.approx(compensacoes + 1)
    assert amostra(
        "pytstop_saga_finalizadas_total", resultado="compensada"
    ) == pytest.approx(compensadas + 1)


def test_estorno_recusado_para_a_sequencia_e_guarda_o_comando_em_voo(
    atendimento: Atendimento,
    engine: Engine,
    broker: Broker,
    session_factory: sessionmaker[Session],
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordem_id = atendimento.abrir()
    atendimento.ate_aguardando_agendamento()
    atendimento.responder("AgendarExecucao", "ExecucaoAgendada")
    atendimento.esperar_passo("ExecucaoAgendada")
    _cancelar(atendimento, session_factory, rastreador, monkeypatch)
    atendimento.receber("CancelarExecucao")
    atendimento.responder("CancelarExecucao", "ExecucaoCancelada")
    estorno = atendimento.receber("EstornarPagamento")

    atendimento.responder("EstornarPagamento", "EstornoDePagamentoFalhou")
    esperar_ate(lambda: atendimento.etapa() == "falha_na_compensacao")

    with session_factory() as sess:
        saga = obter_obter_saga(sess).executar(ordem_id)
        ordem = obter_obter_ordem(sess).executar(ordem_id)
    assert (saga.falha, saga.motivo) == ("estorno_recusado", "cancelamento")
    assert saga.plano_compensacao == tuple(list(_COMPENSACOES)[1:])
    # O comando em voo fica, sem prazo, para a retomada.
    assert saga.comando_em_voo is not None
    assert (saga.comando_em_voo.tipo, saga.prazo_resposta_em) == (
        "EstornarPagamento",
        None,
    )
    with engine.connect() as conexao:
        ids = conexao.execute(
            text(
                "SELECT comando_em_voo -> 'mensagem_ids' FROM sagas WHERE ordem_id = :i"
            ),
            {"i": ordem_id},
        ).scalar_one()
    assert ids == [estorno["id"]]
    # A sequencia parou: nada alem das duas primeiras saiu, e a OS fica como estava.
    assert _compensacoes_na_outbox(engine) == ["CancelarExecucao", "EstornarPagamento"]
    assert ordem.status == "aguardando_execucao"

    # A resposta atrasada ao envio original so atualiza o resumo do pagamento.
    atendimento.responder("EstornarPagamento", "PagamentoEstornado")

    def estornado() -> bool:
        with session_factory() as sess:
            pagamento = obter_obter_ordem(sess).executar(ordem_id).pagamento
        return pagamento is not None and pagamento.status == "estornado"

    esperar_ate(estornado)
    assert atendimento.etapa() == "falha_na_compensacao"
    assert broker.contar(_DLQ) == 0


def _redrive(broker: Broker, nome: str) -> None:
    """O mesmo shovel de ``make redrive FILA=os.eventos``, do platform."""
    broker.rabbitmqctl(
        "set_parameter",
        "shovel",
        nome,
        json.dumps(
            {
                "src-uri": "amqp://",
                "src-queue": _DLQ,
                "dest-uri": "amqp://",
                "dest-queue": "os.eventos",
                "src-delete-after": "queue-length",
            }
        ),
    )


def test_redrive_so_trata_o_evento_depois_de_implantar_a_versao_nova(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    tmp_path: Path,
) -> None:
    # A versao anterior mandava a falha de negocio e a resposta de compensacao
    # para a DLQ com `sem_tratador_nesta_versao`, sem registrar a mensagem
    # (ADR-036). A ordem do procedimento: implantar a versao nova e so depois o
    # redrive. Antes disso a versao anterior, que ainda consome `os.eventos`, as
    # recusa de novo.
    def sem_tratador(_mensagem: object, _transacao: object) -> Desfecho:
        raise FalhaPermanenteError("sem_tratador_nesta_versao")

    anterior = {**montar_despachante(timedelta(seconds=120))}
    for tipo in ("PagamentoRecusado", *_COMPENSACOES.values()):
        anterior[tipo] = sem_tratador
    atendimento = Atendimento(engine, session_factory, broker, rastreador)
    pasta_anterior = tmp_path / "anterior"
    pasta_anterior.mkdir()
    with (
        EmSegundoPlano(relay_de_teste(engine, broker, rastreador, pasta_anterior)),
        EmSegundoPlano(
            consumidor_de_teste(
                session_factory, broker, rastreador, pasta_anterior, anterior
            )
        ),
    ):
        esperar_ate(lambda: (pasta_anterior / "consumidor-pronto").exists())
        atendimento.abrir()
        atendimento.ate_aguardando_pagamento_com_checkout()
        recusado = atendimento.responder("SolicitarPagamento", "PagamentoRecusado")
        esperar_ate(lambda: broker.contar(_DLQ) == 1)
        assert atendimento.etapa() == "aguardando_pagamento"

        # Redrive ANTES de implantar: a versao anterior recusa de novo.
        recusadas = _consumidas("PagamentoRecusado", "dlq")
        _redrive(broker, "redrive-antes-de-implantar")
        esperar_ate(lambda: _consumidas("PagamentoRecusado", "dlq") == recusadas + 1)
        esperar_ate(lambda: broker.contar(_DLQ) == 1)
        assert atendimento.etapa() == "aguardando_pagamento"

    pasta_nova = tmp_path / "nova"
    pasta_nova.mkdir()
    with (
        EmSegundoPlano(relay_de_teste(engine, broker, rastreador, pasta_nova)),
        EmSegundoPlano(
            consumidor_de_teste(session_factory, broker, rastreador, pasta_nova)
        ),
    ):
        esperar_ate(lambda: (pasta_nova / "consumidor-pronto").exists())
        # Versao nova implantada: agora o redrive trata o evento.
        _redrive(broker, "redrive-depois-de-implantar")
        esperar_ate(lambda: atendimento.etapa() == "compensando")
        liberacao = atendimento.receber("LiberarReserva")

    # A saga compensa como se o evento tivesse chegado na hora, e a DLQ esvazia.
    assert liberacao["causation_id"] == recusado["id"]
    assert liberacao["dados"]["motivo"] == "pagamento_recusado"
    esperar_ate(lambda: broker.contar(_DLQ) == 0)
    assert atendimento.gatilhos()[-1] == "PagamentoRecusado"
