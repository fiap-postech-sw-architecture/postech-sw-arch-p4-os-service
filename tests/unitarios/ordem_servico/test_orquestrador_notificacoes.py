"""O e-mail ao cliente sai da transicao da saga, no mesmo commit (RFC-004 secao 7.2).

Cada evento do fluxo e cada fim de compensacao pedem o e-mail do modelo do novo
status; o que nao muda o status (o comando seguinte, o evento ignorado) e o
evento recusado nao pedem nada.
"""

from __future__ import annotations

import pytest

from src.compartilhado.aplicacao.mensageria import Desfecho
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.aplicacao.saga.tratamento import (
    EventoAdiantadoError,
    EventoRecusadoError,
)
from src.ordem_servico.dominio.status import StatusOrdem
from tests.unitarios.ordem_servico.cenario_da_saga import (
    FLUXO_FELIZ,
    CenarioDaSaga,
)
from tests.unitarios.ordem_servico.test_orquestrador_compensacoes import (
    FALHAS,
    RESPOSTA,
)

_S = StatusOrdem
_M = ModeloDeNotificacao

# O e-mail que cada evento do caminho feliz pede (modelo e status), ou nenhum.
_DO_CAMINHO_FELIZ = {
    "DiagnosticoIniciado": (_M.SITUACAO, _S.EM_DIAGNOSTICO),
    "DiagnosticoConcluido": None,
    "OrcamentoGerado": (_M.ORCAMENTO, _S.AGUARDANDO_APROVACAO),
    "OrcamentoAprovado": None,
    # AGUARDANDO_PAGAMENTO nao tem e-mail: o do checkout vem no PagamentoSolicitado.
    "PecasReservadas": None,
    "PagamentoSolicitado": (_M.PAGAMENTO, _S.AGUARDANDO_PAGAMENTO),
    "PagamentoConfirmado": (_M.SITUACAO, _S.AGUARDANDO_EXECUCAO),
    "ExecucaoAgendada": None,
    "ExecucaoIniciada": (_M.SITUACAO, _S.EM_EXECUCAO),
    "ExecucaoFinalizada": (_M.SITUACAO, _S.FINALIZADA),
}


def test_a_tabela_cobre_todos_os_eventos_do_caminho_feliz() -> None:
    assert tuple(_DO_CAMINHO_FELIZ) == FLUXO_FELIZ


@pytest.mark.parametrize(("tipo", "pedido"), _DO_CAMINHO_FELIZ.items())
def test_evento_do_caminho_feliz_pede_o_e_mail_do_novo_status(
    tipo: str, pedido: tuple[ModeloDeNotificacao, StatusOrdem] | None
) -> None:
    cenario = CenarioDaSaga()
    for anterior in FLUXO_FELIZ[: FLUXO_FELIZ.index(tipo)]:
        cenario.receber(anterior)
    antes = len(cenario.notificacoes.registradas)

    cenario.receber(tipo)

    novas = cenario.notificacoes.registradas[antes:]
    esperadas = [] if pedido is None else [Notificacao(cenario.ordem_id, *pedido)]
    assert novas == esperadas


@pytest.mark.parametrize(("falha", "etapa", "motivo", "plano", "rotulo"), FALHAS)
def test_so_o_fim_da_compensacao_pede_o_e_mail_de_cancelamento(
    falha: str, etapa: str, motivo: str, plano: tuple[str, ...], rotulo: str
) -> None:
    cenario = CenarioDaSaga.em(etapa)
    if etapa == "aguardando_pagamento":
        cenario.receber("PagamentoSolicitado")
    antes = list(cenario.notificacoes.registradas)

    cenario.receber(falha)
    for pendente in plano[:-1]:
        cenario.receber(RESPOSTA[pendente])
    # Compensando e com compensacoes respondidas, a OS segue no status de antes.
    assert cenario.notificacoes.registradas == antes

    cenario.receber(RESPOSTA[plano[-1]])

    # O codigo da compensacao, nunca o texto: o rotulo sai na hora da entrega.
    assert cenario.notificacoes.registradas == [
        *antes,
        Notificacao(cenario.ordem_id, _M.CANCELAMENTO, _S.CANCELADA, motivo),
    ]


def test_cancelamento_do_atendente_leva_o_codigo_e_nao_o_texto() -> None:
    cenario = CenarioDaSaga.em("aguardando_decisao")
    antes = list(cenario.notificacoes.registradas)
    cenario.cancelar("Texto livre do atendente")

    # O pedido (202) ainda nao muda a OS: nenhum e-mail.
    assert cenario.notificacoes.registradas == antes
    while cenario.saga.etapa.value == "compensando":
        cenario.receber(RESPOSTA[cenario.saga.plano_compensacao[0]])

    (cancelamento,) = cenario.notificacoes.registradas[len(antes) :]
    assert cancelamento == Notificacao(
        cenario.ordem_id, _M.CANCELAMENTO, _S.CANCELADA, "cancelamento"
    )
    assert "Texto livre" not in repr(cancelamento)


def test_corrida_do_pivot_pede_o_e_mail_de_execucao_iniciada() -> None:
    cenario = CenarioDaSaga.em_compensacao(["CancelarExecucao", "DescartarDiagnostico"])

    cenario.receber("ExecucaoIniciada")

    assert cenario.notificacoes.registradas == [
        Notificacao(cenario.ordem_id, _M.SITUACAO, _S.EM_EXECUCAO)
    ]


def test_evento_repetido_nao_pede_e_mail() -> None:
    cenario = CenarioDaSaga.em("aguardando_pagamento")
    cenario.receber("PagamentoSolicitado")
    antes = list(cenario.notificacoes.registradas)

    tratamento = cenario.receber("PagamentoSolicitado")

    assert tratamento.desfecho is Desfecho.IGNORADA
    assert cenario.notificacoes.registradas == antes


def test_evento_adiantado_nao_pede_e_mail() -> None:
    cenario = CenarioDaSaga.em("aguardando_diagnostico")

    with pytest.raises(EventoAdiantadoError):
        cenario.receber("OrcamentoGerado")

    assert cenario.notificacoes.registradas == []


def test_evento_recusado_nao_pede_e_mail() -> None:
    cenario = CenarioDaSaga.em("aguardando_diagnostico")
    cenario.ordens.ordens.clear()

    recebido = cenario.evento("DiagnosticoIniciado")
    with pytest.raises(EventoRecusadoError):
        cenario.orquestrador.tratar(recebido)

    assert cenario.notificacoes.registradas == []


def test_estorno_automatico_nao_pede_e_mail() -> None:
    cenario = CenarioDaSaga.em("aguardando_pagamento")
    cenario.receber("PagamentoSolicitado")
    cenario.receber("PagamentoConfirmado")
    antes = list(cenario.notificacoes.registradas)

    cenario.receber("PagamentoEstornado", motivo="pagamento_apos_encerramento")

    assert cenario.notificacoes.registradas == antes
