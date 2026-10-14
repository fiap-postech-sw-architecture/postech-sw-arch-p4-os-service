"""``ClientePort.obter_contato``: duas colunas, sem o agregado nem a chave (ADR-042)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.cliente_veiculo.dominio.contato import CONTATO_ANONIMIZADO
from src.compartilhado.infraestrutura.encryption import EncryptionService
from src.ordem_servico.aplicacao.dtos import ContatoDoCliente
from src.ordem_servico.infraestrutura.adapters import ClienteSQLAlchemyAdapter


def _adapter(
    linha: SimpleNamespace | None,
) -> tuple[ClienteSQLAlchemyAdapter, MagicMock]:
    session = MagicMock()
    session.execute.return_value.first.return_value = linha
    return ClienteSQLAlchemyAdapter(session), session


def test_le_so_nome_e_contato_sem_decifrar_o_documento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sem a ENCRYPTION_KEY: qualquer uso da cifragem estoura.
    monkeypatch.setattr(
        EncryptionService,
        "instance",
        classmethod(lambda cls: pytest.fail("nao pode tocar a cifragem")),
    )
    adapter, session = _adapter(SimpleNamespace(nome="Maria", contato="maria@x.com"))
    cliente_id = uuid4()

    contato = adapter.obter_contato(cliente_id)

    assert contato == ContatoDoCliente("Maria", "maria@x.com")
    (consulta,), _ = session.execute.call_args
    sql = str(consulta)
    assert "clientes.nome" in sql
    assert "clientes.contato" in sql
    # Nem o documento (o unico cifrado) nem o hash dele: duas colunas, so.
    assert "documento" not in sql
    assert consulta.compile().params == {"id_1": cliente_id}


def test_cliente_inexistente_nao_tem_contato() -> None:
    adapter, _ = _adapter(None)

    assert adapter.obter_contato(uuid4()) is None


def test_cliente_anonimizado_e_como_sem_contato() -> None:
    # O marcador da LGPD tem forma de e-mail: nunca pode virar destinatario.
    adapter, _ = _adapter(
        SimpleNamespace(nome="ANONIMIZADO", contato=CONTATO_ANONIMIZADO)
    )

    assert adapter.obter_contato(uuid4()) is None
