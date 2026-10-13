from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from src.compartilhado.infraestrutura.database import criar_engine_de_metricas
from src.compartilhado.interfaces import dependencies
from src.compartilhado.interfaces.dependencies import (
    abrir_conexao_de_metricas,
    configurar_engine_de_metricas,
    configurar_session_factory,
    obter_session,
)


class TestDependencies:
    def test_obter_session_sem_factory_levanta_erro(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dependencies, "_session_factory", None)
        with pytest.raises(RuntimeError, match="Session factory"):
            next(obter_session())

    def test_obter_session_com_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Garante o estado limpo ANTES e a restauracao DEPOIS (teardown do
        # monkeypatch), sem resetar via API publica com None.
        monkeypatch.setattr(dependencies, "_session_factory", None)
        mock_session = MagicMock()
        mock_factory = MagicMock(return_value=mock_session)
        configurar_session_factory(mock_factory)

        gen = obter_session()
        session = next(gen)
        assert session is mock_session

        try:
            next(gen)
        except StopIteration:
            pass

        mock_factory.assert_called_once_with()
        mock_session.close.assert_called_once_with()


class TestEngineDeMetricas:
    def test_conexao_antes_do_boot_levanta_erro(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dependencies, "_engine_de_metricas", None)

        with pytest.raises(RuntimeError, match="Engine de metricas"):
            abrir_conexao_de_metricas()

    def test_conexao_vem_da_engine_configurada(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dependencies, "_engine_de_metricas", None)
        engine = create_engine("sqlite://")
        configurar_engine_de_metricas(engine)

        with abrir_conexao_de_metricas() as conexao:
            assert conexao.engine is engine
        engine.dispose()

    def test_engine_de_metricas_tem_uma_conexao_e_prazos_curtos(self) -> None:
        engine = criar_engine_de_metricas("postgresql://u:p@127.0.0.1:1/db")

        assert (engine.pool.size(), engine.pool.timeout()) == (1, 1)
        assert engine.pool._max_overflow == 0
        engine.dispose()
