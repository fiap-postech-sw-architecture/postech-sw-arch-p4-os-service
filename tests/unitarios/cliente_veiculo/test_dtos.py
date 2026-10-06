"""Dado pessoal fica fora do ``repr`` dos DTOs (vai para log e traceback)."""

from __future__ import annotations

from uuid import uuid4

from src.cliente_veiculo.aplicacao.dtos import (
    AdicionarVeiculoDTO,
    ClienteDTO,
    CriarClienteDTO,
    VeiculoDTO,
)

_PLACA = "ABC1D23"


def test_adicionar_veiculo_nao_expoe_a_placa() -> None:
    dto = AdicionarVeiculoDTO(placa=_PLACA, marca="Fiat", modelo="Uno", ano=2020)
    assert _PLACA not in repr(dto)
    assert "Fiat" in repr(dto)


def test_cliente_com_veiculos_nao_expoe_placa_nem_documento() -> None:
    veiculo = VeiculoDTO(id=uuid4(), placa=_PLACA, marca="Fiat", modelo="Uno", ano=2020)
    dto = ClienteDTO(
        id=uuid4(),
        nome="Joao Silva",
        documento_formatado="529.982.247-25",
        documento_mascarado="***.***.***-25",
        tipo_documento="cpf",
        contato="joao@exemplo.com",
        ativo=True,
        veiculos=[veiculo],
    )
    texto = repr(dto)
    for pii in (_PLACA, "Joao", "529.982.247-25", "joao@exemplo.com"):
        assert pii not in texto


def test_criar_cliente_nao_expoe_nome_documento_nem_contato() -> None:
    dto = CriarClienteDTO(
        nome="Joao Silva",
        documento="52998224725",
        tipo_documento="cpf",
        contato="joao@exemplo.com",
    )
    texto = repr(dto)
    for pii in ("Joao", "52998224725", "joao@exemplo.com"):
        assert pii not in texto
