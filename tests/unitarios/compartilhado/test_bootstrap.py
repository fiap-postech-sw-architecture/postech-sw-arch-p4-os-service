from __future__ import annotations

import importlib
from unittest.mock import patch

from src.compartilhado.infraestrutura import bootstrap


class TestBootstrap:
    def test_iniciar_todos_mapeamentos_chamado_apenas_uma_vez(self) -> None:
        """Garante que a flag idempotente funciona."""
        importlib.reload(bootstrap)  # Reseta o estado global

        with (
            patch(
                "src.autenticacao.infraestrutura.mapping.iniciar_mapeamentos"
            ) as mock_auth,
            patch(
                "src.cliente_veiculo.infraestrutura.mapping.iniciar_mapeamentos"
            ) as mock_clie,
            patch(
                "src.ordem_servico.infraestrutura.mapping.iniciar_mapeamentos"
            ) as mock_os,
        ):
            bootstrap.iniciar_todos_mapeamentos()
            mock_auth.assert_called_once_with()
            mock_clie.assert_called_once_with()
            mock_os.assert_called_once_with()

            # Segunda chamada: no-op em todos
            bootstrap.iniciar_todos_mapeamentos()
            assert mock_auth.call_count == 1
            assert mock_clie.call_count == 1
            assert mock_os.call_count == 1


def test_mapeamento_de_cada_contexto_e_idempotente() -> None:
    # Segunda chamada de cada contexto e no-op (o bootstrap e o lifespan e os
    # testes chamam mais de uma vez no mesmo processo).
    from src.autenticacao.infraestrutura import mapping as auth
    from src.cliente_veiculo.infraestrutura import mapping as clientes
    from src.ordem_servico.infraestrutura import mapping as ordens

    for modulo in (auth, clientes, ordens):
        modulo.iniciar_mapeamentos()
        modulo.iniciar_mapeamentos()
        assert modulo._mapeamento_iniciado is True
