"""Plano de compensacao: as 11 linhas da tabela da RFC-004 secao 4.4.

O que a saga ja fez em cada etapa (passos concluidos e comando em voo) e o
plano esperado sao escritos aqui a partir da RFC, e nao lidos do codigo.
"""

from __future__ import annotations

import pytest

from src.ordem_servico.aplicacao.saga.compensacao import (
    MOTIVO_DA_FALHA,
    RESPOSTA_DA_COMPENSACAO,
    ROTULO_DO_MOTIVO,
    plano_de_compensacao,
)
from src.ordem_servico.aplicacao.saga.modelo import MotivoCompensacao
from tests.unitarios.ordem_servico.cenario_da_saga import ESPERADA

# O que a saga ja fez em cada etapa: os passos T concluidos e o comando com
# prazo que espera resposta (RFC-004 secoes 4.1 e 4.3). No pagamento, antes e
# depois do PagamentoSolicitado (o checkout aberto conclui o T6).
FEITO = {
    "aguardando_diagnostico": ((), None),
    "aguardando_orcamento": ((), "GerarOrcamento"),
    "aguardando_decisao": (("T3",), None),
    "aguardando_reserva": (("T3",), "ReservarPecas"),
    "aguardando_pagamento": (("T3", "T5"), "SolicitarPagamento"),
    "aguardando_pagamento_com_checkout": (("T3", "T5", "T6"), None),
    "aguardando_agendamento": (("T3", "T5", "T6"), "AgendarExecucao"),
    "aguardando_inicio": (("T3", "T5", "T6", "T7"), None),
}
TODAS = (
    "CancelarExecucao",
    "EstornarPagamento",
    "LiberarReserva",
    "CancelarOrcamento",
    "DescartarDiagnostico",
)
# (etapa, motivo, compensacoes em ordem): cada linha da tabela, com uma
# entrada por gatilho quando a linha junta dois ("cancelamento ou prazo").
LINHAS = [
    pytest.param("aguardando_diagnostico", "cancelamento", TODAS[4:], id="1"),
    pytest.param("aguardando_orcamento", "geracao_falhou", TODAS[4:], id="2"),
    pytest.param("aguardando_orcamento", "cancelamento", TODAS[3:], id="3-cancel"),
    pytest.param("aguardando_orcamento", "prazo_tecnico", TODAS[3:], id="3-prazo"),
    pytest.param("aguardando_decisao", "orcamento_recusado", TODAS[4:], id="4-rec"),
    pytest.param("aguardando_decisao", "orcamento_expirado", TODAS[4:], id="4-exp"),
    pytest.param("aguardando_decisao", "cancelamento", TODAS[3:], id="5"),
    pytest.param("aguardando_reserva", "reserva_falhou", TODAS[3:], id="6"),
    pytest.param("aguardando_reserva", "cancelamento", TODAS[2:], id="7-cancel"),
    pytest.param("aguardando_reserva", "prazo_tecnico", TODAS[2:], id="7-prazo"),
    pytest.param(
        "aguardando_pagamento_com_checkout", "pagamento_recusado", TODAS[2:], id="8-rec"
    ),
    pytest.param(
        "aguardando_pagamento_com_checkout", "pagamento_expirado", TODAS[2:], id="8-exp"
    ),
    pytest.param("aguardando_pagamento", "cancelamento", TODAS[1:], id="9-em-voo"),
    pytest.param(
        "aguardando_pagamento_com_checkout", "cancelamento", TODAS[1:], id="9-checkout"
    ),
    pytest.param("aguardando_pagamento", "prazo_tecnico", TODAS[1:], id="9-prazo"),
    pytest.param("aguardando_agendamento", "cancelamento", TODAS, id="10-cancel"),
    pytest.param("aguardando_agendamento", "prazo_tecnico", TODAS, id="10-prazo"),
    pytest.param("aguardando_inicio", "cancelamento", TODAS, id="11"),
]


@pytest.mark.parametrize(("etapa", "motivo", "compensacoes"), LINHAS)
def test_plano_de_cada_linha_da_rfc(
    etapa: str, motivo: str, compensacoes: tuple[str, ...]
) -> None:
    concluidos, em_voo = FEITO[etapa]

    plano = plano_de_compensacao(concluidos, em_voo, MotivoCompensacao(motivo))

    assert [c.value for c in plano] == list(compensacoes)


def test_falhas_de_negocio_e_respostas_sao_as_do_contrato() -> None:
    # As seis falhas de negocio que iniciam a compensacao (RFC-004 secao 4.1) e
    # as sete respostas de compensacao que a saga espera em compensando.
    assert set(MOTIVO_DA_FALHA) == {
        "GeracaoDeOrcamentoFalhou",
        "OrcamentoRecusado",
        "OrcamentoExpirado",
        "ReservaDePecasFalhou",
        "PagamentoRecusado",
        "PagamentoExpirado",
    }
    assert set(RESPOSTA_DA_COMPENSACAO) == {
        tipo for tipo, etapa in ESPERADA.items() if etapa == "compensando"
    }
    assert {c.value for c in RESPOSTA_DA_COMPENSACAO.values()} == set(TODAS)


def test_cada_motivo_tem_rotulo_para_a_os_cancelada() -> None:
    assert set(ROTULO_DO_MOTIVO) == set(MotivoCompensacao)
    assert all(0 < len(rotulo) <= 500 for rotulo in ROTULO_DO_MOTIVO.values())
