"""Quando a OS pede e-mail ao cliente e de qual modelo (RFC-004 secoes 3 e 7.2)."""

from __future__ import annotations

import pytest

from src.ordem_servico.aplicacao.notificacoes import (
    MODELO_DO_STATUS,
    ModeloDeNotificacao,
    Notificacao,
    registrar_transicao,
)
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import (
    ATOR_ATENDENTE,
    FLUXO,
    aplicar_fato,
    ordem_em,
    solicitar_pagamento,
)
from tests.unitarios.fakes import NotificacoesEspia

_S = StatusOrdem
_M = ModeloDeNotificacao


def test_todo_status_tem_decisao_de_notificacao() -> None:
    # Um status novo sem decisao cairia no KeyError so em producao.
    assert set(MODELO_DO_STATUS) == set(StatusOrdem)


@pytest.mark.parametrize(
    ("para", "modelo"),
    [
        pytest.param(_S.EM_DIAGNOSTICO, _M.SITUACAO, id="em_diagnostico"),
        pytest.param(_S.AGUARDANDO_APROVACAO, _M.ORCAMENTO, id="orcamento"),
        pytest.param(_S.AGUARDANDO_PAGAMENTO, None, id="checkout-vem-em-seguida"),
        pytest.param(_S.AGUARDANDO_EXECUCAO, _M.SITUACAO, id="pagamento-confirmado"),
        pytest.param(_S.EM_EXECUCAO, _M.SITUACAO, id="execucao-iniciada"),
        pytest.param(_S.FINALIZADA, _M.SITUACAO, id="finalizada"),
        pytest.param(_S.ENTREGUE, _M.SITUACAO, id="entregue"),
    ],
)
def test_cada_status_do_fluxo_pede_o_e_mail_do_seu_modelo(
    para: StatusOrdem, modelo: ModeloDeNotificacao | None
) -> None:
    antes = FLUXO[FLUXO.index(para) - 1]
    ordem = ordem_em(antes)
    aplicar_fato(ordem, para)
    espia = NotificacoesEspia()

    registrar_transicao(espia, ordem, status_antes=antes)

    esperadas = [] if modelo is None else [Notificacao(ordem.id, modelo, para)]
    assert espia.registradas == esperadas


def test_a_abertura_nao_pede_e_mail() -> None:
    # `recebida` nao e transicao do hook: a abertura nem o chama, e se chamasse
    # (status igual ao de antes) nada sairia.
    ordem = ordem_em(_S.RECEBIDA)
    espia = NotificacoesEspia()

    registrar_transicao(espia, ordem, status_antes=_S.RECEBIDA)

    assert espia.registradas == []
    assert MODELO_DO_STATUS[_S.RECEBIDA] is None


def test_o_cancelamento_pede_o_e_mail_com_o_codigo_do_motivo() -> None:
    ordem = ordem_em(_S.AGUARDANDO_EXECUCAO)
    ordem.cancelar("Texto do atendente", OrigemMudanca.SAGA, ator=ATOR_ATENDENTE)
    espia = NotificacoesEspia()

    registrar_transicao(
        espia, ordem, status_antes=_S.AGUARDANDO_EXECUCAO, motivo="cancelamento"
    )

    # O codigo, e nunca o texto livre que o atendente escreveu.
    assert espia.registradas == [
        Notificacao(ordem.id, _M.CANCELAMENTO, _S.CANCELADA, "cancelamento")
    ]


def test_so_o_cancelamento_leva_o_motivo() -> None:
    ordem = ordem_em(_S.EM_DIAGNOSTICO)
    aplicar_fato(ordem, _S.AGUARDANDO_APROVACAO)
    espia = NotificacoesEspia()

    registrar_transicao(
        espia, ordem, status_antes=_S.EM_DIAGNOSTICO, motivo="orcamento_recusado"
    )

    assert espia.registradas == [
        Notificacao(ordem.id, _M.ORCAMENTO, _S.AGUARDANDO_APROVACAO)
    ]


def test_o_checkout_aberto_pede_o_e_mail_de_pagamento_sem_mudar_o_status() -> None:
    ordem = ordem_em(_S.AGUARDANDO_APROVACAO)
    ordem.registrar_pecas_reservadas(ator="consumidor")
    solicitar_pagamento(ordem)
    espia = NotificacoesEspia()

    registrar_transicao(
        espia, ordem, status_antes=_S.AGUARDANDO_PAGAMENTO, abriu_o_checkout=True
    )

    assert espia.registradas == [
        Notificacao(ordem.id, _M.PAGAMENTO, _S.AGUARDANDO_PAGAMENTO)
    ]
