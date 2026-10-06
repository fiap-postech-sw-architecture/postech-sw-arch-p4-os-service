"""Toda transicao legal e ilegal da maquina (9 x 9 = 81 pares)."""

from __future__ import annotations

from itertools import product

import pytest

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaException
from src.ordem_servico.dominio.maquina_de_status import MaquinaDeStatus
from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS, StatusOrdem

S = StatusOrdem

# Brief secao 2: fluxo feliz + CANCELADA antes de EM_EXECUCAO.
LEGAIS = frozenset(
    {
        (S.RECEBIDA, S.EM_DIAGNOSTICO),
        (S.EM_DIAGNOSTICO, S.AGUARDANDO_APROVACAO),
        (S.AGUARDANDO_APROVACAO, S.AGUARDANDO_PAGAMENTO),
        (S.AGUARDANDO_PAGAMENTO, S.AGUARDANDO_EXECUCAO),
        (S.AGUARDANDO_EXECUCAO, S.EM_EXECUCAO),
        (S.EM_EXECUCAO, S.FINALIZADA),
        (S.FINALIZADA, S.ENTREGUE),
        (S.RECEBIDA, S.CANCELADA),
        (S.EM_DIAGNOSTICO, S.CANCELADA),
        (S.AGUARDANDO_APROVACAO, S.CANCELADA),
        (S.AGUARDANDO_PAGAMENTO, S.CANCELADA),
        (S.AGUARDANDO_EXECUCAO, S.CANCELADA),
    }
)
ILEGAIS = sorted(set(product(S, S)) - LEGAIS)

_maquina = MaquinaDeStatus()


def test_contagem_de_pares() -> None:
    assert len(LEGAIS) == 12
    assert len(ILEGAIS) == 69


@pytest.mark.parametrize(("de", "para"), sorted(LEGAIS))
def test_transicao_legal_passa(de: StatusOrdem, para: StatusOrdem) -> None:
    _maquina.validar_transicao(de, para)  # nao levanta
    assert para in _maquina.transicoes_validas(de)


@pytest.mark.parametrize(("de", "para"), ILEGAIS)
def test_transicao_ilegal_levanta(de: StatusOrdem, para: StatusOrdem) -> None:
    with pytest.raises(TransicaoStatusInvalidaException) as exc:
        _maquina.validar_transicao(de, para)
    assert exc.value.codigo == "TRANSICAO_STATUS_INVALIDA"
    assert f"de {de.value} para {para.value}" in exc.value.mensagem


def test_mensagem_lista_as_transicoes_validas() -> None:
    with pytest.raises(TransicaoStatusInvalidaException, match="finalizada"):
        _maquina.validar_transicao(S.EM_EXECUCAO, S.CANCELADA)


@pytest.mark.parametrize("status", list(S))
def test_tabela_cobre_todo_status(status: StatusOrdem) -> None:
    assert isinstance(_maquina.transicoes_validas(status), frozenset)


def test_terminais_sao_exatamente_os_sem_saida() -> None:
    sem_saida = {s for s in S if not _maquina.transicoes_validas(s)}
    assert sem_saida == ESTADOS_TERMINAIS


def test_cancelada_so_antes_da_execucao() -> None:
    cancelaveis = {s for s in S if S.CANCELADA in _maquina.transicoes_validas(s)}
    assert cancelaveis == {
        S.RECEBIDA,
        S.EM_DIAGNOSTICO,
        S.AGUARDANDO_APROVACAO,
        S.AGUARDANDO_PAGAMENTO,
        S.AGUARDANDO_EXECUCAO,
    }
