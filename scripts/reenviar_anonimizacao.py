"""Reenvia o ``AnonimizarVeiculo`` de um veiculo ja anonimizado (LGPD).

Uso, no pod do consumidor ou do relay (que ja tem o ``DATABASE_URL``)::

    python scripts/reenviar_anonimizacao.py <veiculo_id>

O erasure repetido nao reemite o comando. Se o ``AnonimizarVeiculo`` de um veiculo
caiu na ``execucao.comandos.dlq`` (a Execucao nao anonimizou a placa), este script
o grava de novo na outbox, na transacao e com o contrato do proprio erasure; o
relay o publica com o usuario do OS Service, o unico produtor que a Execucao
aceita (nao ha como publica-lo a mao: o broker so aceita ``user_id`` igual ao
usuario da conexao, e o usuario ``os`` nao entra na API de gerenciamento).

So reenvia veiculo ja anonimizado: o comando de um veiculo vivo apagaria a placa
dele na Execucao. O comando leva so o ``veiculo_id``.

Saida: 0 enviado; 1 veiculo inexistente ou ainda nao anonimizado; 2 uso errado.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

# Garante que ``src.*`` seja importavel ao rodar ``python scripts/...`` sem
# ``pip install -e .``. Idempotente: no-op se o projeto ja estiver no path.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import text  # noqa: E402

from src.cliente_veiculo.dominio.placa_anonimizada import (  # noqa: E402
    PREFIXO_DO_TOMBSTONE,
)
from src.compartilhado.aplicacao.mensageria import Comando  # noqa: E402
from src.compartilhado.infraestrutura.database import (  # noqa: E402
    criar_engine,
    criar_session_factory,
    resolver_database_url,
)
from src.compartilhado.infraestrutura.unit_of_work import (  # noqa: E402
    SQLAlchemyUnitOfWork,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.orm import Session


def reenviar(session_factory: Callable[[], Session], veiculo_id: UUID) -> bool:
    """Grava o ``AnonimizarVeiculo`` na outbox; False se o veiculo nao e anonimizado."""
    with SQLAlchemyUnitOfWork(session_factory) as uow:
        placa = uow.session.execute(
            text("SELECT placa FROM veiculos WHERE id = :id"), {"id": veiculo_id}
        ).scalar_one_or_none()
        if placa is None or not placa.startswith(PREFIXO_DO_TOMBSTONE):
            return False
        uow.publicar_comando(
            Comando.ANONIMIZAR_VEICULO,
            {"veiculo_id": veiculo_id},
            correlation_id=veiculo_id,
        )
        uow.commit()
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Le o ``veiculo_id`` da linha de comando e o reenvia."""
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        (bruto,) = args
        veiculo_id = UUID(bruto)
    except ValueError:
        print(
            "uso: python scripts/reenviar_anonimizacao.py <veiculo_id>", file=sys.stderr
        )
        return 2
    engine = criar_engine(resolver_database_url())
    try:
        enviado = reenviar(criar_session_factory(engine), veiculo_id)
    finally:
        engine.dispose()
    if not enviado:
        print(
            "veiculo inexistente ou ainda nao anonimizado: nada foi enviado",
            file=sys.stderr,
        )
        return 1
    print(f"AnonimizarVeiculo do veiculo {veiculo_id} gravado na outbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
