"""Orquestrador da saga com fakes: cada linha da tabela da RFC-004 secao 4.1, a
matriz etapa x tipo pelo handler (processada, ignorada, adiantada ou recusada)
e as compensacoes da secao 4.4 (falhas de negocio, cada compensacao pendente x
cada resposta, estorno automatico e corrida do pivot)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from src.compartilhado.aplicacao.mensageria import (
    Desfecho,
    FalhaTransitoriaError,
    MensagemRecebida,
)
from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    ViolacaoRegraDeNegocioException,
)
from src.ordem_servico.aplicacao.saga import orquestrador as modulo
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.orquestrador import (
    EventoAdiantadoError,
    EventoRecusadoError,
    Tratamento,
)
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from tests.unitarios.ordem_servico.cenario_da_saga import (
    ESPERADA,
    FLUXO_FELIZ,
    PRAZO,
    STATUS_DE_ENTRADA,
    CenarioDaSaga,
    esperado,
    perfil_de_entrada,
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
    assert (passo["de"], passo["para"], passo["em"], passo["ator"]) == (
        antes.value,
        nova.value,
        agora.isoformat(),
        "consumidor",
    )
    # O resumo que a OS guarda sai campo a campo do evento do Billing.
    assert _resumos(cenario.ordem) == _resumos_esperados(cenario, recebido)
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


def _resumos(ordem: OrdemDeServico) -> tuple[object, ...]:
    orcamento, pagamento = ordem.resumo_orcamento, ordem.resumo_pagamento
    return (
        orcamento
        and (
            str(orcamento.orcamento_id),
            orcamento.total.valor,
            orcamento.total.moeda,
            orcamento.link_decisao,
            orcamento.valido_ate,
        ),
        pagamento
        and (
            str(pagamento.pagamento_id),
            pagamento.status.value,
            pagamento.valor.valor,
            pagamento.valor.moeda,
            pagamento.checkout_url,
            pagamento.expira_em,
        ),
    )


def _resumos_esperados(
    cenario: CenarioDaSaga, recebido: MensagemRecebida
) -> tuple[object, ...]:
    """Os resumos pelos eventos do Billing ja tratados (este inclusive)."""
    eventos = {**cenario.billing, recebido.tipo: recebido.dados}
    orcamento = eventos.get("OrcamentoGerado")
    pagamento = eventos.get("PagamentoSolicitado")
    return (
        orcamento
        and (
            orcamento["orcamento_id"],
            Decimal(orcamento["total"]),
            orcamento["moeda"],
            orcamento["link_decisao"],
            datetime.fromisoformat(orcamento["valido_ate"]),
        ),
        pagamento
        and (
            pagamento["pagamento_id"],
            "confirmado" if "PagamentoConfirmado" in eventos else "solicitado",
            Decimal(pagamento["valor"]),
            pagamento["moeda"],
            pagamento["checkout_url"],
            datetime.fromisoformat(pagamento["expira_em"]),
        ),
    )


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
    except EventoRecusadoError as exc:
        return f"recusada:{exc.motivo}"


# Perfis da OS com o resumo do pagamento: o estorno automatico o atualiza.
_COM_PAGAMENTO = {
    "aguardando_pagamento",
    "aguardando_execucao",
    "em_execucao",
    "finalizada",
    "entregue",
}


def _desfecho_esperado(etapa: str, tipo: str, perfil: str | None = None) -> str:
    perfil = perfil or perfil_de_entrada(etapa)
    classificacao = esperado(etapa, tipo, perfil).value
    if classificacao == "adiantado":
        return "adiantada"
    if classificacao == "ordem_encerrada":
        return "recusada:ordem_encerrada"
    if classificacao == "estorno_automatico":
        # So o resumo do pagamento muda, quando a OS o tem.
        return "processada" if perfil in _COM_PAGAMENTO else "ignorada"
    return "processada" if classificacao == "processar" else "ignorada"


@pytest.mark.parametrize(
    ("etapa", "tipo"),
    [
        pytest.param(etapa, tipo, id=f"{etapa}-{tipo}")
        for etapa in STATUS_DE_ENTRADA
        for tipo in ESPERADA
    ],
)
def test_matriz_pelo_handler_processa_ignora_adianta_ou_recusa(
    etapa: str, tipo: str
) -> None:
    cenario = CenarioDaSaga.em(etapa)
    assert cenario.ordem.status is STATUS_DE_ENTRADA[etapa]
    antes = _foto(cenario)

    desfecho = _desfecho(cenario, tipo)

    assert desfecho == _desfecho_esperado(etapa, tipo)
    if desfecho != "processada":
        # Ignorado, adiantado ou recusado nao toca saga, OS nem a outbox.
        assert _foto(cenario) == antes


@pytest.mark.parametrize(
    ("etapa", "perfil", "tipo"),
    [
        pytest.param(etapa, perfil, tipo, id=f"{etapa}-{perfil}-{tipo}")
        for etapa in STATUS_DE_ENTRADA
        for perfil in ("entregue", "cancelada")
        for tipo in ESPERADA
    ],
)
def test_os_encerrada_com_a_saga_viva_recusa_o_evento_sem_tocar_em_nada(
    etapa: str, perfil: str, tipo: str
) -> None:
    # O cancelamento recusa esse estado; se ele existisse, nenhum evento o
    # tocaria: nem comando para a OS encerrada, nem pagamento confirmado
    # consumido em silencio (a DLQ alerta).
    cenario = CenarioDaSaga.com_os(etapa, perfil)
    antes = _foto(cenario)

    desfecho = _desfecho(cenario, tipo)

    assert desfecho == _desfecho_esperado(etapa, tipo, perfil)
    if desfecho != "processada":
        assert _foto(cenario) == antes
    else:
        # So o estorno automatico passa: o resumo do pagamento muda, e a saga
        # e a outbox ficam como estavam.
        assert tipo == "PagamentoEstornado"
        depois = _foto(cenario)
        assert depois[:3] + depois[-1:] == antes[:3] + antes[-1:]
        assert cenario.ordem.resumo_pagamento is not None
        assert cenario.ordem.resumo_pagamento.status.value == "estornado"


def _foto(cenario: CenarioDaSaga) -> tuple[object, ...]:
    saga, ordem = cenario.saga, cenario.ordem
    return (
        saga.etapa,
        saga.passos,
        saga.comando_em_voo,
        ordem.status,
        ordem.historico,
        ordem.resumo_orcamento,
        ordem.resumo_pagamento,
        tuple(cenario.publicador.comandos),
    )


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


def _relogio_atras_do_ultimo_registro(
    cenario: CenarioDaSaga, atras: timedelta
) -> datetime:
    """A proxima leitura do relogio sai ``atras`` do ultimo registro da saga."""
    anterior = cenario.saga.atualizada_em
    # O relogio do cenario anda 1 s a cada leitura.
    cenario.relogio.agora = anterior - atras - timedelta(seconds=1)
    return anterior


@pytest.mark.parametrize(
    ("atras", "avisa"),
    [
        pytest.param(timedelta(milliseconds=5), False, id="5-ms"),
        pytest.param(timedelta(seconds=1), False, id="1-s"),
        pytest.param(timedelta(seconds=1, milliseconds=1), True, id="1-s-e-1-ms"),
        pytest.param(timedelta(seconds=60), True, id="60-s"),
    ],
)
def test_relogio_atras_do_ultimo_registro_nao_recua_o_registro(
    monkeypatch: pytest.MonkeyPatch, atras: timedelta, avisa: bool
) -> None:
    # A replica que trata o DiagnosticoConcluido le um relogio atras do que
    # gravou o DiagnosticoIniciado: o evento passa no instante do anterior, e
    # passando de 1 s o log mostra a defasagem.
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga()
    cenario.receber("DiagnosticoIniciado")
    anterior = _relogio_atras_do_ultimo_registro(cenario, atras)

    with capture_logs() as logs:
        tratamento = cenario.receber("DiagnosticoConcluido")

    assert tratamento.desfecho is Desfecho.PROCESSADA
    assert cenario.saga.passos[-1]["em"] == anterior.isoformat()
    assert cenario.saga.prazo_resposta_em == anterior + PRAZO
    avisos = [log for log in logs if log["event"] == "saga clock behind last record"]
    assert avisos == (
        [
            {
                "event": "saga clock behind last record",
                "log_level": "warning",
                "etapa": "aguardando_diagnostico",
                "defasagem_segundos": atras.total_seconds(),
                "correlation_id": str(cenario.ordem_id),
                "tipo": "DiagnosticoConcluido",
            }
        ]
        if avisa
        else []
    )


def test_relogio_mais_de_60_s_atras_do_ultimo_registro_vai_para_a_dlq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nao e jitter entre replicas: relogio fora de hora gravaria `em` e prazos
    # no futuro. Nada e gravado, e o log diz de quanto era a defasagem.
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga()
    cenario.receber("DiagnosticoIniciado")
    _relogio_atras_do_ultimo_registro(cenario, timedelta(seconds=60, milliseconds=1))
    antes = _foto(cenario)

    with capture_logs() as logs, pytest.raises(EventoRecusadoError) as exc:
        cenario.receber("DiagnosticoConcluido")

    assert (exc.value.motivo, exc.value.etapa) == (
        "relogio_defasado",
        E.AGUARDANDO_DIAGNOSTICO,
    )
    assert _foto(cenario) == antes
    assert [log["defasagem_segundos"] for log in logs] == [60.001]


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


@pytest.mark.parametrize(
    ("etapa", "tipo", "classificacao"),
    [
        pytest.param(
            "aguardando_decisao", "OrcamentoGerado", "obsoleto", id="obsoleto"
        ),
        pytest.param(
            "aguardando_orcamento", "DiagnosticoIniciado", "obsoleto", id="anterior"
        ),
        pytest.param("concluida", "ExecucaoFinalizada", "fora_do_fluxo", id="fluxo"),
        pytest.param(
            "aguardando_decisao", "ReservaLiberada", "fora_da_compensacao", id="comp"
        ),
    ],
)
def test_ignorado_sai_no_log_com_a_classificacao(
    monkeypatch: pytest.MonkeyPatch, etapa: str, tipo: str, classificacao: str
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga.em(etapa)

    with capture_logs() as logs:
        tratamento = cenario.receber(tipo)

    assert tratamento.desfecho is Desfecho.IGNORADA
    assert logs == [
        {
            "event": "saga event ignored",
            "log_level": "info",
            "etapa": etapa,
            "classificacao": classificacao,
            "correlation_id": str(cenario.ordem_id),
            "tipo": tipo,
        }
    ]


def test_repetido_na_mesma_etapa_sai_no_log_como_repetido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga()
    cenario.receber("DiagnosticoIniciado")

    with capture_logs() as logs:
        cenario.receber("DiagnosticoIniciado")

    assert [(log["event"], log["classificacao"]) for log in logs] == [
        ("saga event ignored", "repetido")
    ]


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
            "motivo": None,
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


@pytest.mark.parametrize(
    "falta",
    [pytest.param("saga", id="os-sem-saga"), pytest.param("os", id="saga-sem-os")],
)
def test_os_sem_saga_ou_saga_sem_os_vai_para_a_dlq(falta: str) -> None:
    cenario = CenarioDaSaga()
    if falta == "saga":
        # OS anterior a saga: nenhum ambiente persistente a tem (sem backfill).
        del cenario.sagas.sagas[cenario.ordem_id]
    else:
        del cenario.ordens.ordens[cenario.ordem_id]

    with pytest.raises(EventoRecusadoError) as exc:
        cenario.receber("DiagnosticoIniciado")

    assert (exc.value.motivo, exc.value.etapa) == ("saga_inexistente", None)


def test_ordem_id_dos_dados_diferente_do_correlation_id_e_recusado() -> None:
    cenario = CenarioDaSaga()

    with pytest.raises(EventoRecusadoError) as exc:
        cenario.receber("DiagnosticoIniciado", ordem_id=str(uuid4()))

    assert (exc.value.motivo, exc.value.etapa) == ("ordem_id_divergente", None)
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

    with pytest.raises(EventoRecusadoError) as exc:
        cenario.receber("PecasReservadas")

    assert (exc.value.motivo, exc.value.etapa) == (
        "transicao_invalida",
        E.AGUARDANDO_RESERVA,
    )
    assert isinstance(exc.value.__cause__, ViolacaoRegraDeNegocioException)
    # A OS recusa antes de mudar: nada de status novo nem comando.
    assert (sem_orcamento.status, len(sem_orcamento.historico)) == (
        StatusOrdem.AGUARDANDO_APROVACAO,
        0,
    )
    assert cenario.publicador.comandos[-1][0] == "ReservarPecas"
