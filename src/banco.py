"""Espera do banco migrado: ``python -m src.banco aguardar``.

E o initContainer ``aguarda-migracao`` dos Deployments (API, relay e consumidor):
o pod so sobe depois do Job de migracao da versao dele, que le o ``alembic_version``
com o papel da aplicacao. So espera, nao prepara o banco (a migracao e do Job,
com o dono do schema).

Sai com 0 quando o banco esta na ``head`` desta imagem ou numa revisao que a
imagem nao conhece: depois de um ``rollout undo`` o banco fica adiante da
imagem (a migracao nao desce, ADR-042), e prender o pod ali travaria o rollback.
Espera, e diz no log o motivo, com o banco atras da ``head``, sem nenhuma
revisao ainda ou fora do alcance (conexao recusada, senha errada, DNS).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DBAPIError

from src.compartilhado.infraestrutura.database import (
    criar_engine,
    resolver_database_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Engine

_RAIZ = Path(__file__).resolve().parents[1]
_INTERVALO_S = 2.0


def revisoes_da_imagem() -> tuple[str, frozenset[str]]:
    """A ``head`` desta imagem e todas as revisoes que ela conhece."""
    config = Config(str(_RAIZ / "alembic.ini"))
    config.set_main_option("script_location", str(_RAIZ / "migrations"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        msg = "a imagem nao traz nenhuma migracao em migrations/versions"
        raise RuntimeError(msg)
    return head, frozenset(revisao.revision for revisao in script.walk_revisions())


def _causa(erro: DBAPIError) -> str:
    """Tipo e primeira linha do erro do driver, que nunca leva a senha."""
    linhas = str(erro.orig).strip().splitlines()
    return f"{type(erro).__name__}: {linhas[0] if linhas else ''}"


def _motivo_da_espera(
    engine: Engine, head: str, conhecidas: frozenset[str]
) -> str | None:
    """Por que o pod ainda espera; ``None`` quando pode subir."""
    try:
        with engine.connect() as conexao:
            atual = MigrationContext.configure(conexao).get_current_revision()
    except DBAPIError as erro:
        return _causa(erro)
    if atual is None:
        return "no revision yet"
    if atual == head or atual not in conhecidas:
        return None
    return f"database at revision {atual}, this image needs {head}"


def aguardar(
    engine: Engine,
    *,
    intervalo: float = _INTERVALO_S,
    esperar: Callable[[float], None] = time.sleep,
) -> None:
    """Espera o banco na ``head`` desta imagem (ou adiante dela)."""
    head, conhecidas = revisoes_da_imagem()
    while (motivo := _motivo_da_espera(engine, head, conhecidas)) is not None:
        sys.stderr.write(f"waiting for the migration Job: {motivo}\n")
        sys.stderr.flush()
        esperar(intervalo)


def main(args: Sequence[str] = ()) -> None:
    """``aguardar`` e o unico comando: espera a migracao e sai."""
    if list(args) != ["aguardar"]:
        sys.exit("usage: python -m src.banco aguardar")
    engine = criar_engine(resolver_database_url(), pool_size=1, max_overflow=0)
    try:
        aguardar(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
