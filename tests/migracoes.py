"""As revisoes do Alembic desta imagem, lidas dos scripts de ``migrations/``."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_RAIZ = Path(__file__).resolve().parents[1]


def _scripts() -> ScriptDirectory:
    config = Config(str(_RAIZ / "alembic.ini"))
    config.set_main_option("script_location", str(_RAIZ / "migrations"))
    return ScriptDirectory.from_config(config)


def revisao_head() -> str:
    """A ultima revisao da imagem (a que o Job de migracao deixa no banco)."""
    head = _scripts().get_current_head()
    assert head is not None
    return head


def revisao_anterior() -> str:
    """A revisao logo antes da ``head``: a do banco que o Job ainda nao migrou."""
    anterior = _scripts().get_revision(revisao_head()).down_revision
    assert isinstance(anterior, str)
    return anterior
