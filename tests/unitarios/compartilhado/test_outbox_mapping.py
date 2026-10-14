"""Testes unitarios do NOTIFY da outbox (sem Postgres real).

``pg_notify_outbox`` deve ser no-op fora do Postgres (o backend sqlite de
teste nao tem ``pg_notify``): com ``dialect.name != 'postgresql'`` nenhum
``session.execute`` pode ser emitido. Session mockada, sem banco.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.compartilhado.infraestrutura.outbox_mapping import (
    CANAL_NOTIFY,
    pg_notify,
    pg_notify_outbox,
)


def _session_com_dialeto(nome: str) -> MagicMock:
    session = MagicMock()
    session.get_bind.return_value.dialect.name = nome
    return session


def test_pg_notify_nao_executa_fora_do_postgres() -> None:
    # Guard off-Postgres: dialeto 'sqlite' -> NENHUM execute (NOTIFY pulado).
    session = _session_com_dialeto("sqlite")

    pg_notify_outbox(session)

    session.execute.assert_not_called()


def test_pg_notify_executa_no_postgres() -> None:
    # Caminho positivo: em postgresql o NOTIFY e emitido na transacao corrente.
    session = _session_com_dialeto("postgresql")

    pg_notify_outbox(session)

    session.execute.assert_called_once()


def test_pg_notify_de_um_canal_qualquer_so_no_postgres() -> None:
    # O mesmo guard serve a outro canal (as notificacoes): sqlite pula, Postgres emite.
    sqlite = _session_com_dialeto("sqlite")
    postgres = _session_com_dialeto("postgresql")

    pg_notify(sqlite, "notificacao_nova")
    pg_notify(postgres, "notificacao_nova")

    sqlite.execute.assert_not_called()
    (chamada,) = postgres.execute.call_args_list
    assert chamada.args[1] == {"canal": "notificacao_nova"}


def test_pg_notify_outbox_usa_o_canal_do_relay() -> None:
    session = _session_com_dialeto("postgresql")

    pg_notify_outbox(session)

    assert session.execute.call_args.args[1] == {"canal": CANAL_NOTIFY}
