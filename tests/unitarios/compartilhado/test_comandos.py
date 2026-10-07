"""Os comandos que a aplicacao publica sao os do contrato, e o fake os registra."""

from __future__ import annotations

from uuid import uuid4

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from tests.unitarios.fakes import FakeUnitOfWork


def test_comandos_da_aplicacao_sao_os_que_o_contrato_diz_que_o_os_publica() -> None:
    assert {comando.value for comando in Comando} == catalogo().publicados


def test_fake_da_unidade_de_trabalho_registra_os_comandos_publicados() -> None:
    uow = FakeUnitOfWork()
    ordem_id, causa = uuid4(), uuid4()

    mensagem_id = uow.publicar_comando(
        Comando.DESCARTAR_DIAGNOSTICO,
        {"ordem_id": ordem_id, "motivo": "cancelamento"},
        correlation_id=ordem_id,
        causation_id=causa,
    )

    assert mensagem_id is not None
    assert uow.comandos == [
        (
            Comando.DESCARTAR_DIAGNOSTICO,
            {"ordem_id": ordem_id, "motivo": "cancelamento"},
            ordem_id,
            causa,
        )
    ]
    assert not uow.committed
