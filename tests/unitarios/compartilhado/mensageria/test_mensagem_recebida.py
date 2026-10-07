"""A mensagem recebida guarda os dados so para leitura e fora do repr."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from src.compartilhado.aplicacao.mensageria import MensagemRecebida
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS


def _mensagem() -> MensagemRecebida:
    envelope: dict[str, Any] = json.loads(
        (CONTRATOS / "exemplos/OrcamentoGerado.json").read_text()
    )
    return MensagemRecebida.do_envelope(envelope)


def test_repr_nao_mostra_os_dados() -> None:
    mensagem = _mensagem()

    assert "link_decisao" in mensagem.dados
    assert str(mensagem.dados["link_decisao"]) not in repr(mensagem)
    assert "dados" not in repr(mensagem)


def test_dados_sao_so_leitura_e_a_mensagem_tem_hash() -> None:
    mensagem = _mensagem()

    with pytest.raises(TypeError):
        cast("dict[str, Any]", mensagem.dados)["total"] = "0.00"
    assert hash(mensagem) == hash(_mensagem())
    assert mensagem == _mensagem()
