"""Plano de compensacao: as 11 linhas da tabela da RFC-004 secao 4.4.

O que a saga ja fez em cada etapa (``FEITO``) e o plano de cada linha
(``PLANO_DA_RFC``) ficam no cenario, escritos a partir da RFC e nao lidos do
codigo.
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
from tests.unitarios.ordem_servico.cenario_da_saga import (
    COMPENSACOES,
    ESPERADA,
    FEITO,
    PLANO_DA_RFC,
)

# Cada linha da tabela, com uma entrada por gatilho quando a linha junta dois.
LINHAS = [
    pytest.param(etapa, motivo, plano, id=f"{etapa}-{motivo}")
    for (etapa, motivo), plano in PLANO_DA_RFC.items()
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
    assert {c.value for c in RESPOSTA_DA_COMPENSACAO.values()} == set(COMPENSACOES)


def test_cada_motivo_tem_rotulo_para_a_os_cancelada() -> None:
    assert set(ROTULO_DO_MOTIVO) == set(MotivoCompensacao)
    assert all(0 < len(rotulo) <= 500 for rotulo in ROTULO_DO_MOTIVO.values())
