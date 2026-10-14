"""``scripts/reenviar_anonimizacao.py`` com o PostgreSQL real.

O comando volta para a outbox, com o contrato do erasure, so para o veiculo que
ja foi anonimizado (RFC-004 secao 5.3).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from scripts.reenviar_anonimizacao import reenviar
from src.cliente_veiculo.infraestrutura.repository import ClienteSQLAlchemyRepository
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integracao

_PLACA = "QZX7W42"
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "reenviar_anonimizacao.py"


def _comandos(engine: Engine) -> list[Any]:
    with engine.connect() as conexao:
        return list(
            conexao.execute(
                text(
                    "SELECT exchange, routing_key, correlation_id, envelope "
                    "FROM outbox WHERE envelope ->> 'tipo' = 'AnonimizarVeiculo'"
                )
            ).all()
        )


def _veiculo(session_factory: sessionmaker[Session], *, anonimizar: bool) -> UUID:
    with session_factory() as sessao:
        cliente = criar_cliente_com_veiculo(sessao, placa=_PLACA)
        sessao.commit()
        if anonimizar:
            ClienteSQLAlchemyRepository(sessao).anonimizar_dados(cliente.id)
            sessao.commit()
        return cliente.veiculos[0].id


def test_veiculo_anonimizado_volta_para_a_outbox_so_com_o_veiculo_id(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    veiculo_id = _veiculo(session_factory, anonimizar=True)

    assert reenviar(session_factory, veiculo_id) is True

    (comando,) = _comandos(engine)
    assert (comando.exchange, comando.routing_key, comando.correlation_id) == (
        "pytstop.comandos",
        "comando.execucao.anonimizar_veiculo",
        veiculo_id,
    )
    catalogo().validar(comando.envelope)
    assert comando.envelope["dados"] == {"veiculo_id": str(veiculo_id)}
    assert comando.envelope["causation_id"] is None
    assert _PLACA not in str(comando.envelope)


def test_veiculo_vivo_nao_gera_comando(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    veiculo_id = _veiculo(session_factory, anonimizar=False)

    assert reenviar(session_factory, veiculo_id) is False

    assert _comandos(engine) == []


def test_veiculo_inexistente_nao_gera_comando(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    assert reenviar(session_factory, uuid4()) is False

    assert _comandos(engine) == []


def test_script_pela_linha_de_comando_como_o_readme_manda(
    engine: Engine, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    # Fora da raiz do projeto, so com o DATABASE_URL do ambiente (o do pod).
    anonimizado = _veiculo(session_factory, anonimizar=True)
    vivo = _veiculo(session_factory, anonimizar=False)
    ambiente = {
        **os.environ,
        "ENVIRONMENT": "test",
        "DATABASE_URL": engine.url.render_as_string(hide_password=False),
    }

    def rodar(veiculo_id: UUID) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603  # o proprio interpretador e o script do repo
            [sys.executable, str(_SCRIPT), str(veiculo_id)],
            cwd=tmp_path,
            env=ambiente,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    enviado = rodar(anonimizado)
    recusado = rodar(vivo)

    assert (enviado.returncode, recusado.returncode) == (0, 1)
    assert "gravado na outbox" in enviado.stdout
    assert "nao anonimizado" in recusado.stderr
    (comando,) = _comandos(engine)
    assert comando.correlation_id == anonimizado
