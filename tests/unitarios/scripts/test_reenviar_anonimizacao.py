"""``scripts/reenviar_anonimizacao.py``: a linha de comando e as guardas."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from scripts import reenviar_anonimizacao as script


def _sessao_com_placa(placa: str | None) -> MagicMock:
    sessao = MagicMock()
    sessao.execute.return_value.scalar_one_or_none.return_value = placa
    return sessao


@pytest.mark.parametrize(
    "placa",
    [
        pytest.param(None, id="veiculo-inexistente"),
        pytest.param("ABC1234", id="veiculo-vivo"),
    ],
)
def test_veiculo_que_nao_foi_anonimizado_nada_envia(placa: str | None) -> None:
    # O comando de um veiculo vivo apagaria a placa dele na Execucao.
    sessao = _sessao_com_placa(placa)

    enviado = script.reenviar(lambda: sessao, uuid4())

    assert enviado is False
    sessao.commit.assert_not_called()
    sessao.execute.assert_called_once()


@pytest.mark.parametrize(
    "args",
    [[], ["a", "b"], ["nao-e-uuid"], [""]],
    ids=["sem-argumento", "dois-argumentos", "nao-uuid", "vazio"],
)
def test_uso_errado_sai_com_2_sem_tocar_o_banco(
    args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def nao_deve_abrir_o_banco() -> str:
        raise AssertionError

    monkeypatch.setattr(script, "resolver_database_url", nao_deve_abrir_o_banco)

    assert script.main(args) == 2
    assert "uso:" in capsys.readouterr().err


@pytest.mark.parametrize(("enviado", "saida"), [(True, 0), (False, 1)])
def test_main_devolve_0_se_enviou_e_1_se_o_veiculo_nao_foi_anonimizado(
    enviado: bool, saida: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = MagicMock()
    monkeypatch.setattr(script, "resolver_database_url", lambda: "postgresql://x")
    monkeypatch.setattr(script, "criar_engine", lambda _url: engine)
    monkeypatch.setattr(script, "criar_session_factory", lambda _engine: MagicMock())
    monkeypatch.setattr(script, "reenviar", lambda _fabrica, _id: enviado)

    assert script.main([str(uuid4())]) == saida
    engine.dispose.assert_called_once()
