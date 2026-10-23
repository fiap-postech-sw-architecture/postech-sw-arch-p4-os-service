"""Contrato local do caller CD; os gates de imagem/deploy sao testados no Platform."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[2]
WORKFLOWS = RAIZ / ".github" / "workflows"
CD = WORKFLOWS / "cd.yml"
SHA_PADRAO = re.compile(r"^[0-9a-f]{40}$")
DIGEST = r"sha256:[0-9a-f]{64}"
CD_REUTILIZAVEL = (
    "fiap-postech-sw-architecture/postech-sw-arch-p4-platform/"
    ".github/workflows/cd-servico.yml"
)


def _carrega(caminho: Path = CD) -> dict[Any, Any]:
    carregado: dict[Any, Any] = yaml.safe_load(caminho.read_text())
    return carregado


def _gatilhos(workflow: dict[Any, Any]) -> dict[str, Any]:
    # O YAML 1.1 do PyYAML le a chave `on` como o booleano True.
    gatilhos: dict[str, Any] = workflow[True]
    return gatilhos


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _carrega()["jobs"]
    return jobs


def test_caller_preserva_gates_locais_antes_do_cd_reutilizavel() -> None:
    jobs = _jobs()

    assert list(jobs) == ["ci", "security-scan", "cd"]
    assert jobs["ci"] == {
        "uses": "./.github/workflows/ci.yml",
        "permissions": {"contents": "read"},
    }
    assert jobs["security-scan"] == {
        "uses": "./.github/workflows/security.yml",
        "permissions": {"contents": "read"},
    }
    assert set(jobs["cd"]) == {"needs", "uses", "permissions", "with"}
    assert jobs["cd"]["needs"] == ["ci", "security-scan"]
    assert re.fullmatch(
        re.escape(CD_REUTILIZAVEL) + r"@[0-9a-f]{40}", jobs["cd"]["uses"]
    )


def test_caller_encaminha_servico_e_os_quatro_inputs_sem_transformacao() -> None:
    entradas = _gatilhos(_carrega())["workflow_dispatch"]["inputs"]

    assert _jobs()["cd"]["with"] == {
        "servico": "os-service",
        **{nome: "${{ inputs." + nome + " }}" for nome in entradas},
    }
    assert "env" not in _carrega()


def test_caller_concede_so_a_uniao_das_permissoes_do_cd() -> None:
    assert _carrega()["permissions"] == {"contents": "read"}
    assert _jobs()["cd"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "packages": "write",
    }
    # Sem secrets: inherit, environment ou passos locais com acesso a segredos.
    assert set(_carrega()) == {"name", True, "permissions", "concurrency", "jobs"}


def test_ci_e_seguranca_conservam_os_nove_checks_e_workflow_call() -> None:
    for arquivo, checks in [
        ("ci.yml", {"lint", "type-check", "security", "test", "sonarqube", "build"}),
        ("security.yml", {"pip-audit", "gitleaks", "trivy"}),
    ]:
        workflow = _carrega(WORKFLOWS / arquivo)
        assert set(workflow["jobs"]) == checks
        assert "workflow_call" in _gatilhos(workflow)


def test_gatilhos_sao_o_push_na_main_e_o_dispatch() -> None:
    gatilhos = _gatilhos(_carrega())

    assert set(gatilhos) == {"push", "workflow_dispatch"}
    assert gatilhos["push"] == {"branches": ["main"]}
    entradas = gatilhos["workflow_dispatch"]["inputs"]
    assert list(entradas) == [
        "sha_platform",
        "sha_os_service",
        "sha_billing_service",
        "sha_execution_service",
    ]
    assert all(
        entrada["required"] is False and entrada["type"] == "string"
        for entrada in entradas.values()
    )


def test_concurrency_por_repositorio_sem_cancelar_o_run_em_curso() -> None:
    assert _carrega()["concurrency"] == {
        "group": "cd-${{ github.repository }}",
        "cancel-in-progress": False,
    }


def test_imagem_do_actionlint_do_makefile_por_digest() -> None:
    makefile = (RAIZ / "Makefile").read_text()

    assert re.search(
        rf"^ACTIONLINT_IMAGE := rhysd/actionlint:\d+\.\d+\.\d+@{DIGEST}$",
        makefile,
        re.MULTILINE,
    )


def test_nenhum_run_interpola_expressao() -> None:
    # Nada de ${{ }} em script, nos tres workflows: input, saida de passo e
    # contexto do github entram por env, onde o shell os trata como dado. O
    # ci.yml e o security.yml rodam em PR de fork, onde github.head_ref e o
    # titulo do PR sao do autor.
    arquivos = sorted(WORKFLOWS.glob("*.yml"))
    com_expressao = [
        (arquivo.name, nome, passo.get("name") or passo.get("id") or passo["run"][:40])
        for arquivo in arquivos
        for nome, job in _carrega(arquivo)["jobs"].items()
        for passo in job.get("steps", [])
        if "run" in passo and "${{" in passo["run"]
    ]

    assert [arquivo.name for arquivo in arquivos] == [
        "cd.yml",
        "ci.yml",
        "security.yml",
    ]
    assert com_expressao == []


_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?:\s+#\s*(?P<tag>\S.*))?$")


@pytest.mark.parametrize(
    "arquivo", sorted(WORKFLOWS.glob("*.yml")), ids=lambda a: a.name
)
def test_action_de_terceiro_por_sha_de_commit_com_a_tag_em_comentario(
    arquivo: Path,
) -> None:
    usos = [
        resultado
        for linha in arquivo.read_text().splitlines()
        if (resultado := _USES.match(linha))
    ]

    assert usos, arquivo.name
    for uso in usos:
        ref = uso["ref"]
        if ref.startswith("./") or ref.partition("@")[0] == CD_REUTILIZAVEL:
            continue
        dono, _, versao = ref.partition("@")
        if SHA_PADRAO.match(versao):
            assert uso["tag"], f"{ref}: sem a tag em comentario"
            assert re.match(r"v\d+(\.\d+)*", uso["tag"]), ref
        else:
            # Sem SHA, so as oficiais, pela tag de versao.
            assert dono.startswith("actions/"), f"{ref}: action de terceiro sem SHA"
            assert re.fullmatch(r"v\d+(\.\d+)*", versao), ref


def test_dependabot_sobe_actions_python_e_imagens_toda_semana_agrupados() -> None:
    # As actions entram por SHA: sem isto nada as atualiza, e o SHA e a tag do
    # comentario sobem juntos no PR dele (o teste `rede` confere o par).
    config = yaml.safe_load((RAIZ / ".github" / "dependabot.yml").read_text())
    agenda = {
        "interval": "weekly",
        "day": "monday",
        "time": "06:00",
        "timezone": "America/Sao_Paulo",
    }
    patch_e_minor = {"update-types": ["minor", "patch"]}
    # O Python do Dockerfile sobe a mao, junto do .python-version e dos workflows.
    so_patch_do_python = [
        {
            "dependency-name": "python",
            "update-types": [
                "version-update:semver-major",
                "version-update:semver-minor",
            ],
        }
    ]
    # O teto do pyproject.toml nao segura o Dependabot (ele o alarga): a minor do
    # sqlalchemy (troca o driver do PostgreSQL) e a dos opentelemetry-* (o
    # observability.py mexe em atributos privados do SDK) so entram a mao. A do
    # fastapi (0.x: a minor traz mudanca incompativel) fica segurada ate a
    # entrega da fase 4, em 17/11/2026: o patch sobe pelo grupo, e o pydantic
    # nao tem ignore.
    sem_minor = ["version-update:semver-major", "version-update:semver-minor"]
    ignore_do_uv = [
        {"dependency-name": "sqlalchemy", "update-types": sem_minor},
        {"dependency-name": "opentelemetry-*", "update-types": sem_minor},
        {"dependency-name": "fastapi", "update-types": sem_minor},
    ]
    # Os opentelemetry-* sobem juntos no generico do uv: um grupo so deles
    # nunca recebe pacote, porque o Dependabot o trata como menos especifico.
    esperado = {
        "uv": (
            "chore(deps)",
            {"python-patch-e-minor": patch_e_minor},
            ignore_do_uv,
        ),
        "github-actions": ("ci", {"actions-patch-e-minor": patch_e_minor}, []),
        "docker": (
            "chore(deps)",
            {"docker-patch-e-minor": patch_e_minor},
            so_patch_do_python,
        ),
    }
    blocos = {b["package-ecosystem"]: b for b in config["updates"]}

    assert config["version"] == 2
    assert set(config) == {"version", "updates"}
    assert set(blocos) == set(esperado)
    for nome, (prefixo, grupos, ignore) in esperado.items():
        # O bloco inteiro: chave a mais (allow, cooldown, target-branch...) reprova.
        # Sem `labels`: rotulo declarado e inexistente no repositorio vira
        # comentario em cada PR do Dependabot, que cria os padrao se faltarem.
        assert blocos[nome] == {
            "package-ecosystem": nome,
            "directory": "/",
            "schedule": agenda,
            "open-pull-requests-limit": 3,
            "commit-message": {"prefix": prefixo},
            "groups": grupos,
            **({"ignore": ignore} if ignore else {}),
        }, nome


def _commit_da_tag(repositorio: str, tag: str) -> str | None:
    git = shutil.which("git") or "git"
    saida = subprocess.run(  # noqa: S603 - git, com argumentos fixos
        [
            git,
            "ls-remote",
            f"https://github.com/{repositorio}.git",
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ],
        env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    refs = {
        referencia: sha
        for sha, referencia in (
            linha.split("\t") for linha in saida.stdout.splitlines()
        )
    }
    # A tag anotada aponta para o objeto da tag; o commit esta no `^{}`.
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


@pytest.mark.rede
def test_sha_de_cada_action_e_o_commit_da_tag_do_comentario() -> None:
    fixadas = {
        (acao, versao, uso["tag"].split()[0])
        for arquivo in sorted(WORKFLOWS.glob("*.yml"))
        for linha in arquivo.read_text().splitlines()
        if (uso := _USES.match(linha))
        for acao, _, versao in [uso["ref"].partition("@")]
        if SHA_PADRAO.match(versao) and "/.github/workflows/" not in acao
    }

    assert fixadas
    obtidos = {
        (acao, tag): (versao, _commit_da_tag("/".join(acao.split("/")[:2]), tag))
        for acao, versao, tag in sorted(fixadas)
    }
    divergentes = {chave: par for chave, par in obtidos.items() if par[0] != par[1]}
    assert divergentes == {}
