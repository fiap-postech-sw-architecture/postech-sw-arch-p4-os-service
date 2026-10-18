"""O ``cd.yml`` (ADR-042): a estrutura do workflow lida do YAML (jobs,
permissoes, gates, actions por SHA, imagens por digest, artefatos) e os passos
que valem executar de verdade, com ``gh``, ``docker`` e script falsos: a guarda
de ref, a escolha dos commits do platform e dos vizinhos (a regra de seguranca
dos inputs), o SBOM, a comparacao do platform com a ``main`` dele e a chamada
do deploy remoto. O arquivo vale, sem mudar, na copia de qualquer um dos tres
servicos: o ``SERVICO`` vem do YAML."""

from __future__ import annotations

import json
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
DONO = "fiap-postech-sw-architecture"
BASH = shutil.which("bash") or "/bin/bash"


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


def _passos(job: str) -> list[dict[str, Any]]:
    passos: list[dict[str, Any]] = _jobs()[job]["steps"]
    return passos


def _todos_os_passos() -> list[tuple[str, dict[str, Any]]]:
    return [
        (nome, passo) for nome, job in _jobs().items() for passo in job.get("steps", [])
    ]


def _resolve(valor: str) -> str:
    # O unico contexto dos nomes de artefato que o teste resolve: o servico.
    return valor.replace("${{ env.SERVICO }}", _carrega()["env"]["SERVICO"])


def _passo_que_usa(job: str, acao: str, nome: str | None = None) -> dict[str, Any]:
    achados = [
        passo
        for passo in _passos(job)
        if passo.get("uses", "").startswith(acao)
        and (nome is None or _resolve(passo["with"].get("name", "")) == nome)
    ]
    assert len(achados) == 1, (job, acao, nome)
    return achados[0]


def _passo_com_id(job: str, id_: str) -> dict[str, Any]:
    return next(passo for passo in _passos(job) if passo.get("id") == id_)


SERVICO: str = _carrega()["env"]["SERVICO"]
SERVICOS = ("os-service", "billing-service", "execution-service")
# Os outros dois servicos, na ordem de SERVICOS: o arquivo vale, sem mudar, na
# copia que o Billing e a Execucao fizerem (so o env SERVICO do workflow muda).
VIZINHOS = tuple(servico for servico in SERVICOS if servico != SERVICO)
PRIMEIRO_VIZINHO, SEGUNDO_VIZINHO = VIZINHOS


def _entrada(servico: str) -> str:
    """O nome do input do dispatch (SHA_...) como o passo o recebe por env."""
    return "SHA_" + servico.upper().replace("-", "_")


def _saida(servico: str) -> str:
    """O nome da saida do passo (sha_...) e do input do dispatch."""
    return "sha_" + servico.replace("-", "_")


