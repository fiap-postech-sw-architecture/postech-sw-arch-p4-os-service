from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import quote

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import sessionmaker

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

metadata = MetaData()

# Ambientes em que a URL do banco pode ser montada das variaveis POSTGRES_*
# (compose e dev local); qualquer outro exige DATABASE_URL explicita.
AMBIENTES_DEV = frozenset({"development", "test"})


def _url_por_variaveis_postgres(fonte: Mapping[str, str]) -> str:
    obrigatorias = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    ausentes = [nome for nome in obrigatorias if not fonte.get(nome)]
    if ausentes:
        msg = (
            f"Variaveis obrigatorias ausentes: {ausentes!r}. Configure POSTGRES_DB, "
            "POSTGRES_USER e POSTGRES_PASSWORD, ou defina DATABASE_URL."
        )
        raise RuntimeError(msg)
    usuario = quote(fonte["POSTGRES_USER"], safe="")
    senha = quote(fonte["POSTGRES_PASSWORD"], safe="")
    banco = quote(fonte["POSTGRES_DB"], safe="")
    host = fonte.get("POSTGRES_HOST", "localhost")
    porta = fonte.get("POSTGRES_PORT", "5432")
    return f"postgresql://{usuario}:{senha}@{host}:{porta}/{banco}"


def resolver_database_url(env: Mapping[str, str] | None = None) -> str:
    """URL do banco, fonte unica para a API, o Alembic e o seed do admin.

    ``DATABASE_URL`` tem precedencia. Em ``development``/``test`` ela pode ser
    montada de ``POSTGRES_*`` (a senha nunca fica no codigo); fora deles a
    ausencia aborta. ``env`` e injetavel para testes (padrao: ``os.environ``).
    """
    fonte = os.environ if env is None else env
    url = fonte.get("DATABASE_URL")
    if url:
        return url
    if fonte.get("ENVIRONMENT", "development").lower() in AMBIENTES_DEV:
        return _url_por_variaveis_postgres(fonte)
    msg = "DATABASE_URL obrigatoria quando ENVIRONMENT nao for 'development' ou 'test'."
    raise RuntimeError(msg)


# Dimensionamento do pool para escala horizontal. Os defaults sao os do
# SQLAlchemy (5 + 10): com ate 5 replicas no HPA, (pool_size + max_overflow) *
# replicas = 75 conexoes, dentro do max_connections=100 do postgres:16 padrao.
# Sobrescritiveis por env (DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_RECYCLE).
_DB_POOL_SIZE_PADRAO = "5"
_DB_MAX_OVERFLOW_PADRAO = "10"
# Recicla conexoes a cada 30min para evitar conexoes presas/stale em pods
# de vida longa sob o HPA.
_DB_POOL_RECYCLE_PADRAO = "1800"
# Banco fora do ar falha em segundos em vez de prender a thread da request
# (e a readiness) no timeout de TCP do sistema.
_DB_CONNECT_TIMEOUT_PADRAO = "5"


def tempo_de_conexao() -> int:
    """Segundos de ``DB_CONNECT_TIMEOUT`` para abrir uma conexao (padrao 5)."""
    return int(os.environ.get("DB_CONNECT_TIMEOUT", _DB_CONNECT_TIMEOUT_PADRAO))


def criar_engine(url: str) -> Engine:
    # pool_pre_ping valida a conexao no checkout (descarta conexoes mortas
    # apos restart do banco ou ociosidade) — aplicavel a qualquer pool.
    # hide_parameters: erro de statement nao leva os valores (placa, nome,
    # texto livre) para a mensagem da excecao nem para o log.
    if url.startswith("postgresql"):
        # QueuePool (Postgres): dimensiona o pool para N replicas.
        return create_engine(
            url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            hide_parameters=True,
            pool_size=int(os.environ.get("DB_POOL_SIZE", _DB_POOL_SIZE_PADRAO)),
            max_overflow=int(
                os.environ.get("DB_MAX_OVERFLOW", _DB_MAX_OVERFLOW_PADRAO)
            ),
            pool_recycle=int(
                os.environ.get("DB_POOL_RECYCLE", _DB_POOL_RECYCLE_PADRAO)
            ),
            connect_args={"connect_timeout": tempo_de_conexao()},
        )
    # SQLite (testes) usa SingletonThreadPool e rejeita pool_size/max_overflow.
    return create_engine(
        url, echo=False, future=True, pool_pre_ping=True, hide_parameters=True
    )


def criar_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False mantem atributos utilizaveis apos uow.commit().
    # Essencial porque use cases fazem: with uow: repo.salvar(x); uow.commit();
    # return _dto(x). Com expire_on_commit=True (padrao), acessar x.id apos o
    # commit dispara refresh em sessao ja fechada -> DetachedInstanceError.
    # Se reverter este flag, todos os use cases precisam ser refatorados para
    # ler os atributos ANTES do commit.
    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
