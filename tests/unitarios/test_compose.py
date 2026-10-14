"""Stack local (docker-compose.yml): o que os servicos precisam ter em comum."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).parents[2] / "docker-compose.yml"


def _servicos() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_prazo_de_resposta_da_saga_e_o_mesmo_na_api_e_no_consumidor() -> None:
    # A API grava o prazo da primeira compensacao e o consumidor o das seguintes:
    # um ponto so no compose, para nao gravarem prazos diferentes para a saga.
    servicos = _servicos()

    prazos = {
        nome: servicos[nome]["environment"]["SAGA_PRAZO_RESPOSTA_SEGUNDOS"]
        for nome in ("api", "consumidor")
    }

    assert set(prazos.values()) == {"${SAGA_PRAZO_RESPOSTA_SEGUNDOS:-120}"}
    definicoes = re.findall(
        r"^\s+SAGA_PRAZO_RESPOSTA_SEGUNDOS:", COMPOSE.read_text(), re.MULTILINE
    )
    assert len(definicoes) == 1
