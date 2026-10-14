"""Leitura dos manifestos de ``k8s/`` para os testes que os conferem."""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

RAIZ = Path(__file__).resolve().parents[1]
BASE = RAIZ / "k8s/base"
OVERLAYS = ("kind", "kind-ci", "k3s")

# Os objetos com template de pods.
TIPOS_COM_PODS = ("Deployment", "StatefulSet", "Job")


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


def literais_do_servico(
    kustomization: Path = BASE / "kustomization.yaml",
) -> dict[str, str]:
    """Os literais do ConfigMap ``os-service`` de uma kustomization."""
    documento = yaml.safe_load(kustomization.read_text())
    gerador: dict[str, Any] = next(
        (
            g
            for g in documento.get("configMapGenerator", [])
            if g["name"] == "os-service"
        ),
        {"literals": []},
    )
    return dict(item.split("=", 1) for item in gerador["literals"])


def exigir_kubectl() -> None:
    """Encerra a sessao de testes, uma vez e com o motivo, sem ``kubectl`` no PATH.

    Sem ele cada teste do render falharia com um ``FileNotFoundError`` cru. Nao e
    ``skip``: o gate dos manifestos nao pode passar sem rodar.
    """
    if shutil.which("kubectl") is None:
        pytest.exit(
            "kubectl nao esta no PATH: os testes dos manifestos leem o render do "
            "`kubectl kustomize` (o make test exige o kubectl, ver o README)",
            returncode=1,
        )


@cache
def renderizar(overlay: str) -> tuple[dict[str, Any], ...]:
    """Os objetos que o ``kubectl kustomize`` de ``k8s/overlays/<overlay>`` gera."""
    # Argumentos fixos: o kubectl do PATH e um overlay deste repositorio.
    saida = subprocess.run(  # noqa: S603
        ["kubectl", "kustomize", str(RAIZ / "k8s/overlays" / overlay)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return tuple(documento for documento in yaml.safe_load_all(saida) if documento)


def do_render(overlay: str, tipo: str, nome: str | None = None) -> dict[str, Any]:
    """O objeto do render do ``overlay``; sem ``nome``, o unico daquele tipo."""
    encontrados = [
        objeto_renderizado
        for objeto_renderizado in renderizar(overlay)
        if objeto_renderizado["kind"] == tipo
        and nome in (None, objeto_renderizado["metadata"]["name"])
    ]
    assert len(encontrados) == 1, f"{tipo}/{nome} no overlay {overlay}: {encontrados}"
    return dict(encontrados[0])


def do_render_por_tipo(overlay: str, tipo: str) -> list[dict[str, Any]]:
    """Todos os objetos de um ``tipo`` do render do ``overlay``."""
    return [o for o in renderizar(overlay) if o["kind"] == tipo]


def pods(overlay: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(tipo, nome, pod) de cada Deployment, StatefulSet e Job do render."""
    for tipo in TIPOS_COM_PODS:
        for objeto_renderizado in do_render_por_tipo(overlay, tipo):
            yield (
                tipo,
                objeto_renderizado["metadata"]["name"],
                objeto_renderizado["spec"]["template"]["spec"],
            )


def containers(
    overlay: str,
) -> Iterator[tuple[tuple[str, str, str], dict[str, Any], dict[str, Any]]]:
    """((tipo, objeto, container), container, pod) de cada container e init."""
    for tipo, nome, pod in pods(overlay):
        for secao in ("initContainers", "containers"):
            for especificacao in pod.get(secao, []):
                yield (tipo, nome, especificacao["name"]), especificacao, pod


_VARIAVEL = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


def expandir_ambiente(
    especificacao: dict[str, Any], segredo: Callable[[str, str], str]
) -> dict[str, str]:
    """O ``env`` de um container como o kubelet o monta.

    ``segredo(nome, chave)`` devolve o valor de um ``secretKeyRef``. Como no
    kubelet, ``$(VAR)`` so expande uma variavel definida antes na lista: a que
    vem depois fica escrita como esta.
    """
    ambiente: dict[str, str] = {}
    for variavel in especificacao.get("env", []):
        if "value" in variavel:
            valor = _VARIAVEL.sub(
                lambda achado: ambiente.get(achado.group(1), achado.group(0)),
                str(variavel["value"]),
            )
        else:
            referencia = variavel["valueFrom"]["secretKeyRef"]
            valor = segredo(referencia["name"], referencia["key"])
        ambiente[variavel["name"]] = valor
    return ambiente
