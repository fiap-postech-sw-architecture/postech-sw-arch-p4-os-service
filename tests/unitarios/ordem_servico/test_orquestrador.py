"""Orquestrador da saga com fakes: cada linha da tabela da RFC-004 secao 4.1 e a
matriz etapa x tipo pelo handler (processada, ignorada ou adiantada)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from src.compartilhado.aplicacao.mensageria import (
    ContratoInvalidoError,
    Desfecho,
    FalhaTransitoriaError,
)
from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    ViolacaoRegraDeNegocioException,
)
from src.ordem_servico.aplicacao.saga import orquestrador as modulo
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    SagaNaoEncontradaException,
)
from src.ordem_servico.aplicacao.saga.orquestrador import (
    EventoAdiantadoError,
    Tratamento,
)
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.unitarios.ordem_servico.cenario_da_saga import (
    ESPERADA,
    FLUXO_FELIZ,
    PRAZO,
    STATUS_DE_ENTRADA,
    CenarioDaSaga,
    esperado,
)

E = EtapaSaga
_ITENS = [
    {"tipo": "servico", "codigo": "SRV-SUSPENSAO", "quantidade": 1},
    {"tipo": "peca", "codigo": "PEC-AMORTECEDOR-D", "quantidade": 2},
]
# orcamento_id do OrcamentoGerado de exemplo do platform.
_ORCAMENTO_ID = "8605886c-3bb2-4165-8f1d-2368c87022e9"


def _dados_esperados(comando: str, ordem_id: UUID) -> dict[str, Any]:
    return {
        "GerarOrcamento": {"ordem_id": str(ordem_id), "itens": _ITENS},
        "ReservarPecas": {
            "ordem_id": str(ordem_id),
            "pecas": [{"sku": "PEC-AMORTECEDOR-D", "quantidade": 2}],
        },
        "SolicitarPagamento": {
            "ordem_id": str(ordem_id),
            "orcamento_id": _ORCAMENTO_ID,
        },
        "AgendarExecucao": {"ordem_id": str(ordem_id), "prioridade": "normal"},
    }[comando]


# (evento, etapa antes, status da OS depois, comando enviado, etapa nova)
LINHAS = [
    pytest.param(
        0,
        E.AGUARDANDO_DIAGNOSTICO,
        "em_diagnostico",
        None,
        E.AGUARDANDO_DIAGNOSTICO,
        id="DiagnosticoIniciado",
    ),
    pytest.param(
        1,
        E.AGUARDANDO_DIAGNOSTICO,
        "em_diagnostico",
        "GerarOrcamento",
        E.AGUARDANDO_ORCAMENTO,
        id="DiagnosticoConcluido",
    ),
    pytest.param(
        2,
        E.AGUARDANDO_ORCAMENTO,
        "aguardando_aprovacao",
        None,
        E.AGUARDANDO_DECISAO,
        id="OrcamentoGerado",
    ),
    pytest.param(
        3,
        E.AGUARDANDO_DECISAO,
        "aguardando_aprovacao",
        "ReservarPecas",
        E.AGUARDANDO_RESERVA,
        id="OrcamentoAprovado",
    ),
    pytest.param(
        4,
        E.AGUARDANDO_RESERVA,
        "aguardando_pagamento",
        "SolicitarPagamento",
        E.AGUARDANDO_PAGAMENTO,
        id="PecasReservadas",
    ),
    pytest.param(
        5,
        E.AGUARDANDO_PAGAMENTO,
        "aguardando_pagamento",
        None,
        E.AGUARDANDO_PAGAMENTO,
        id="PagamentoSolicitado",
    ),
    pytest.param(
        6,
        E.AGUARDANDO_PAGAMENTO,
        "aguardando_execucao",
        "AgendarExecucao",
        E.AGUARDANDO_AGENDAMENTO,
        id="PagamentoConfirmado",
    ),
    pytest.param(
        7,
        E.AGUARDANDO_AGENDAMENTO,
        "aguardando_execucao",
        None,
        E.AGUARDANDO_INICIO,
        id="ExecucaoAgendada",
    ),
    pytest.param(
        8,
        E.AGUARDANDO_INICIO,
        "em_execucao",
        None,
        E.EM_EXECUCAO,
        id="ExecucaoIniciada",
    ),
    pytest.param(
        9, E.EM_EXECUCAO, "finalizada", None, E.CONCLUIDA, id="ExecucaoFinalizada"
    ),
]


@pytest.mark.parametrize(("indice", "antes", "status", "comando", "nova"), LINHAS)
def test_cada_linha_da_tabela_grava_status_passo_comando_e_prazo(
    indice: int, antes: EtapaSaga, status: str, comando: str | None, nova: EtapaSaga
) -> None:
    cenario = CenarioDaSaga()
    for tipo in FLUXO_FELIZ[:indice]:
        cenario.receber(tipo)
    assert cenario.saga.etapa is antes
    enviados = len(cenario.publicador.comandos)
    recebido = cenario.evento(FLUXO_FELIZ[indice])

    tratamento = cenario.orquestrador.tratar(recebido)

    assert tratamento == Tratamento(Desfecho.PROCESSADA, antes, nova)
    saga, agora = cenario.saga, cenario.relogio.agora
    assert (saga.etapa, cenario.ordem.status.value) == (nova, status)
    passo = saga.passos[-1]
    assert (passo["gatilho"], passo["mensagem_id"], passo["seq"]) == (
        recebido.tipo,
        str(recebido.id),
        len(saga.passos),
    )
    assert (passo["de"], passo["para"], passo["em"]) == (
        antes.value,
        nova.value,
        agora.isoformat(),
    )
    novos = cenario.publicador.comandos[enviados:]
    if comando is None:
        assert novos == []
        assert (passo["comando"], passo["comando_id"]) == (None, None)
        # Sem comando com prazo, a saga nao espera resposta automatica.
        assert (saga.comando_em_voo, saga.prazo_resposta_em) == (None, None)
        return
    ((tipo, dados, correlation_id, causation_id),) = novos
    esperado_ = _dados_esperados(comando, cenario.ordem_id)
    assert (tipo, dados, correlation_id, causation_id) == (
        comando,
        esperado_,
        cenario.ordem_id,
        recebido.id,
    )
    comando_id = cenario.publicador.envelopes[-1]["id"]
    assert (passo["comando"], passo["comando_id"]) == (comando, comando_id)
    # Comando com resposta automatica: prazo e reenvios zerados (RFC-004 4.6).
    assert saga.comando_em_voo == {
        "tipo": comando,
        "dados": esperado_,
        "mensagem_ids": [comando_id],
        "enviado_em": agora.isoformat(),
    }
    assert (saga.prazo_resposta_em, saga.reenvios) == (agora + PRAZO, 0)


def test_caminho_feliz_inteiro_conclui_a_saga_com_os_passos_concluidos() -> None:
    cenario = CenarioDaSaga()

    for tipo in FLUXO_FELIZ:
        assert cenario.receber(tipo).desfecho is Desfecho.PROCESSADA

    assert (cenario.saga.etapa, cenario.ordem.status) == (
        E.CONCLUIDA,
        StatusOrdem.FINALIZADA,
    )
    assert cenario.saga.passos_concluidos == ("T3", "T5", "T6", "T7")
    assert [c[0] for c in cenario.publicador.comandos] == [
        "SolicitarDiagnostico",
        "GerarOrcamento",
        "ReservarPecas",
        "SolicitarPagamento",
        "AgendarExecucao",
    ]
    assert [m.ator for m in cenario.ordem.historico][1:] == ["consumidor"] * 6
    assert cenario.ordem.resumo_pagamento is not None
    assert cenario.ordem.resumo_pagamento.status.value == "confirmado"
    assert cenario.saga.passos[8]["posicao_na_fila"] == 3


def _desfecho(cenario: CenarioDaSaga, tipo: str) -> str:
    try:
        return cenario.receber(tipo).desfecho.value
    except EventoAdiantadoError:
        return "adiantada"


def _desfecho_esperado(etapa: str, tipo: str) -> str:
    classificacao = esperado(etapa, tipo).value
    if classificacao == "adiantado":
        return "adiantada"
    # So o fluxo normal e processado; falhas de negocio e respostas de
    # compensacao ficam para as compensacoes.
    if classificacao == "processar" and tipo in FLUXO_FELIZ:
        return "processada"
    return "ignorada"


@pytest.mark.parametrize(
    ("etapa", "tipo"),
    [
        pytest.param(etapa, tipo, id=f"{etapa}-{tipo}")
        for etapa in STATUS_DE_ENTRADA
        for tipo in ESPERADA
    ],
)
def test_matriz_pelo_handler_so_levanta_a_falha_transitoria(
    etapa: str, tipo: str
) -> None:
    cenario = CenarioDaSaga.em(etapa)
    assert cenario.ordem.status is STATUS_DE_ENTRADA[etapa]
    passos, versao_da_os = len(cenario.saga.passos), cenario.ordem.versao

    desfecho = _desfecho(cenario, tipo)

    assert desfecho == _desfecho_esperado(etapa, tipo)
    if desfecho != "processada":
        # Ignorado ou adiantado nao toca saga nem OS.
        assert (len(cenario.saga.passos), cenario.ordem.versao) == (
            passos,
            versao_da_os,
        )
        assert cenario.saga.etapa.value == etapa


def test_adiantado_e_falha_transitoria_e_passa_quando_a_saga_alcanca() -> None:
    cenario = CenarioDaSaga.em("aguardando_agendamento")
    iniciada = cenario.evento("ExecucaoIniciada")

    with pytest.raises(FalhaTransitoriaError) as exc:
        cenario.orquestrador.tratar(iniciada)
    assert isinstance(exc.value, EventoAdiantadoError)
    assert exc.value.etapa is E.AGUARDANDO_AGENDAMENTO

    cenario.receber("ExecucaoAgendada")
    assert cenario.orquestrador.tratar(iniciada).desfecho is Desfecho.PROCESSADA
    assert cenario.saga.etapa is E.EM_EXECUCAO


def test_resposta_repetida_com_id_novo_nao_muda_nada() -> None:
    cenario = CenarioDaSaga.em("aguardando_decisao")
    passos, comandos = cenario.saga.passos, list(cenario.publicador.comandos)

    # O OrcamentoGerado republicado (reenvio do GerarOrcamento) chega depois.
    tratamento = cenario.receber("OrcamentoGerado")

    assert tratamento == Tratamento(
        Desfecho.IGNORADA, E.AGUARDANDO_DECISAO, E.AGUARDANDO_DECISAO
    )
    assert (cenario.saga.passos, cenario.publicador.comandos) == (passos, comandos)


def test_decisao_do_atendente_vai_para_o_ator_do_passo() -> None:
    cenario = CenarioDaSaga.em("aguardando_decisao")
    atendente = str(uuid4())

    cenario.receber("OrcamentoAprovado", canal="atendente", decidido_por=atendente)

    assert cenario.saga.passos[-1]["ator"] == atendente


def test_orcamento_so_de_servicos_reserva_lista_vazia() -> None:
    cenario = CenarioDaSaga()
    cenario.receber("DiagnosticoIniciado")
    cenario.receber(
        "DiagnosticoConcluido",
        itens=[{"tipo": "servico", "codigo": "SRV-ALINHAMENTO", "quantidade": 1}],
    )
    cenario.receber("OrcamentoGerado")

    cenario.receber("OrcamentoAprovado")

    tipo, dados, _, _ = cenario.publicador.comandos[-1]
    assert (tipo, dados["pecas"]) == ("ReservarPecas", [])


def test_falha_de_negocio_na_etapa_e_ignorada_com_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga.em("aguardando_decisao")
    passos = cenario.saga.passos

    with capture_logs() as logs:
        tratamento = cenario.receber("OrcamentoRecusado")

    assert tratamento.desfecho is Desfecho.IGNORADA
    assert cenario.saga.passos == passos
    assert {
        "event": "saga event ignored",
        "log_level": "info",
        "etapa": "aguardando_decisao",
        "classificacao": "sem_compensacao",
        "correlation_id": str(cenario.ordem_id),
        "tipo": "OrcamentoRecusado",
    } in logs


def test_logs_da_transicao_e_do_adiantado_sem_texto_livre(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga()
    cenario.receber("DiagnosticoIniciado")

    with capture_logs() as logs:
        cenario.receber("DiagnosticoConcluido", observacoes="Joao 11999990000")
        with pytest.raises(EventoAdiantadoError):
            cenario.receber("PecasReservadas")

    assert logs == [
        {
            "event": "saga transition",
            "log_level": "info",
            "etapa": "aguardando_diagnostico",
            "etapa_nova": "aguardando_orcamento",
            "status": "em_diagnostico",
            "correlation_id": str(cenario.ordem_id),
            "tipo": "DiagnosticoConcluido",
        },
        {
            "event": "saga event ahead",
            "log_level": "warning",
            "etapa": "aguardando_orcamento",
            "correlation_id": str(cenario.ordem_id),
            "tipo": "PecasReservadas",
        },
    ]


def test_ordem_sem_saga_e_erro_permanente() -> None:
    cenario = CenarioDaSaga()

    with pytest.raises(SagaNaoEncontradaException):
        cenario.orquestrador.tratar(evento("DiagnosticoIniciado", uuid4()))


def test_ordem_id_dos_dados_diferente_do_correlation_id_e_recusado() -> None:
    cenario = CenarioDaSaga()

    with pytest.raises(ContratoInvalidoError, match="diverge"):
        cenario.receber("DiagnosticoIniciado", ordem_id=str(uuid4()))

    assert cenario.ordem.status is StatusOrdem.RECEBIDA


def test_ordem_id_em_maiusculas_nos_dados_e_o_mesmo_uuid() -> None:
    cenario = CenarioDaSaga()

    tratamento = cenario.receber(
        "DiagnosticoIniciado", ordem_id=str(cenario.ordem_id).upper()
    )

    assert tratamento.desfecho is Desfecho.PROCESSADA


def test_conflito_de_versao_ao_gravar_propaga_como_transitorio() -> None:
    cenario = CenarioDaSaga()
    cenario.sagas.provocar_conflito()

    with pytest.raises(ConflitoDeConcorrenciaException):
        cenario.receber("DiagnosticoIniciado")


def test_pecas_reservadas_sem_orcamento_na_os_e_erro_permanente() -> None:
    cenario = CenarioDaSaga.em("aguardando_reserva")
    # OS reidratada sem o resumo: nao acontece pelo caminho legal, que grava o
    # resumo no OrcamentoGerado.
    ordem = cenario.ordem
    sem_orcamento = OrdemDeServico(
        id=ordem.id,
        _cliente_id=ordem.cliente_id,
        _veiculo_id=ordem.veiculo_id,
        _descricao_problema="x",
        _status=StatusOrdem.AGUARDANDO_APROVACAO,
    )
    cenario.ordens.ordens = {ordem.id: sem_orcamento}

    with pytest.raises(ViolacaoRegraDeNegocioException, match="orcamento"):
        cenario.receber("PecasReservadas")
