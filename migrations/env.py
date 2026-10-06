from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

config = context.config

# ``configure_logger`` (attributes) permite a invocacao programatica dos
# testes desligar o fileConfig: ele reconfigura o logging global do processo
# e contaminaria asserts de log de outros testes na mesma sessao pytest.
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name)

# Carrega o metadata compartilhado e registra os mapeamentos imperativos de
# cada bounded context. Sem isso, ``target_metadata`` fica vazio e o Alembic
# autogenerate nao detecta as tabelas, levando a migrations stub.
from src.compartilhado.infraestrutura.bootstrap import iniciar_todos_mapeamentos
from src.compartilhado.infraestrutura.database import metadata, resolver_database_url

iniciar_todos_mapeamentos()

target_metadata = metadata

# Chave do pg_advisory_lock das migracoes (qualquer bigint fixo do servico).
_TRAVA_DE_MIGRACAO = 4_034_001


def get_url() -> str:
    """Mesma resolucao da API: DATABASE_URL, ou POSTGRES_* so em dev/test."""
    return resolver_database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url())
    with engine.connect() as connection:
        # Replicas com RUN_MIGRATIONS_ON_STARTUP=true serializam aqui: a
        # segunda espera a primeira e encontra o schema em head. A trava e de
        # sessao; o commit fecha so a transacao implicita do SELECT, para o
        # Alembic abrir (e commitar) a dele.
        connection.execute(
            text("SELECT pg_advisory_lock(:chave)"), {"chave": _TRAVA_DE_MIGRACAO}
        )
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:chave)"),
                {"chave": _TRAVA_DE_MIGRACAO},
            )
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
