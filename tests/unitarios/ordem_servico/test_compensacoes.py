"""Compensacoes pelo orquestrador, com fakes (RFC-004 secoes 4.4, 4.5 e 4.7).

Cada falha de negocio ate a OS cancelada, cada compensacao pendente x cada
resposta (casada ou nao pelo ``causation_id``), o estorno automatico nos dois
motivos, a corrida do pivot nos dois sentidos e os eventos atrasados. O
esperado vem do oraculo do ``cenario_da_saga``, escrito a partir da RFC.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from src.compartilhado.aplicacao.mensageria import Comando, Desfecho
from src.ordem_servico.aplicacao.saga import orquestrador as modulo
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.compensacao import dados_da_compensacao
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.tratamento import Tratamento
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.resumos import StatusPagamento
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ordem_em
from tests.unitarios.ordem_servico.cenario_da_saga import (
    COMPENSACOES,
    RESPONDE,
    CenarioDaSaga,
    esperado,
)

C = Classificacao
E = EtapaSaga
S = StatusOrdem
# A resposta de cada compensacao, como o participante a publica.
RESPOSTA = {
    "CancelarExecucao": "ExecucaoCancelada",
    "EstornarPagamento": "PagamentoEstornado",
    "LiberarReserva": "ReservaLiberada",
    "CancelarOrcamento": "OrcamentoCancelado",
    "DescartarDiagnostico": "DiagnosticoDescartado",
}

# (falha, etapa em que chega, motivo, plano, rotulo da OS cancelada): as linhas
# de falha de negocio da RFC-004 secao 4.4.
FALHAS = [
    pytest.param(
        "GeracaoDeOrcamentoFalhou",
        "aguardando_orcamento",
        "geracao_falhou",
        COMPENSACOES[4:],
        "Falha na geração do orçamento",
        id="geracao",
    ),
    pytest.param(
        "OrcamentoRecusado",
        "aguardando_decisao",
        "orcamento_recusado",
        COMPENSACOES[4:],
        "Orçamento recusado pelo cliente",
        id="recusa",
    ),
    pytest.param(
        "OrcamentoExpirado",
        "aguardando_decisao",
        "orcamento_expirado",
        COMPENSACOES[4:],
        "Orçamento expirado sem decisão do cliente",
        id="expiracao",
    ),
    pytest.param(
        "ReservaDePecasFalhou",
        "aguardando_reserva",
        "reserva_falhou",
        COMPENSACOES[3:],
        "Falta de peça para a reserva",
        id="reserva",
    ),
    pytest.param(
        "PagamentoRecusado",
        "aguardando_pagamento",
        "pagamento_recusado",
        COMPENSACOES[2:],
        "Pagamento recusado",
        id="pagamento-recusado",
    ),
    pytest.param(
        "PagamentoExpirado",
        "aguardando_pagamento",
        "pagamento_expirado",
        COMPENSACOES[2:],
        "Pagamento não realizado no prazo",
        id="pagamento-expirado",
    ),
]


def _ate_a_falha(etapa: str) -> CenarioDaSaga:
    """Caminho feliz ate a ``etapa``; no pagamento, com o checkout aberto."""
    cenario = CenarioDaSaga.em(etapa)
    if etapa == "aguardando_pagamento":
        cenario.receber("PagamentoSolicitado")
    return cenario


def _responder_ate_o_fim(cenario: CenarioDaSaga) -> list[str]:
    """O participante responde a cada compensacao, casada pelo ``causation_id``."""
    respondidas = []
    while cenario.saga.etapa is E.COMPENSANDO:
        pendente = cenario.saga.plano_compensacao[0]
        respondidas.append(pendente)
        assert cenario.receber(RESPOSTA[pendente]).desfecho is Desfecho.PROCESSADA
    return respondidas


def _foto(cenario: CenarioDaSaga) -> tuple[object, ...]:
    saga, ordem = cenario.saga, cenario.ordem
    return (
        saga.etapa,
        saga.passos,
        saga.plano_compensacao,
        saga.comando_em_voo,
        ordem.status,
        ordem.historico,
        ordem.resumo_pagamento,
        tuple(cenario.publicador.comandos),
    )


def _envio_pendente(cenario: CenarioDaSaga) -> UUID:
    em_voo = cenario.saga.comando_em_voo
    assert em_voo is not None
    return UUID(em_voo["mensagem_ids"][-1])


@pytest.mark.parametrize(("falha", "etapa", "motivo", "plano", "rotulo"), FALHAS)
def test_falha_de_negocio_compensa_na_ordem_e_cancela_a_os(
    falha: str, etapa: str, motivo: str, plano: tuple[str, ...], rotulo: str
) -> None:
    cenario = _ate_a_falha(etapa)
    status = cenario.ordem.status
    enviados = len(cenario.publicador.comandos)
    recebido = cenario.evento(falha)

    tratamento = cenario.orquestrador.tratar(recebido)

    assert tratamento == Tratamento(Desfecho.PROCESSADA, E(etapa), E.COMPENSANDO)
    # A OS mantem o status enquanto a saga compensa (RFC-004 secao 4.1).
    assert cenario.ordem.status is status
    (_, dados, _, causation_id) = cenario.publicador.comandos[enviados]
    assert (dados["motivo"], causation_id) == (motivo, recebido.id)
    assert _responder_ate_o_fim(cenario) == list(plano)
    assert [c[0] for c in cenario.publicador.comandos[enviados:]] == list(plano)
    assert (cenario.saga.etapa, cenario.ordem.status) == (E.COMPENSADA, S.CANCELADA)
    assert cenario.ordem.motivo_cancelamento == rotulo
    ultima = cenario.ordem.historico[-1]
    assert (ultima.de, ultima.origem.value, ultima.motivo, ultima.ator) == (
        status,
        "saga",
        rotulo,
        "consumidor",
    )


@pytest.mark.parametrize("falha", ["PagamentoRecusado", "PagamentoExpirado"])
def test_pagamento_recusado_ou_expirado_passa_ao_resumo(falha: str) -> None:
    cenario = _ate_a_falha("aguardando_pagamento")

    cenario.receber(falha)

    resumo = cenario.ordem.resumo_pagamento
    assert resumo is not None
    assert resumo.status.value == falha.removeprefix("Pagamento").lower()


def test_recusa_registrada_pelo_atendente_vai_para_o_ator_do_registro() -> None:
    cenario = CenarioDaSaga.em("aguardando_decisao")
    atendente = str(uuid4())

    cenario.receber("OrcamentoRecusado", canal="atendente", decidido_por=atendente)

    assert cenario.saga.passos[-1]["ator"] == atendente


@pytest.mark.parametrize(
    ("status", "comando", "ids"),
    [
        pytest.param(
            S.EM_DIAGNOSTICO, "CancelarOrcamento", set(), id="orcamento-em-voo"
        ),
        pytest.param(
            S.AGUARDANDO_EXECUCAO, "CancelarOrcamento", {"orcamento_id"}, id="orcamento"
        ),
        pytest.param(
            S.AGUARDANDO_PAGAMENTO,
            "EstornarPagamento",
            {"pagamento_id"},
            id="pagamento",
        ),
        pytest.param(S.AGUARDANDO_EXECUCAO, "LiberarReserva", set(), id="reserva"),
        pytest.param(S.AGUARDANDO_EXECUCAO, "CancelarExecucao", set(), id="execucao"),
        pytest.param(
            S.AGUARDANDO_EXECUCAO, "DescartarDiagnostico", set(), id="diagnostico"
        ),
    ],
)
def test_dados_da_compensacao_levam_o_id_que_a_os_conhece(
    status: StatusOrdem, comando: str, ids: set[str]
) -> None:
    # Sem o id (o passo estava em voo), o participante acha o recurso pelo
    # ordem_id (RFC-004 secao 5.3).
    ordem = ordem_em(status)
    conhecidos = {
        "orcamento_id": ordem.resumo_orcamento
        and str(ordem.resumo_orcamento.orcamento_id),
        "pagamento_id": ordem.resumo_pagamento
        and str(ordem.resumo_pagamento.pagamento_id),
    }

    dados = dados_da_compensacao(
        Comando(comando), ordem.id, "cancelamento", MarcosDaOrdem.da_ordem(ordem)
    )

    assert dados == {
        "ordem_id": str(ordem.id),
        "motivo": "cancelamento",
        **{chave: conhecidos[chave] for chave in ids},
    }


@pytest.mark.parametrize(
    ("pendente", "tipo", "casada"),
    [
        pytest.param(p, t, c, id=f"{p}-{t}-{'casada' if c else 'outra'}")
        for p in COMPENSACOES
        for t in RESPONDE
        for c in (True, False)
    ],
)
def test_cada_compensacao_pendente_por_cada_resposta_pelo_handler(
    pendente: str, tipo: str, casada: bool
) -> None:
    plano = COMPENSACOES[COMPENSACOES.index(pendente) :]
    cenario = CenarioDaSaga.em_compensacao(plano)
    antes = _foto(cenario)
    enviados = len(cenario.publicador.comandos)
    causa = _envio_pendente(cenario) if casada else uuid4()
    recebido = evento(tipo, cenario.ordem_id, causation_id=causa)

    tratamento = cenario.orquestrador.tratar(recebido)

    novos = [c[0] for c in cenario.publicador.comandos[enviados:]]
    match esperado("compensando", tipo, pendente=pendente, casada=casada):
        case C.PROCESSAR if tipo == "EstornoDePagamentoFalhou":
            assert (tratamento.desfecho, tratamento.etapa_nova) == (
                Desfecho.PROCESSADA,
                E.FALHA_NA_COMPENSACAO,
            )
            # A sequencia para: nada sai, e a OS mantem o status.
            assert (novos, cenario.ordem.status) == ([], S.AGUARDANDO_EXECUCAO)
            assert cenario.saga.plano_compensacao == plano
        case C.PROCESSAR if len(plano) == 1:
            assert tratamento.etapa_nova is E.COMPENSADA
            assert (novos, cenario.ordem.status) == ([], S.CANCELADA)
        case C.PROCESSAR:
            assert tratamento.etapa_nova is E.COMPENSANDO
            assert novos == [plano[1]]
            assert cenario.publicador.comandos[-1][3] == recebido.id
            assert cenario.saga.plano_compensacao == plano[1:]
        case C.ESTORNO_AUTOMATICO:
            # So o resumo do pagamento muda: a saga e a outbox ficam.
            assert tratamento == Tratamento(
                Desfecho.PROCESSADA, E.COMPENSANDO, E.COMPENSANDO
            )
            assert cenario.ordem.resumo_pagamento is not None
            assert cenario.ordem.resumo_pagamento.status.value == "estornado"
            assert _foto(cenario)[:4] == antes[:4]
            assert novos == []
        case _:
            assert tratamento.desfecho is Desfecho.IGNORADA
            assert _foto(cenario) == antes


@pytest.mark.parametrize("motivo", ["compensacao", "pagamento_apos_encerramento"])
def test_estorno_casado_conclui_a_compensacao_com_qualquer_motivo(motivo: str) -> None:
    # O Billing responde ao comando com pagamento_apos_encerramento quando a
    # aprovacao tardia ja tinha sido estornada (RFC-004 secao 4.4).
    cenario = CenarioDaSaga.em_compensacao(COMPENSACOES[1:])

    tratamento = cenario.orquestrador.tratar(
        evento(
            "PagamentoEstornado",
            cenario.ordem_id,
            causation_id=_envio_pendente(cenario),
            motivo=motivo,
        )
    )

    assert tratamento.desfecho is Desfecho.PROCESSADA
    assert cenario.saga.plano_compensacao == COMPENSACOES[2:]
    assert cenario.publicador.comandos[-1][0] == "LiberarReserva"
    assert cenario.ordem.resumo_pagamento is not None
    assert cenario.ordem.resumo_pagamento.status.value == "estornado"


def _estorno_automatico(cenario: CenarioDaSaga) -> None:
    """O Billing estorna sozinho a aprovacao tardia (causa: o SolicitarPagamento)."""
    automatico = evento(
        "PagamentoEstornado",
        cenario.ordem_id,
        causation_id=uuid4(),
        motivo="pagamento_apos_encerramento",
    )
    assert cenario.orquestrador.tratar(automatico).desfecho is Desfecho.PROCESSADA


def test_cancelado_casado_depois_do_estorno_automatico_conclui_a_compensacao() -> None:
    # Duas replicas do consumidor podem entregar o estorno automatico antes da
    # resposta ao EstornarPagamento: o resumo ja esta estornado, e a resposta
    # casada nao pode prender a saga em compensando (sem saida pelo redrive).
    cenario = CenarioDaSaga.em_compensacao(
        COMPENSACOES[1:], status=S.AGUARDANDO_PAGAMENTO
    )
    _estorno_automatico(cenario)
    assert cenario.ordem.resumo_pagamento is not None
    assert cenario.ordem.resumo_pagamento.status.value == "estornado"

    tratamento = cenario.orquestrador.tratar(
        evento(
            "PagamentoCancelado",
            cenario.ordem_id,
            causation_id=_envio_pendente(cenario),
        )
    )

    assert tratamento == Tratamento(Desfecho.PROCESSADA, E.COMPENSANDO, E.COMPENSANDO)
    assert cenario.saga.plano_compensacao == COMPENSACOES[2:]
    assert cenario.publicador.comandos[-1][0] == "LiberarReserva"
    # O estado mais novo fica: o estorno nao volta a cancelado.
    assert cenario.ordem.resumo_pagamento.status.value == "estornado"


@pytest.mark.parametrize(
    ("estado_anterior", "resumo_depois"),
    [
        pytest.param("solicitado", "cancelado", id="em-aberto"),
        pytest.param("cancelado", "cancelado", id="repetida"),
        pytest.param("recusado", "recusado", id="recusado"),
        pytest.param("expirado", "expirado", id="expirado"),
        pytest.param("estornado", "estornado", id="estornado"),
        pytest.param("confirmado", "confirmado", id="confirmado"),
    ],
)
def test_cancelado_casado_conclui_a_compensacao_com_o_resumo_em_qualquer_estado(
    estado_anterior: str, resumo_depois: str
) -> None:
    confirmado = estado_anterior == "confirmado"
    cenario = CenarioDaSaga.em_compensacao(
        COMPENSACOES[1:],
        status=S.AGUARDANDO_EXECUCAO if confirmado else S.AGUARDANDO_PAGAMENTO,
    )
    if not confirmado and estado_anterior != "solicitado":
        cenario.ordem.registrar_estado_do_pagamento(StatusPagamento(estado_anterior))

    tratamento = cenario.orquestrador.tratar(
        evento(
            "PagamentoCancelado",
            cenario.ordem_id,
            causation_id=_envio_pendente(cenario),
        )
    )

    assert tratamento.desfecho is Desfecho.PROCESSADA
    assert cenario.saga.plano_compensacao == COMPENSACOES[2:]
    assert cenario.publicador.comandos[-1][0] == "LiberarReserva"
    assert cenario.ordem.resumo_pagamento is not None
    assert cenario.ordem.resumo_pagamento.status.value == resumo_depois


@pytest.mark.parametrize("anterior", ["cancelado", "estornado"])
def test_estornado_casado_conclui_a_compensacao_com_o_resumo_ja_cancelado_ou_estornado(
    anterior: str,
) -> None:
    # A resposta inversa: o PagamentoEstornado do comando chega com o resumo ja
    # cancelado (ou estornado pelo automatico) e o deixa estornado.
    cenario = CenarioDaSaga.em_compensacao(
        COMPENSACOES[1:], status=S.AGUARDANDO_PAGAMENTO
    )
    cenario.ordem.registrar_estado_do_pagamento(StatusPagamento(anterior))

    tratamento = cenario.orquestrador.tratar(
        evento(
            "PagamentoEstornado",
            cenario.ordem_id,
            causation_id=_envio_pendente(cenario),
            motivo="compensacao",
        )
    )

    assert tratamento.desfecho is Desfecho.PROCESSADA
    assert cenario.saga.plano_compensacao == COMPENSACOES[2:]
    assert cenario.publicador.comandos[-1][0] == "LiberarReserva"
    assert cenario.ordem.resumo_pagamento is not None
    assert cenario.ordem.resumo_pagamento.status.value == "estornado"


@pytest.mark.parametrize("motivo", ["compensacao", "pagamento_apos_encerramento"])
@pytest.mark.parametrize(
    "onde",
    [
        "estorno-pendente",
        "outra-pendente",
        "falha",
        "compensada",
        "checkout-aberto",
        "aguardando_agendamento",
        "aguardando_inicio",
        "em_execucao",
        "concluida",
    ],
)
def test_estorno_automatico_so_atualiza_o_resumo(motivo: str, onde: str) -> None:
    # O PagamentoEstornado do estorno automatico leva o id do SolicitarPagamento
    # e pode chegar em qualquer etapa: a saga nao muda nem envia comando.
    match onde:
        case "checkout-aberto":
            cenario = CenarioDaSaga.em("aguardando_pagamento")
            cenario.receber("PagamentoSolicitado")
        case "estorno-pendente":
            cenario = CenarioDaSaga.em_compensacao(COMPENSACOES[1:])
        case "outra-pendente":
            cenario = CenarioDaSaga.em_compensacao(COMPENSACOES[2:])
        case "falha":
            cenario = CenarioDaSaga.em_compensacao(
                COMPENSACOES[1:], falha="estorno_recusado"
            )
        case "compensada":
            cenario = CenarioDaSaga.em_compensacao(COMPENSACOES[4:])
            cenario.receber("DiagnosticoDescartado")
        case _:
            cenario = CenarioDaSaga.em(onde)
    antes = _foto(cenario)
    ordens_salvas, sagas_salvas = len(cenario.ordens.salvas), len(cenario.sagas.salvas)
    # O causation_id e o do SolicitarPagamento, nunca o de um envio em voo.
    automatico = evento(
        "PagamentoEstornado", cenario.ordem_id, causation_id=uuid4(), motivo=motivo
    )

    tratamento = cenario.orquestrador.tratar(automatico)

    # So a OS e gravada (o resumo); a saga fica como estava.
    assert len(cenario.ordens.salvas) == ordens_salvas + 1
    assert len(cenario.sagas.salvas) == sagas_salvas

    assert tratamento == Tratamento(
        Desfecho.PROCESSADA, cenario.saga.etapa, cenario.saga.etapa
    )
    assert cenario.ordem.resumo_pagamento is not None
    assert cenario.ordem.resumo_pagamento.status.value == "estornado"
    depois = _foto(cenario)
    assert (depois[:6], depois[-1]) == (antes[:6], antes[-1])


@pytest.mark.parametrize(
    "etapa",
    [
        "aguardando_diagnostico",
        "aguardando_orcamento",
        "aguardando_decisao",
        "aguardando_reserva",
        "aguardando_pagamento",
    ],
)
def test_estorno_automatico_sem_resumo_e_ignorado_nas_etapas_antes_do_checkout(
    monkeypatch: pytest.MonkeyPatch, etapa: str
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga.em(etapa)
    antes = _foto(cenario)
    salvas = len(cenario.ordens.salvas)

    with capture_logs() as logs:
        tratamento = cenario.receber("PagamentoEstornado")

    assert tratamento.desfecho is Desfecho.IGNORADA
    assert [(log["event"], log["classificacao"]) for log in logs] == [
        ("saga event ignored", "estorno_automatico")
    ]
    assert _foto(cenario) == antes
    assert len(cenario.ordens.salvas) == salvas


def test_estorno_automatico_repetido_e_ignorado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    estornado = CenarioDaSaga.em("aguardando_inicio")
    estornado.receber("PagamentoEstornado")
    antes = _foto(estornado)

    with capture_logs() as logs:
        tratamento = estornado.receber("PagamentoEstornado")

    assert tratamento.desfecho is Desfecho.IGNORADA
    assert [(log["event"], log["classificacao"]) for log in logs] == [
        ("saga event ignored", "estorno_automatico")
    ]
    assert _foto(estornado) == antes


def test_estorno_automatico_sai_no_log_sem_os_dados_do_pagamento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga.em("aguardando_inicio")

    with capture_logs() as logs:
        cenario.receber("PagamentoEstornado", motivo="pagamento_apos_encerramento")

    assert logs == [
        {
            "event": "late payment refunded",
            "log_level": "info",
            "etapa": "aguardando_inicio",
            "correlation_id": str(cenario.ordem_id),
            "tipo": "PagamentoEstornado",
        }
    ]


@pytest.mark.parametrize("etapa", ["compensando", "falha_na_compensacao"])
def test_inicio_da_execucao_vence_o_cancelamento(etapa: str) -> None:
    # A Execucao iniciou antes de receber o CancelarExecucao: nada foi
    # desfeito, a OS vai a EM_EXECUCAO e o pedido e descartado (RFC-004 4.5).
    cenario = CenarioDaSaga.em_compensacao(
        COMPENSACOES, falha="reenvios_esgotados" if etapa != "compensando" else None
    )
    cenario.ordem.registrar_pedido_de_cancelamento("cliente desistiu")
    enviados = len(cenario.publicador.comandos)

    tratamento = cenario.receber("ExecucaoIniciada")

    assert tratamento == Tratamento(Desfecho.PROCESSADA, E(etapa), E.EM_EXECUCAO)
    assert (cenario.ordem.status, cenario.ordem.motivo_cancelamento) == (
        S.EM_EXECUCAO,
        None,
    )
    passo = cenario.saga.passos[-1]
    assert (passo["gatilho"], passo["motivo"]) == ("cancelamento_recusado", None)
    assert (cenario.saga.motivo, cenario.saga.plano_compensacao) == (None, ())
    assert len(cenario.publicador.comandos) == enviados
    # Dai em diante, o fluxo normal: a finalizacao conclui a saga.
    cenario.receber("ExecucaoFinalizada")
    assert (cenario.saga.etapa, cenario.ordem.status) == (E.CONCLUIDA, S.FINALIZADA)


def test_cancelamento_vence_o_inicio_e_o_inicio_atrasado_e_incoerente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga.em_compensacao(COMPENSACOES)
    cenario.receber("ExecucaoCancelada")
    assert cenario.publicador.comandos[-1][0] == "EstornarPagamento"
    antes = _foto(cenario)

    with capture_logs() as logs:
        tratamento = cenario.receber("ExecucaoIniciada")

    # A Execucao confirmou o cancelamento: o inicio contradiz o que a saga sabe.
    assert tratamento.desfecho is Desfecho.IGNORADA
    assert _foto(cenario) == antes
    assert logs == [
        {
            "event": "saga event ignored",
            "log_level": "error",
            "etapa": "compensando",
            "classificacao": "incoerente",
            "correlation_id": str(cenario.ordem_id),
            "tipo": "ExecucaoIniciada",
        }
    ]


@pytest.mark.parametrize("tipo", ["OrcamentoAprovado", "PagamentoConfirmado"])
@pytest.mark.parametrize("etapa", ["compensando", "compensada"])
def test_aprovacao_ou_pagamento_atrasados_sao_ignorados(etapa: str, tipo: str) -> None:
    # Isolamento (RFC-004 secao 4.5): a decisao que chega depois do cancelamento
    # nao reabre a saga; o plano ja cancela o orcamento e devolve o pagamento.
    cenario = CenarioDaSaga.em_compensacao(
        COMPENSACOES[3:], status=S.AGUARDANDO_APROVACAO
    )
    if etapa == "compensada":
        _responder_ate_o_fim(cenario)
    antes = _foto(cenario)

    tratamento = cenario.receber(tipo)

    assert tratamento.desfecho is Desfecho.IGNORADA
    assert _foto(cenario) == antes


def test_os_cancelada_pela_saga_leva_o_texto_do_pedido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cenario = CenarioDaSaga.em_compensacao(
        COMPENSACOES[3:], status=S.AGUARDANDO_APROVACAO
    )
    cenario.ordem.registrar_pedido_de_cancelamento("Cliente vendeu o carro")

    _responder_ate_o_fim(cenario)

    assert cenario.ordem.status is S.CANCELADA
    assert cenario.ordem.historico[-1].motivo == "Cliente vendeu o carro"
    assert cenario.ordem.motivo_cancelamento == "Cliente vendeu o carro"


def test_transicao_da_compensacao_sai_no_log_com_o_motivo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modulo, "_log", structlog.get_logger())
    cenario = CenarioDaSaga.em("aguardando_decisao")

    with capture_logs() as logs:
        cenario.receber("OrcamentoRecusado")

    assert logs == [
        {
            "event": "saga transition",
            "log_level": "info",
            "etapa": "aguardando_decisao",
            "etapa_nova": "compensando",
            "status": "aguardando_aprovacao",
            "motivo": "orcamento_recusado",
            "correlation_id": str(cenario.ordem_id),
            "tipo": "OrcamentoRecusado",
        }
    ]
