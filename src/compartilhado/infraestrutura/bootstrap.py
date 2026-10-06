"""Bootstrap compartilhado dos imperative mappings de todos os bounded contexts.

Ponto de verdade unico para a ordem de registro das tabelas no metadata
SQLAlchemy. Chamado pelo lifespan do FastAPI, pelo env.py do Alembic, pelo
conftest de integracao e pelo seed de admin.

Ordem: ``cliente_veiculo`` antes de ``ordem_servico`` porque
``ordens_de_servico`` referencia ``clientes``/``veiculos`` via FK.
``autenticacao`` nao tem relacionamento cross-context.
"""

from __future__ import annotations

_mapeamentos_registrados = False


def iniciar_todos_mapeamentos() -> None:
    """Registra todos os imperative mappings dos bounded contexts.

    Idempotente: chamadas subsequentes sao no-op (warm restart do uvicorn,
    re-entrada em testes, invocacao multipla em scripts).
    """
    global _mapeamentos_registrados  # noqa: PLW0603  # init-once flag
    if _mapeamentos_registrados:
        return

    from src.autenticacao.infraestrutura.mapping import (
        iniciar_mapeamentos as iniciar_auth,
    )
    from src.cliente_veiculo.infraestrutura.mapping import (
        iniciar_mapeamentos as iniciar_cliente,
    )
    from src.ordem_servico.infraestrutura.mapping import (
        iniciar_mapeamentos as iniciar_os,
    )

    iniciar_cliente()
    iniciar_os()
    iniciar_auth()

    # Tabelas Core da outbox: sem agregado mapeado, o import as registra.
    import src.compartilhado.infraestrutura.outbox_mapping  # noqa: F401

    _mapeamentos_registrados = True
