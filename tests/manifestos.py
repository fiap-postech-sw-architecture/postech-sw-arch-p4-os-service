"""Leitura dos manifestos de ``k8s/base`` para os testes que os conferem."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[1]
BASE = RAIZ / "k8s/base"


def objeto(tipo: str, nome: str) -> dict[str, Any]:
    """O objeto ``tipo``/``nome`` dos YAML de ``k8s/base``."""
    # Todo YAML de k8s/base, menos o kustomization.yaml (sem metadata).
    for arquivo in sorted(set(BASE.glob("*.yaml")) - {BASE / "kustomization.yaml"}):
        for documento in yaml.safe_load_all(arquivo.read_text()):
            if (documento["kind"], documento["metadata"]["name"]) == (tipo, nome):
                return dict(documento)
    msg = f"{tipo}/{nome} nao esta em k8s/base"
    raise AssertionError(msg)


def container(manifesto: dict[str, Any], nome: str) -> dict[str, Any]:
    """O container (ou initContainer) ``nome`` do template de pods."""
    especificacao = manifesto["spec"]["template"]["spec"]
    containers = especificacao["containers"] + especificacao.get("initContainers", [])
    return dict(next(c for c in containers if c["name"] == nome))


def comando(manifesto: dict[str, Any], nome: str) -> str:
    """O script do ``sh -c`` de um container."""
    sh, opcao, script = container(manifesto, nome)["command"]
    assert (sh, opcao) == ("sh", "-c")
    return str(script)
