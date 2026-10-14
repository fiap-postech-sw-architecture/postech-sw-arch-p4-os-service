"""``RetomarCompensacao``: a retomada da compensacao parada (RFC-004 4.7 e 6.1).

Sobre a saga e a OS em memoria: o comando guardado sai de novo com id novo, a
saga volta a ``compensando`` e segue o plano ate ``compensada``; fora de
``falha_na_compensacao``, 409, sem comando nem commit.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    TransicaoStatusInvalidaException,
)
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    SagaNaoEncontradaException,
)
from src.ordem_servico.aplicacao.use_cases import (
    RETOMADA_RECUSADA,
    RetomarCompensacao,
)
from tests.fabricas import ATOR_ATENDENTE
from tests.unitarios.fakes import FakeUnitOfWork
from tests.unitarios.ordem_servico.cenario_da_saga import CenarioDaSaga

PLANO = ("EstornarPagamento", "LiberarReserva", "CancelarOrcamento")


def _retomada(cenario: CenarioDaSaga) -> tuple[RetomarCompensacao, FakeUnitOfWork]:
    uow = FakeUnitOfWork()
    contexto = ContextoDeEnvio(timedelta(seconds=120), cenario.relogio)
    return RetomarCompensacao(uow, cenario.sagas, contexto), uow


def _parada(falha: str = "estorno_recusado") -> CenarioDaSaga:
    return CenarioDaSaga.em_compensacao(PLANO, falha=falha)


@pytest.mark.parametrize("falha", ["estorno_recusado", "reenvios_esgotados"])
def test_reenvia_a_pendente_com_id_novo_e_volta_a_compensando(falha: str) -> None:
    cenario = _parada(falha)
    caso_de_uso, uow = _retomada(cenario)
    antes = cenario.saga.comando_em_voo
    assert antes is not None

    dto = caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    (tipo, dados, correlation_id, causation_id) = uow.comandos[0]
    # O comando guardado de novo: mesmo tipo e dados, causa nula (a requisicao).
    assert (tipo, dados, correlation_id, causation_id) == (
        "EstornarPagamento",
        antes["dados"],
        cenario.ordem_id,
        None,
    )
    assert (dto.etapa, dto.falha, dto.plano_compensacao) == (
        "compensando",
        None,
        PLANO,
    )
    assert dto.comando_em_voo is not None
    assert dto.comando_em_voo.tipo == "EstornarPagamento"
    assert dto.reenvios == 0
    assert dto.prazo_resposta_em is not None
    assert cenario.saga.comando_em_voo["mensagem_ids"] == [
        *antes["mensagem_ids"],
        uow.envelopes[0]["id"],
    ]
    assert dto.passos[-1]["gatilho"] == "retomada"
    assert dto.passos[-1]["ator"] == ATOR_ATENDENTE
    assert uow.committed


def test_a_retomada_chega_a_compensada() -> None:
    cenario = _parada()
    caso_de_uso, _ = _retomada(cenario)
    caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    for resposta in ("PagamentoEstornado", "ReservaLiberada"):
        cenario.receber(resposta)
        assert cenario.saga.etapa is EtapaSaga.COMPENSANDO

    cenario.receber("OrcamentoCancelado")

    assert cenario.saga.etapa is EtapaSaga.COMPENSADA
    assert cenario.ordem.status.value == "cancelada"


@pytest.mark.parametrize(
    "etapa",
    [
        "aguardando_diagnostico",
        "aguardando_orcamento",
        "aguardando_decisao",
        "em_execucao",
        "concluida",
    ],
)
def test_fora_da_falha_na_compensacao_e_409_sem_comando(etapa: str) -> None:
    cenario = CenarioDaSaga.em(etapa)
    caso_de_uso, uow = _retomada(cenario)

    with (
        capture_logs() as logs,
        pytest.raises(TransicaoStatusInvalidaException) as erro,
    ):
        caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    assert (erro.value.codigo, erro.value.mensagem) == (
        "TRANSICAO_STATUS_INVALIDA",
        RETOMADA_RECUSADA,
    )
    assert (uow.comandos, uow.committed) == ([], False)
    recusa = next(e for e in logs if e["event"] == "saga resume refused")
    assert (recusa["correlation_id"], recusa["ator"], recusa["resultado"]) == (
        str(cenario.ordem_id),
        ATOR_ATENDENTE,
        "TRANSICAO_STATUS_INVALIDA",
    )


def test_compensando_nao_e_retomada() -> None:
    cenario = CenarioDaSaga.em_compensacao(PLANO)
    caso_de_uso, uow = _retomada(cenario)

    with pytest.raises(TransicaoStatusInvalidaException):
        caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    assert uow.comandos == []


def test_ordem_sem_saga_e_404() -> None:
    cenario = _parada()
    caso_de_uso, uow = _retomada(cenario)

    with pytest.raises(SagaNaoEncontradaException):
        caso_de_uso.executar(uuid4(), ator=ATOR_ATENDENTE)

    assert uow.comandos == []


def test_conflito_de_versao_e_409_e_nao_deixa_comando() -> None:
    cenario = _parada()
    caso_de_uso, uow = _retomada(cenario)
    cenario.sagas.provocar_conflito()

    with capture_logs() as logs, pytest.raises(ConflitoDeConcorrenciaException):
        caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    assert uow.committed is False
    assert uow.rolled_back is True
    assert any(
        e["event"] == "saga resume refused"
        and e["resultado"] == "CONFLITO_DE_CONCORRENCIA"
        for e in logs
    )


def test_auditoria_do_aceite_leva_ator_e_correlacao_sem_dados_do_comando() -> None:
    cenario = _parada()
    caso_de_uso, _ = _retomada(cenario)

    with capture_logs() as logs:
        caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    (aceite,) = [e for e in logs if e["event"] == "saga resumed"]
    assert aceite["correlation_id"] == str(cenario.ordem_id)
    assert aceite["ator"] == ATOR_ATENDENTE
    assert (aceite["etapa"], aceite["comando"]) == ("compensando", "EstornarPagamento")
    assert "pagamento_id" not in str(logs)


def test_uma_segunda_retomada_com_a_saga_ja_compensando_e_409() -> None:
    cenario = _parada()
    caso_de_uso, uow = _retomada(cenario)
    caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    with pytest.raises(TransicaoStatusInvalidaException):
        caso_de_uso.executar(cenario.ordem_id, ator=ATOR_ATENDENTE)

    assert len(uow.comandos) == 1
    assert UUID(uow.envelopes[0]["id"]) in {
        UUID(i) for i in cenario.saga.comando_em_voo["mensagem_ids"]
    }