def _executa(
    passo: dict[str, Any],
    pasta: Path,
    env: dict[str, str],
    falsos: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Roda o ``run`` de um passo como o runner o roda (``bash -e``).

    Em ``pasta``, so com o ``env`` dado e com os comandos falsos de ``falsos``
    na frente do PATH.
    """
    script = pasta / "passo.sh"
    script.write_text(passo["run"])
    caminho = os.environ["PATH"] if falsos is None else f"{falsos}:{os.environ['PATH']}"
    return subprocess.run(  # noqa: S603 - bash e o script do proprio workflow
        [BASH, "-e", str(script)],
        env={"PATH": caminho} | env,
        cwd=pasta,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# --- estrutura -------------------------------------------------------------


def test_jobs_e_dependencias_do_adr_042() -> None:
    jobs = _jobs()

    assert list(jobs) == [
        "ci",
        "security-scan",
        "image",
        "deploy-kind",
        "deploy-remote",
        "remote-skipped",
    ]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["security-scan"]["uses"] == "./.github/workflows/security.yml"
    # Os workflows reutilizaveis nao recebem segredo: nem `secrets:` explicito
    # nem `secrets: inherit`, so a chamada e as permissoes.
    assert set(jobs["ci"]) == {"uses", "permissions"}
    assert set(jobs["security-scan"]) == {"uses", "permissions"}
    assert jobs["image"]["needs"] == ["ci", "security-scan"]
    assert jobs["deploy-kind"]["needs"] == "image"
    assert jobs["deploy-remote"]["needs"] == ["image", "deploy-kind"]
    assert jobs["remote-skipped"]["needs"] == "deploy-kind"
    # O CD so comeca com o CI do commit verde: nada de `if` que deixe o image
    # rodar com o ci ou o security-scan vermelho, e o deploy-kind depende da imagem.
    assert "if" not in jobs["image"]
    assert "if" not in jobs["deploy-kind"]


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


def test_permissoes_minimas_por_job() -> None:
    workflow = _carrega()
    jobs = _jobs()

    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["ci"]["permissions"] == {"contents": "read"}
    assert jobs["security-scan"]["permissions"] == {"contents": "read"}
    assert jobs["image"]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["deploy-kind"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }
    assert jobs["deploy-remote"]["permissions"] == {
        "contents": "read",
        "packages": "read",
    }
    assert jobs["remote-skipped"]["permissions"] == {}
    # Nenhum job pede token de identidade, e so o image escreve pacote.
    escrevem = {
        nome
        for nome, job in jobs.items()
        if "write" in job.get("permissions", {}).values()
    }
    assert escrevem == {"image"}


def test_runner_fixo_e_prazo_em_todo_job_que_roda_passos() -> None:
    prazos = {"image": 30, "deploy-kind": 45, "deploy-remote": 20, "remote-skipped": 5}

    for nome, job in _jobs().items():
        if "uses" in job:
            continue
        assert job["runs-on"] == "ubuntu-24.04", nome
        assert job["timeout-minutes"] == prazos[nome], nome


def test_deploy_remote_vem_desligado_e_o_skipped_na_condicao_inversa() -> None:
    jobs = _jobs()

    assert (
        jobs["deploy-remote"]["if"]
        == "vars.DEPLOY_REMOTO_HABILITADO == 'true' && github.ref == 'refs/heads/main'"
    )
    assert jobs["remote-skipped"]["if"] == "vars.DEPLOY_REMOTO_HABILITADO != 'true'"
    passos = jobs["remote-skipped"]["steps"]
    assert len(passos) == 1
    assert passos[0]["run"].startswith('echo "::notice::deploy remoto desligado')
    assert "DEPLOY_REMOTO_HABILITADO" in passos[0]["run"]


def test_so_a_main_publica_imagem_e_implanta_no_remoto(tmp_path: Path) -> None:
    # O workflow_dispatch roda de qualquer ref: a guarda e o primeiro passo do
    # image, antes do checkout, do login no GHCR e do build, e o deploy-remote
    # repete a condicao.
    guarda = _passos("image")[0]

    assert "uses" not in guarda
    resultado = _executa(guarda, tmp_path, {"GITHUB_REF": "refs/heads/main"})
    assert resultado.returncode == 0, resultado.stderr
    assert "github.ref == 'refs/heads/main'" in _jobs()["deploy-remote"]["if"]


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/ci/cd",
        "refs/heads/main-2",
        "refs/heads/feature/main",
        "refs/tags/main",
        "refs/pull/12/merge",
        "",
    ],
    ids=["branch", "prefixo-de-main", "main-no-fim", "tag", "pull-request", "sem-ref"],
)
def test_ref_que_nao_e_a_main_nao_publica_imagem(tmp_path: Path, ref: str) -> None:
    resultado = _executa(_passos("image")[0], tmp_path, {"GITHUB_REF": ref})

    assert resultado.returncode == 1
    assert "::error::images are published from main only" in resultado.stdout


def test_deploy_remote_nao_tem_segredo_nem_credencial_de_provedor() -> None:
    # Ligar o deploy remoto acrescenta o passo de acesso ao cluster (README,
    # "Deploy remoto") e relaxa estas assercoes de proposito.
    job = _jobs()["deploy-remote"]
    workflow = yaml.safe_dump(_carrega()).lower()

    assert "environment" not in job
    assert "id-token" not in job["permissions"]
    assert "secrets." not in yaml.safe_dump(job).lower()
    # O unico segredo do workflow e o token do GHCR, no login do image.
    assert set(re.findall(r"secrets\.(\w+)", workflow)) == {"github_token"}
    # Nenhuma action de login de provedor: o acesso ao cluster e do dono do repositorio.
    usos = {
        passo["uses"].partition("@")[0] for passo in job["steps"] if "uses" in passo
    }
    assert usos == {"actions/checkout"}


def test_deploy_remote_usa_o_commit_do_platform_do_kind_e_o_digest_da_imagem() -> None:
    checkout = _passo_que_usa("deploy-remote", "actions/checkout")
    deploy = _passos("deploy-remote")[-1]

    assert checkout["with"]["ref"] == "${{ needs.deploy-kind.outputs.sha_platform }}"
    assert _jobs()["deploy-kind"]["outputs"] == {
        "sha_platform": "${{ steps.shas.outputs.sha_platform }}"
    }
    # Contexto, overlay, imagem e digest entram por env, nunca dentro do script.
    assert deploy["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "CONTEXTO": "${{ vars.DEPLOY_REMOTO_CONTEXTO }}",
        "OVERLAY": "${{ vars.DEPLOY_REMOTO_OVERLAY || 'k3s' }}",
        "IMAGEM": "${{ needs.image.outputs.imagem }}",
        "DIGEST": "${{ needs.image.outputs.digest }}",
    }
    chamada = 'deploy-remoto.sh --contexto "$CONTEXTO" --overlay "$OVERLAY"'
    assert chamada in deploy["run"]
    assert '--servico "$SERVICO" --imagem "${IMAGEM}@${DIGEST}"' in deploy["run"]
    # Sem o script do platform o job reprova nomeando o arquivo, em vez de
    # um "No such file" no meio do log.
    assert "deploy-remoto.sh does not exist" in deploy["run"]


# --- imagem ----------------------------------------------------------------


def test_image_publica_com_sbom_e_proveniencia_e_cache_do_gha() -> None:
    build = _passo_com_id("image", "build")

    assert build["uses"].startswith("docker/build-push-action@")
    com = build["with"]
    assert com["push"] is True
    assert com["sbom"].startswith("generator=docker/buildkit-syft-scanner@")
    assert com["provenance"] == "mode=min"
    assert com["tags"] == "${{ steps.meta.outputs.imagem }}"
    assert "GIT_SHA=${{ github.sha }}" in com["build-args"]
    assert "GIT_DATE=" in com["build-args"]
    assert com["cache-from"] == "type=gha,scope=${{ env.SERVICO }}"
    assert com["cache-to"] == "type=gha,mode=max,scope=${{ env.SERVICO }}"
    assert _jobs()["image"]["outputs"] == {
        "imagem": "${{ steps.meta.outputs.imagem }}",
        "digest": "${{ steps.build.outputs.digest }}",
    }
    meta = _passo_com_id("image", "meta")["run"]
    assert "ghcr.io/${DONO,,}/pytstop-${SERVICO}:${GITHUB_SHA}" in meta


DIGEST = r"sha256:[0-9a-f]{64}"


def test_buildkit_e_gerador_do_sbom_entram_por_digest() -> None:
    # As tags (buildx-stable-1 e stable-1) andam; este e o job que escreve no GHCR.
    buildx = _passo_que_usa("image", "docker/setup-buildx-action")
    sbom = _passo_com_id("image", "build")["with"]["sbom"]

    assert re.fullmatch(
        f"image=moby/buildkit@{DIGEST}\nprovenance-add-gha=false\n",
        buildx["with"]["driver-opts"],
    )
    assert re.fullmatch(f"generator=docker/buildkit-syft-scanner@{DIGEST}", sbom)


def test_proveniencia_minima_e_sem_o_contexto_do_github() -> None:
    # O mode=min sozinho nao basta: o buildx grava o payload do evento do push
    # (nome e e-mail dos autores) na proveniencia em qualquer mode, e ela fica
    # no registro publico. So o provenance-add-gha=false o tira.
    buildx = _passo_que_usa("image", "docker/setup-buildx-action")
    build = _passo_com_id("image", "build")

    assert build["with"]["provenance"] == "mode=min"
    assert "provenance-add-gha=false" in buildx["with"]["driver-opts"].splitlines()


def test_imagem_do_actionlint_do_makefile_por_digest() -> None:
    makefile = (RAIZ / "Makefile").read_text()

    assert re.search(
        rf"^ACTIONLINT_IMAGE := rhysd/actionlint:\d+\.\d+\.\d+@{DIGEST}$",
        makefile,
        re.MULTILINE,
    )


def test_image_exporta_o_tar_amd64_e_confere_o_digest() -> None:
    passo = next(
        passo for passo in _passos("image") if "docker save" in passo.get("run", "")
    )

    assert "docker pull --platform linux/amd64" in passo["run"]
    assert 'grep -qxF "Digest: $DIGEST"' in passo["run"]
    assert "docker save --platform linux/amd64 -o imagem.tar" in passo["run"]
    sbom = next(
        passo for passo in _passos("image") if "imagetools" in passo.get("run", "")
    )
    assert "--format '{{ json .SBOM }}'" in sbom["run"]
    # O buildx devolve {"SPDX": <documento>}: o artefato leva so o documento.
    assert "jq .SPDX sbom-buildx.json >" in sbom["run"]
    assert "sbom-${SERVICO}.spdx.json" in sbom["run"]
    assert "$GITHUB_STEP_SUMMARY" in sbom["run"]
    # O summary so abre para quem tem login: a mesma linha vai para o log.
    assert (
        'echo "image $IMAGEM digest $DIGEST, SBOM with $pacotes packages"'
        in sbom["run"]
    )


def test_artefatos_e_retencoes() -> None:
    envios = {
        _resolve(passo["with"]["name"]): passo["with"]["retention-days"]
        for _, passo in _todos_os_passos()
        if passo.get("uses", "").startswith("actions/upload-artifact@")
    }

    assert envios == {"imagem": 1, f"sbom-{SERVICO}": 90, "e2e-kind": 90}
    # Nenhum outro: a action de build anexaria o registro do build (90 dias).
    assert _passo_com_id("image", "build")["env"] == {
        "DOCKER_BUILD_RECORD_UPLOAD": False
    }
    sbom = _passo_que_usa("image", "actions/upload-artifact", f"sbom-{SERVICO}")
    assert _resolve(sbom["with"]["path"]) == f"sbom-{SERVICO}.spdx.json"
    assert _passo_que_usa("deploy-kind", "actions/download-artifact")["with"] == {
        "name": "imagem"
    }
    e2e = _passo_que_usa("deploy-kind", "actions/upload-artifact", "e2e-kind")
    assert e2e["with"]["path"] == "platform/reports/e2e/"
    assert e2e["if"] == "always()"


# --- o que reprova o job ----------------------------------------------------

_IGNORA_A_FALHA = re.compile(r"\|\|\s*(?:true\b|:|exit\s+0\b)|set\s+\+e")


@pytest.mark.parametrize("job", ["image", "deploy-kind"])
def test_nenhum_passo_dos_gates_ignora_a_falha(job: str) -> None:
    # O CD so fica verde com o E2E, o smoke e a conferencia da imagem verdes.
    assert "continue-on-error" not in _jobs()[job], job
    for passo in _passos(job):
        nome = passo.get("name") or passo.get("id") or passo.get("uses")
        assert "continue-on-error" not in passo, (job, nome)
        assert _IGNORA_A_FALHA.search(passo.get("run", "")) is None, (job, nome)


def test_smoke_e_e2e_sao_exatamente_os_comandos_do_adr() -> None:
    corridas = {
        passo["name"]: passo["run"]
        for passo in _passos("deploy-kind")
        if "name" in passo and "run" in passo
    }

    assert corridas["Smoke por namespace"] == (
        "platform/scripts/medir-kind.sh etapa smoke\n"
        "platform/scripts/ci/smoke-servicos.sh\n"
    )
    assert corridas["E2E BDD da saga (make e2e)"] == (
        "platform/scripts/medir-kind.sh etapa e2e\nmake -C platform e2e\n"
    )


# O docker falso so responde ao `buildx imagetools inspect`, com o conteudo de
# $SBOM_FAKE.
DOCKER_FALSO = """#!/usr/bin/env bash
if [ "$1" = buildx ] && [ "$2" = imagetools ] && [ "$3" = inspect ]; then
  cat "$SBOM_FAKE"
  exit 0
fi
exit 64
"""


def _roda_sbom(pasta: Path, conteudo: str) -> subprocess.CompletedProcess[str]:
    _exige_jq()
    bin_ = pasta / "bin"
    bin_.mkdir()
    docker = bin_ / "docker"
    docker.write_text(DOCKER_FALSO)
    docker.chmod(0o755)
    (pasta / "entrada.json").write_text(conteudo)
    (pasta / "resumo.md").touch()
    passo = next(p for p in _passos("image") if "imagetools" in p.get("run", ""))
    env = {
        "SBOM_FAKE": str(pasta / "entrada.json"),
        "IMAGEM": f"ghcr.io/org/pytstop-{SERVICO}:abc",
        "DIGEST": "sha256:def",
        "SERVICO": SERVICO,
        "GITHUB_STEP_SUMMARY": str(pasta / "resumo.md"),
    }
    return _executa(passo, pasta, env, falsos=bin_)


def test_sbom_com_pacotes_passa_e_conta_no_log_e_no_summary(tmp_path: Path) -> None:
    resultado = _roda_sbom(
        tmp_path, '{"SPDX":{"packages":[{"name":"a"},{"name":"b"}]}}'
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "SBOM with 2 packages" in resultado.stdout
    assert "| SBOM (SPDX) | 2 packages," in (tmp_path / "resumo.md").read_text()
    # O artefato e o documento SPDX, sem o involucro {"SPDX": ...} do buildx.
    assert json.loads((tmp_path / f"sbom-{SERVICO}.spdx.json").read_text()) == {
        "packages": [{"name": "a"}, {"name": "b"}]
    }


@pytest.mark.parametrize(
    "conteudo",
    [
        pytest.param("null", id="sem-sbom"),
        pytest.param('{"SPDX":{"packages":[]}}', id="sem-pacote"),
        pytest.param("", id="saida-vazia"),
        pytest.param("nao e json", id="nao-json"),
    ],
)
def test_sbom_vazio_ou_invalido_reprova(tmp_path: Path, conteudo: str) -> None:
    resultado = _roda_sbom(tmp_path, conteudo)

    assert resultado.returncode != 0
    assert "SBOM with" not in resultado.stdout


# --- deploy-kind -----------------------------------------------------------


def test_deploy_kind_roda_os_scripts_do_platform_na_ordem_do_adr() -> None:
    corridas = [
        passo["run"]
        for passo in _passos("deploy-kind")
        if "platform/scripts" in passo.get("run", "")
        or "make -C" in passo.get("run", "")
    ]
    texto = "\n".join(corridas)
    ordem = [
        "platform/scripts/ci/deploy-kind.sh",
        "medir-kind.sh etapa servicos",
        "platform/scripts/ci/implantar-servicos.sh --overlay kind-ci",
        "platform/scripts/ci/smoke-servicos.sh",
        "medir-kind.sh etapa e2e",
        "make -C platform e2e",
        "platform/scripts/medir-kind.sh resumo",
        "platform/scripts/ci/diagnostico.sh",
    ]
    posicoes = [texto.index(trecho) for trecho in ordem]

    assert posicoes == sorted(posicoes)
    implantar = next(c for c in corridas if "implantar-servicos.sh" in c)
    # A imagem deste servico vem do tar do job image; os vizinhos, do codigo.
    assert '--tar "${SERVICO}=imagem.tar" --imagem "${SERVICO}=$IMAGEM"' in implantar
    for servico in SERVICOS:
        assert f"{servico}={servico}" in implantar


def test_resumo_e_upload_sempre_e_diagnostico_na_falha_ou_cancelamento() -> None:
    passos = _passos("deploy-kind")
    resumo = next(
        p for p in passos if p.get("run") == "platform/scripts/medir-kind.sh resumo"
    )
    diagnostico = next(p for p in passos if "diagnostico.sh" in p.get("run", ""))

    assert (
        resumo["if"] == "always() && hashFiles('platform/scripts/medir-kind.sh') != ''"
    )
    assert diagnostico["if"] == (
        "(failure() || cancelled()) "
        "&& hashFiles('platform/scripts/ci/diagnostico.sh') != ''"
    )
    upload = _passo_que_usa("deploy-kind", "actions/upload-artifact", "e2e-kind")
    assert upload["if"] == "always()"
    # O diagnostico e o ultimo passo: roda com o cluster ainda de pe.
    assert passos[-1] is diagnostico


def test_relatorios_do_e2e_passam_pela_varredura_antes_do_upload() -> None:
    passos = _passos("deploy-kind")
    varredura = next(p for p in passos if "e2e.varredura" in p.get("run", ""))
    upload = _passo_que_usa("deploy-kind", "actions/upload-artifact", "e2e-kind")

    assert varredura["run"] == "uv run --group e2e python -m e2e.varredura reports/e2e"
    assert varredura["working-directory"] == "platform"
    # Com ou sem falha do E2E, desde que haja relatorio a varrer.
    assert varredura["if"] == "always() && hashFiles('platform/reports/e2e/**') != ''"
    assert passos.index(varredura) < passos.index(upload)
    # Sobe so a pasta que a varredura olha.
    assert upload["with"]["path"] == "platform/reports/e2e/"


def test_passo_pos_falha_so_roda_com_o_script_do_platform_no_disco() -> None:
    # Sem o checkout do platform (o passo shas reprovou) nao ha script a rodar:
    # o passo seria um "No such file" a mais, nao um diagnostico.
    apos_falha = re.compile(r"always\(\)|failure\(\)|cancelled\(\)")
    verificados = 0
    for passo in _passos("deploy-kind"):
        condicao = passo.get("if", "")
        if not apos_falha.search(condicao):
            continue
        for script in re.findall(r"platform/scripts/\S+\.sh", passo.get("run", "")):
            assert f"hashFiles('{script}') != ''" in condicao, passo["name"]
            verificados += 1

    assert verificados == 2


def test_checkouts_do_platform_e_dos_vizinhos_nos_shas_e_sem_credencial() -> None:
    checkouts = [
        passo
        for passo in _passos("deploy-kind")
        if passo.get("uses", "").startswith("actions/checkout@")
    ]

    # Os quatro checkouts sao iguais em qualquer um dos tres repositorios: o
    # proprio servico tambem entra pelo nome e pelo sha que o passo shas grava.
    dono = "${{ github.repository_owner }}"
    assert {
        c["with"]["path"]: (c["with"]["repository"], c["with"]["ref"])
        for c in checkouts
    } == {
        "platform": (
            f"{dono}/postech-sw-arch-p4-platform",
            "${{ steps.shas.outputs.sha_platform }}",
        ),
        **{
            servico: (
                f"{dono}/postech-sw-arch-p4-{servico}",
                f"${{{{ steps.shas.outputs.{_saida(servico)} }}}}",
            )
            for servico in SERVICOS
        },
    }
    for _, passo in _todos_os_passos():
        if passo.get("uses", "").startswith("actions/checkout@"):
            assert passo["with"]["persist-credentials"] is False
    # A escolha e a validacao dos SHAs vem antes de qualquer checkout.
    passos = _passos("deploy-kind")
    assert passos[0]["id"] == "shas"


def test_inputs_do_dispatch_so_entram_por_env_e_o_passo_valida_o_padrao() -> None:
    passo = _passo_com_id("deploy-kind", "shas")

    assert {
        chave: valor
        for chave, valor in passo["env"].items()
        if chave.startswith("SHA_") and chave != "SHA_PROPRIO"
    } == {
        "SHA_PLATFORM": "${{ inputs.sha_platform }}",
        "SHA_OS_SERVICE": "${{ inputs.sha_os_service }}",
        "SHA_BILLING_SERVICE": "${{ inputs.sha_billing_service }}",
        "SHA_EXECUTION_SERVICE": "${{ inputs.sha_execution_service }}",
    }
    assert "padrao='^[0-9a-f]{40}$'" in passo["run"]
    assert passo["env"]["GH_TOKEN"] == "${{ github.token }}"


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


# --- actions por SHA -------------------------------------------------------
# O teste abaixo confere a forma (SHA de 40 hexadecimais e a tag em comentario).
# Que o SHA seja mesmo o commit da tag so se sabe perguntando ao GitHub, entao
# essa conferencia e o teste `rede` mais adiante.

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
        if ref.startswith("./"):
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
    blocos = {b["package-ecosystem"]: b for b in config["updates"]}

    assert set(blocos) == {"uv", "github-actions", "docker"}
    for bloco in blocos.values():
        assert bloco["directory"] == "/"
        assert bloco["schedule"]["interval"] == "weekly"
        assert bloco["open-pull-requests-limit"] == 3
    assert {nome: b["commit-message"]["prefix"] for nome, b in blocos.items()} == {
        "uv": "chore(deps)",
        "github-actions": "ci",
        "docker": "chore(deps)",
    }
    assert blocos["github-actions"]["groups"] == {
        "actions-patch-e-minor": {"update-types": ["minor", "patch"]}
    }
    # O primeiro grupo que casa leva a dependencia: o do OpenTelemetry vem antes
    # do generico, senao o SDK subiria sem as instrumentacoes da mesma versao.
    assert list(blocos["uv"]["groups"]) == ["opentelemetry", "python-patch-e-minor"]
    # O Python do Dockerfile sobe a mao, junto do .python-version e dos workflows.
    assert blocos["docker"]["ignore"] == [
        {
            "dependency-name": "python",
            "update-types": [
                "version-update:semver-major",
                "version-update:semver-minor",
            ],
        }
    ]


def test_job_que_escreve_pacote_so_usa_actions_por_sha() -> None:
    # O image e o unico job com packages: write, entao ate as oficiais (que nos
    # outros jobs entram pela tag de versao) entram aqui pelo SHA do commit.
    usos = [passo["uses"] for passo in _passos("image") if "uses" in passo]

    assert usos
    for uso in usos:
        assert SHA_PADRAO.match(uso.partition("@")[2]), uso


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
        if SHA_PADRAO.match(versao)
    }

    assert len(fixadas) > 3
    obtidos = {
        (acao, tag): (versao, _commit_da_tag("/".join(acao.split("/")[:2]), tag))
        for acao, versao, tag in sorted(fixadas)
    }
    divergentes = {chave: par for chave, par in obtidos.items() if par[0] != par[1]}
    assert divergentes == {}


# --- o passo dos commits, executado ---------------------------------------

REPOS = {"platform": f"{DONO}/postech-sw-arch-p4-platform"} | {
    servico: f"{DONO}/postech-sw-arch-p4-{servico}" for servico in VIZINHOS
}
PROPRIO = "a" * 40
VERDE = {
    "platform": "1" * 40,
    PRIMEIRO_VIZINHO: "2" * 40,
    SEGUNDO_VIZINHO: "3" * 40,
}
MAIN = {
    "platform": "4" * 40,
    PRIMEIRO_VIZINHO: "5" * 40,
    SEGUNDO_VIZINHO: "6" * 40,
}

# O gh falso responde com o JSON de um diretorio e aplica o --jq de verdade, de
# modo que o filtro do workflow e testado contra o formato da API.
GH_FALSO = """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_GH/chamadas"
[ "$1" = api ] || exit 64
shift
filtro=""
endpoint=""
while [ "$#" -gt 0 ]; do
  case $1 in
    --jq) filtro=$2; shift 2 ;;
    -X | -f) shift 2 ;;
    repos/*) endpoint=$1; shift ;;
    *) exit 64 ;;
  esac
done
base="$FAKE_GH/$(tr '/' '_' <<< "$endpoint")"
if [ -f "$base.erro" ]; then cat "$base.erro" >&2; exit 1; fi
[ -f "$base.json" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
jq -r "$filtro" "$base.json"
"""


def _exige_jq() -> None:
    if shutil.which("jq") is None:
        # Sem o jq somem, calados, os testes que executam os passos do workflow.
        mensagem = "jq nao esta no PATH: os testes dos passos do workflow precisam dele"
        if os.environ.get("CI"):
            pytest.fail(mensagem)
        pytest.skip(mensagem)


def _gh_falso(pasta: Path) -> tuple[Path, Path]:
    """O diretorio dos comandos falsos (com o ``gh``) e o das respostas dele."""
    _exige_jq()
    fake = pasta / "gh"
    fake.mkdir()
    bin_ = pasta / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text(GH_FALSO)
    gh.chmod(0o755)
    return bin_, fake


def _arquivo_do_endpoint(fake: Path, endpoint: str, sufixo: str) -> Path:
    return fake / (endpoint.replace("/", "_") + sufixo)


class _Ambiente:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.bin, self.fake = _gh_falso(tmp_path)
        script = tmp_path / "shas.sh"
        script.write_text(_passo_com_id("deploy-kind", "shas")["run"])
        self.script = script
        self.saida = tmp_path / "saida"
        self.resumo = tmp_path / "resumo"
        self.saida.touch()
        self.resumo.touch()

    def _arquivo(self, endpoint: str, sufixo: str) -> Path:
        return _arquivo_do_endpoint(self.fake, endpoint, sufixo)

    def corridas(self, repo: str, *shas: str) -> None:
        corpo = {
            "total_count": len(shas),
            "workflow_runs": [
                {"head_sha": sha, "conclusion": "success"} for sha in shas
            ],
        }
        endpoint = f"repos/{REPOS[repo]}/actions/workflows/cd.yml/runs"
        self._arquivo(endpoint, ".json").write_text(json.dumps(corpo))

    def main(self, repo: str, sha: str) -> None:
        self._arquivo(f"repos/{REPOS[repo]}/commits/main", ".json").write_text(
            json.dumps({"sha": sha})
        )

    def falha_da_api(self, repo: str, mensagem: str) -> None:
        endpoint = f"repos/{REPOS[repo]}/actions/workflows/cd.yml/runs"
        self._arquivo(endpoint, ".erro").write_text(mensagem + "\n")

    def falha_da_main(self, repo: str, mensagem: str) -> None:
        endpoint = f"repos/{REPOS[repo]}/commits/main"
        self._arquivo(endpoint, ".erro").write_text(mensagem + "\n")

    def todos_verdes(self) -> None:
        for repo, sha in VERDE.items():
            self.corridas(repo, sha)
            self.main(repo, MAIN[repo])

    def roda(self, **entradas: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_GH": str(self.fake),
            "GITHUB_OUTPUT": str(self.saida),
            "GITHUB_STEP_SUMMARY": str(self.resumo),
            "GH_TOKEN": "x",
            "DONO": DONO,
            "SERVICO": SERVICO,
            "SHA_PROPRIO": PROPRIO,
            "SHA_PLATFORM": "",
            **{_entrada(servico): "" for servico in SERVICOS},
        } | entradas
        return subprocess.run(  # noqa: S603 - bash e o script do proprio workflow
            [BASH, str(self.script)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def saidas(self) -> dict[str, str]:
        return dict(
            linha.split("=", 1) for linha in self.saida.read_text().splitlines()
        )

    def chamadas(self) -> list[str]:
        arquivo = self.fake / "chamadas"
        return arquivo.read_text().splitlines() if arquivo.exists() else []


@pytest.fixture
def amb(tmp_path: Path) -> _Ambiente:
    return _Ambiente(tmp_path)


def test_sem_input_usa_o_ultimo_cd_verde_de_cada_repositorio(amb: _Ambiente) -> None:
    amb.todos_verdes()

    resultado = amb.roda()

    assert resultado.returncode == 0, resultado.stderr
    assert amb.saidas() == {
        "sha_platform": VERDE["platform"],
        _saida(SERVICO): PROPRIO,
        _saida(PRIMEIRO_VIZINHO): VERDE[PRIMEIRO_VIZINHO],
        _saida(SEGUNDO_VIZINHO): VERDE[SEGUNDO_VIZINHO],
    }
    assert "::warning::" not in resultado.stdout
    consultas = [c for c in amb.chamadas() if "workflows/cd.yml/runs" in c]
    assert len(consultas) == 3
    for consulta in consultas:
        # Os filtros da consulta: o ultimo push verde na main, um so resultado.
        for filtro in (
            "-X GET",
            "branch=main",
            "event=push",
            "status=success",
            "per_page=1",
        ):
            assert filtro in consulta
    tabela = amb.resumo.read_text()
    vizinho = f"postech-sw-arch-p4-{PRIMEIRO_VIZINHO} | `{VERDE[PRIMEIRO_VIZINHO]}`"
    assert f"| {vizinho} | last green CD |" in tabela
    assert f"| postech-sw-arch-p4-{SERVICO} | `{PROPRIO}` | this run |" in tabela


def test_repositorio_sem_cd_yml_cai_na_main_head_com_aviso(amb: _Ambiente) -> None:
    amb.todos_verdes()
    for repo in VIZINHOS:
        # O endpoint do workflow responde 404 enquanto o cd.yml nao existe.
        (
            amb.fake
            / (
                f"repos/{REPOS[repo]}/actions/workflows/cd.yml/runs".replace("/", "_")
                + ".json"
            )
        ).unlink()

    resultado = amb.roda()

    assert resultado.returncode == 0, resultado.stderr
    saidas = amb.saidas()
    assert saidas["sha_platform"] == VERDE["platform"]
    assert saidas[_saida(PRIMEIRO_VIZINHO)] == MAIN[PRIMEIRO_VIZINHO]
    assert saidas[_saida(SEGUNDO_VIZINHO)] == MAIN[SEGUNDO_VIZINHO]
    avisos = [
        linha
        for linha in resultado.stdout.splitlines()
        if linha.startswith("::warning::")
    ]
    assert avisos == [
        f"::warning::postech-sw-arch-p4-{repo} has no green CD run on main yet: "
        f"using main HEAD {MAIN[repo]}"
        for repo in VIZINHOS
    ]
    assert "main HEAD, no green CD yet" in amb.resumo.read_text()


def test_workflow_sem_nenhum_run_verde_tambem_cai_na_main_head(amb: _Ambiente) -> None:
    amb.todos_verdes()
    amb.corridas("platform")

    resultado = amb.roda()

    assert resultado.returncode == 0, resultado.stderr
    assert amb.saidas()["sha_platform"] == MAIN["platform"]
    assert resultado.stdout.count("::warning::") == 1


def test_input_do_dispatch_vence_e_dispensa_a_consulta(amb: _Ambiente) -> None:
    amb.todos_verdes()
    fixo = "c" * 40

    resultado = amb.roda(
        **{_entrada(PRIMEIRO_VIZINHO): fixo, _entrada(SERVICO): PROPRIO}
    )

    assert resultado.returncode == 0, resultado.stderr
    assert amb.saidas()[_saida(PRIMEIRO_VIZINHO)] == fixo
    assert not any(PRIMEIRO_VIZINHO in c for c in amb.chamadas())
    assert f"`{fixo}` | dispatch input" in amb.resumo.read_text()


@pytest.mark.parametrize(
    "valor",
    [
        "A" * 40,
        "a" * 39,
        "a" * 41,
        "g" * 40,
        "a" * 39 + "\n",
        "main",
        "a" * 40 + "; echo pwned",
        "$(echo pwned)",
        "a" * 40 + "\n::set-output name=x::y",
    ],
    ids=[
        "maiusculas",
        "39",
        "41",
        "fora-do-hexa",
        "quebra-de-linha",
        "branch",
        "comando-depois",
        "substituicao",
        "comando-de-workflow",
    ],
)
@pytest.mark.parametrize(
    "entrada",
    ["SHA_PLATFORM", *(_entrada(servico) for servico in SERVICOS)],
)
def test_input_fora_do_padrao_reprova_antes_de_qualquer_consulta_sem_ecoar_o_valor(
    amb: _Ambiente, entrada: str, valor: str
) -> None:
    amb.todos_verdes()

    resultado = amb.roda(**{entrada: valor})

    assert resultado.returncode == 1
    assert (
        f"::error::input {entrada.lower()} must be 40 lowercase hexadecimal characters"
        in resultado.stdout
    )
    assert "pwned" not in resultado.stdout + resultado.stderr
    assert "set-output" not in resultado.stdout + resultado.stderr
    assert amb.chamadas() == []
    assert amb.saida.read_text() == ""


def test_sha_do_proprio_servico_que_nao_e_o_do_run_reprova(amb: _Ambiente) -> None:
    amb.todos_verdes()

    resultado = amb.roda(**{_entrada(SERVICO): "b" * 40})

    assert resultado.returncode == 1
    assert (
        f"{_saida(SERVICO)} must be empty or the commit of this run" in resultado.stdout
    )
    assert amb.chamadas() == []


def test_falha_da_api_que_nao_e_404_reprova_em_vez_de_cair_na_main(
    amb: _Ambiente,
) -> None:
    amb.todos_verdes()
    amb.falha_da_api(PRIMEIRO_VIZINHO, "gh: API rate limit exceeded (HTTP 403)")

    resultado = amb.roda()

    assert resultado.returncode != 0
    assert "HTTP 403" in resultado.stderr
    assert "::warning::" not in resultado.stdout
    assert amb.saida.read_text() == ""


@pytest.mark.parametrize(
    "mensagem",
    ["gh: Not Found (HTTP 404)", "gh: Server Error (HTTP 500)"],
    ids=["404", "500"],
)
def test_falha_ao_ler_a_main_head_reprova_sem_gravar_a_saida(
    amb: _Ambiente, mensagem: str
) -> None:
    # Sem run verde o passo le a main HEAD: se essa segunda chamada falha, o
    # passo reprova, em vez de seguir com um commit que nao resolveu.
    amb.todos_verdes()
    amb.corridas(PRIMEIRO_VIZINHO)
    amb.falha_da_main(PRIMEIRO_VIZINHO, mensagem)

    resultado = amb.roda()

    assert resultado.returncode != 0
    assert mensagem in resultado.stderr
    assert "::warning::" not in resultado.stdout
    assert amb.saida.read_text() == ""


def test_sem_jq_a_fixture_falha_na_ci_e_pula_fora_dela(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda nome: None)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        _exige_jq()

    monkeypatch.delenv("CI")
    with pytest.raises(pytest.skip.Exception):
        _exige_jq()


def test_resposta_que_nao_e_um_commit_reprova(amb: _Ambiente) -> None:
    amb.todos_verdes()
    amb.corridas(SEGUNDO_VIZINHO, "nao-e-um-sha")

    resultado = amb.roda()

    assert resultado.returncode == 1
    assert (
        f"could not resolve a commit for postech-sw-arch-p4-{SEGUNDO_VIZINHO}"
        in resultado.stdout
    )
    assert amb.saida.read_text() == ""


# --- o deploy-remote, executado ---------------------------------------------

SHA_DO_PLATFORM = "9" * 40
PASSO_DA_COMPARACAO = "Commit do platform esta na main do platform"


def test_a_comparacao_com_a_main_do_platform_e_o_primeiro_passo_do_deploy_remote() -> (
    None
):
    # Antes do checkout do platform, do acesso ao cluster e do deploy: o commit
    # que nao esta na main do platform nem chega a ser baixado.
    passos = _passos("deploy-remote")
    comparacao = passos[0]

    assert comparacao["name"] == PASSO_DA_COMPARACAO
    assert comparacao["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "DONO": "${{ github.repository_owner }}",
        "SHA_PLATFORM": "${{ needs.deploy-kind.outputs.sha_platform }}",
    }
    assert (
        "postech-sw-arch-p4-platform/compare/main...$SHA_PLATFORM" in comparacao["run"]
    )
    assert passos[1]["uses"].startswith("actions/checkout@")


def _compara_com_a_main(
    pasta: Path, status: str | None
) -> subprocess.CompletedProcess[str]:
    """Roda o passo com o compare do GitHub respondendo ``status`` (``None``: 404)."""
    bin_, fake = _gh_falso(pasta)
    if status is not None:
        endpoint = f"repos/{REPOS['platform']}/compare/main...{SHA_DO_PLATFORM}"
        _arquivo_do_endpoint(fake, endpoint, ".json").write_text(
            json.dumps({"status": status})
        )
    env = {
        "FAKE_GH": str(fake),
        "GH_TOKEN": "x",
        "DONO": DONO,
        "SHA_PLATFORM": SHA_DO_PLATFORM,
    }
    return _executa(_passos("deploy-remote")[0], pasta, env, falsos=bin_)


@pytest.mark.parametrize("status", ["identical", "behind"])
def test_commit_que_ja_esta_na_main_do_platform_passa(
    tmp_path: Path, status: str
) -> None:
    resultado = _compara_com_a_main(tmp_path, status)

    assert resultado.returncode == 0, resultado.stderr
    chamadas = (tmp_path / "gh" / "chamadas").read_text().splitlines()
    comparacao = f"repos/{REPOS['platform']}/compare/main...{SHA_DO_PLATFORM}"
    assert chamadas == [f"api {comparacao} --jq .status"]


@pytest.mark.parametrize(
    "status",
    ["diverged", "ahead", "", "null"],
    ids=["diverged", "ahead", "vazio", "null"],
)
def test_commit_fora_da_main_do_platform_reprova(tmp_path: Path, status: str) -> None:
    resultado = _compara_com_a_main(tmp_path, status)

    assert resultado.returncode == 1
    assert (
        f"::error::the platform commit {SHA_DO_PLATFORM} is not in the history "
        f"of the platform main (compare status: {status})"
    ) in resultado.stdout


def test_commit_que_o_github_nao_conhece_reprova(tmp_path: Path) -> None:
    resultado = _compara_com_a_main(tmp_path, None)

    assert resultado.returncode == 1
    assert "::error::could not compare the platform commit" in resultado.stdout
    assert "HTTP 404" in resultado.stderr


def _roda_deploy_remote(
    pasta: Path, contexto: str, *, com_script: bool = True
) -> subprocess.CompletedProcess[str]:
    """Roda o ultimo passo do deploy-remote com um script que grava os argumentos."""
    if com_script:
        script = pasta / "platform" / "scripts" / "ci" / "deploy-remoto.sh"
        script.parent.mkdir(parents=True)
        script.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > args.txt\n')
        script.chmod(0o755)
    env = {
        "CONTEXTO": contexto,
        "OVERLAY": "k3s",
        "IMAGEM": f"ghcr.io/org/pytstop-{SERVICO}:abc",
        "DIGEST": "sha256:def",
        "SERVICO": SERVICO,
    }
    return _executa(_passos("deploy-remote")[-1], pasta, env)


def test_deploy_remote_chama_o_script_do_platform_com_o_digest(tmp_path: Path) -> None:
    resultado = _roda_deploy_remote(tmp_path, "meu-cluster")

    assert resultado.returncode == 0, resultado.stderr
    assert (tmp_path / "args.txt").read_text().splitlines() == [
        "--contexto",
        "meu-cluster",
        "--overlay",
        "k3s",
        "--servico",
        SERVICO,
        "--imagem",
        f"ghcr.io/org/pytstop-{SERVICO}:abc@sha256:def",
    ]


def test_deploy_remote_sem_contexto_falha_cedo_nomeando_a_variavel(
    tmp_path: Path,
) -> None:
    resultado = _roda_deploy_remote(tmp_path, "")

    assert resultado.returncode == 1
    assert "::error::DEPLOY_REMOTO_CONTEXTO is not set" in resultado.stdout
    assert not (tmp_path / "args.txt").exists()


def test_deploy_remote_sem_o_script_no_platform_reprova_nomeando_o_arquivo(
    tmp_path: Path,
) -> None:
    resultado = _roda_deploy_remote(tmp_path, "meu-cluster", com_script=False)

    assert resultado.returncode == 1
    assert "deploy-remoto.sh does not exist in the platform commit" in resultado.stdout
