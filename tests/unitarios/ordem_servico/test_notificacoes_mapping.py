"""A gravacao do pedido de e-mail: o INSERT e o NOTIFY que acorda o notificador."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.notificacoes_mapping import (
    CANAL_NOTIFICACAO,
    NotificacoesSQLAlchemy,
)


def _registrar(dialeto: str) -> MagicMock:
    session = MagicMock()
    session.get_bind.return_value.dialect.name = dialeto
    NotificacoesSQLAlchemy(session).registrar(
        Notificacao(uuid4(), ModeloDeNotificacao.SITUACAO, StatusOrdem.ENTREGUE)
    )
    return session


def test_no_postgres_grava_o_pedido_e_emite_o_notify_do_canal_dos_pedidos() -> None:
    session = _registrar("postgresql")

    insert, notify = (chamada.args for chamada in session.execute.call_args_list)
    assert insert[0].table.name == "notificacoes"
    assert notify[1] == {"canal": CANAL_NOTIFICACAO}


def test_fora_do_postgres_so_grava_o_pedido() -> None:
    session = _registrar("sqlite")

    session.execute.assert_called_once()
