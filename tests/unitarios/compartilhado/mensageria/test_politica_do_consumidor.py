"""Regras do consumidor que nao dependem de broker nem de banco."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.compartilhado.infraestrutura.mensageria.consumidor import (
    _MensagemRejeitadaError,
    _uuid_ou_nada,
    conferir_origem,
    fila_de_retry_da_copia,
    ler_tentativa,
)


def test_cada_tentativa_tem_a_sua_fila_de_retry_e_depois_da_quinta_e_dlq() -> None:
    assert [fila_de_retry_da_copia(t) for t in range(7)] == [
        "os.eventos.retry.1s",
        "os.eventos.retry.5s",
        "os.eventos.retry.15s",
        "os.eventos.retry.60s",
        "os.eventos.retry.300s",
        None,
        None,
    ]


@pytest.mark.parametrize(
    ("cabecalhos", "tentativa"),
    [
        pytest.param({}, 0, id="sem-cabecalho-e-a-primeira-entrega"),
        pytest.param({"x-tentativa": 0}, 0, id="zero"),
        pytest.param({"x-tentativa": 5}, 5, id="quinta-copia"),
    ],
)
def test_tentativa_lida_do_cabecalho(
    cabecalhos: dict[str, object], tentativa: int
) -> None:
    assert ler_tentativa(cabecalhos) == tentativa


@pytest.mark.parametrize(
    "valor",
    [
        pytest.param(-1, id="negativa"),
        pytest.param(6, id="acima-de-cinco"),
        pytest.param(True, id="booleano"),
        pytest.param("1", id="texto"),
        pytest.param(Decimal(1), id="decimal"),
        pytest.param(None, id="nulo"),
    ],
)
def test_tentativa_fora_de_zero_a_cinco_ou_nao_inteira_e_recusada(
    valor: object,
) -> None:
    with pytest.raises(_MensagemRejeitadaError, match=r"^tentativa_invalida$"):
        ler_tentativa({"x-tentativa": valor})


@pytest.mark.parametrize(
    ("usuario", "tentativa"),
    [
        pytest.param("billing", 0, id="produtor-do-tipo"),
        pytest.param("billing", 2, id="produtor-do-tipo-com-x-tentativa"),
        pytest.param("os", 1, id="copia-de-retry-do-proprio-consumidor"),
        pytest.param("os", 5, id="ultima-copia-de-retry"),
    ],
)
def test_origem_aceita(usuario: str, tentativa: int) -> None:
    conferir_origem(
        usuario=usuario, produtor="billing", consumidor="os", tentativa=tentativa
    )


@pytest.mark.parametrize(
    ("usuario", "tentativa"),
    [
        pytest.param("execucao", 0, id="outro-servico"),
        pytest.param("execucao", 3, id="outro-servico-com-x-tentativa"),
        pytest.param("os", 0, id="proprio-consumidor-sem-x-tentativa"),
        pytest.param(None, 0, id="sem-user-id"),
        pytest.param(None, 2, id="sem-user-id-com-x-tentativa"),
    ],
)
def test_origem_recusada(usuario: str | None, tentativa: int) -> None:
    with pytest.raises(_MensagemRejeitadaError, match=r"^produtor_divergente$"):
        conferir_origem(
            usuario=usuario, produtor="billing", consumidor="os", tentativa=tentativa
        )


@pytest.mark.parametrize(
    ("valor", "no_log"),
    [
        pytest.param(
            "6F9619FF-8B86-D011-B42D-00C04FC964FF",
            "6f9619ff-8b86-d011-b42d-00c04fc964ff",
            id="uuid-canonico",
        ),
        pytest.param("placa QZX7W42", None, id="texto-livre"),
        pytest.param(None, None, id="ausente"),
        pytest.param(b"6f9619ff-8b86-d011-b42d-00c04fc964ff", None, id="bytes"),
    ],
)
def test_so_o_id_convertido_em_uuid_vai_para_o_log(
    valor: object, no_log: str | None
) -> None:
    assert _uuid_ou_nada(valor) == no_log
