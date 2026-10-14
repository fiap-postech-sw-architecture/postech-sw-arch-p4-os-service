"""O ``cd.yml`` (ADR-042): a estrutura do workflow lida do YAML, a regra de
seguranca dos inputs e o passo que escolhe os commits do platform e dos
vizinhos, executado de verdade com um ``gh`` falso."""

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


# --- estrutura -------------------------------------------------------------


def test_jobs_e_dependencias_do_adr_042() -> None:
    jobs = _jobs()

    assert list(jobs) == [
        "ci",
        "security-scan",
        "image",
        "deploy-kind",
        "deploy-k3s",
        "k3s-skipped",
    ]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["security-scan"]["uses"] == "./.github/workflows/security.yml"
    assert jobs["image"]["needs"] == ["ci", "security-scan"]
    assert jobs["deploy-kind"]["needs"] == "image"
    assert jobs["deploy-k3s"]["needs"] == ["image", "deploy-kind"]
    assert jobs["k3s-skipped"]["needs"] == "deploy-kind"
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
    assert jobs["deploy-k3s"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "packages": "read",
    }
    assert jobs["k3s-skipped"]["permissions"] == {}
    # So o deploy-k3s pede o token de identidade, e so o image escreve pacote.
    escrevem = {
        nome
        for nome, job in jobs.items()
        if "write" in job.get("permissions", {}).values()
    }
    assert escrevem == {"image", "deploy-k3s"}


def test_runner_fixo_e_prazo_em_todo_job_que_roda_passos() -> None:
    prazos = {"image": 30, "deploy-kind": 45, "deploy-k3s": 20, "k3s-skipped": 5}

    for nome, job in _jobs().items():
        if "uses" in job:
            continue
        assert job["runs-on"] == "ubuntu-24.04", nome
        assert job["timeout-minutes"] == prazos[nome], nome


def test_deploy_k3s_so_com_a_variavel_ligada_e_o_k3s_skipped_na_condicao_inversa() -> (
    None
):
    jobs = _jobs()

    assert jobs["deploy-k3s"]["if"] == "vars.K3S_HABILITADO == 'true'"
    assert jobs["deploy-k3s"]["environment"] == "producao"
    assert jobs["k3s-skipped"]["if"] == "vars.K3S_HABILITADO != 'true'"
    passos = jobs["k3s-skipped"]["steps"]
    assert len(passos) == 1
    assert passos[0]["run"].startswith('echo "::notice::')


def test_deploy_k3s_usa_o_commit_do_platform_do_kind_e_o_digest_da_imagem() -> None:
    checkout = _passo_que_usa("deploy-k3s", "actions/checkout")
    deploy = _passos("deploy-k3s")[-1]

    assert checkout["with"]["ref"] == "${{ needs.deploy-kind.outputs.sha_platform }}"
    assert _jobs()["deploy-kind"]["outputs"] == {
        "sha_platform": "${{ steps.shas.outputs.sha_platform }}"
    }
    assert deploy["env"]["DIGEST"] == "${{ needs.image.outputs.digest }}"
    assert '--imagem "${IMAGEM}@${DIGEST}"' in deploy["run"]
    # Sem o script do platform o job reprova nomeando o arquivo, em vez de
    # um "No such file" no meio do log.
    assert "deploy-k3s.sh does not exist" in deploy["run"]


# --- imagem ----------------------------------------------------------------


def test_image_publica_com_sbom_e_proveniencia_e_cache_do_gha() -> None:
    build = _passo_com_id("image", "build")

    assert build["uses"].startswith("docker/build-push-action@")
    com = build["with"]
    assert com["push"] is True
    assert com["sbom"] is True
    assert com["provenance"] == "mode=max"
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
    assert "sbom-${SERVICO}.spdx.json" in sbom["run"]
    assert "$GITHUB_STEP_SUMMARY" in sbom["run"]


def test_artefatos_e_retencoes() -> None:
    envios = {
        _resolve(passo["with"]["name"]): passo["with"]["retention-days"]
        for _, passo in _todos_os_passos()
        if passo.get("uses", "").startswith("actions/upload-artifact@")
    }

    assert envios == {"imagem": 1, "sbom-os-service": 90, "e2e-kind": 90}
    assert _passo_que_usa("deploy-kind", "actions/download-artifact")["with"] == {
        "name": "imagem"
    }
    e2e = _passo_que_usa("deploy-kind", "actions/upload-artifact", "e2e-kind")
    assert e2e["with"]["path"] == "platform/reports/"
    assert e2e["if"] == "always()"


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
    assert '--tar "os-service=imagem.tar" --imagem "os-service=$IMAGEM"' in implantar
    for servico in ("os-service", "billing-service", "execution-service"):
        assert f"{servico}={servico}" in implantar


def test_resumo_e_upload_sempre_e_diagnostico_na_falha_ou_cancelamento() -> None:
    passos = _passos("deploy-kind")
    resumo = next(
        p for p in passos if p.get("run") == "platform/scripts/medir-kind.sh resumo"
    )
    diagnostico = next(p for p in passos if "diagnostico.sh" in p.get("run", ""))

    assert resumo["if"] == "always()"
    assert diagnostico["if"] == "failure() || cancelled()"
    # O diagnostico e o ultimo passo: roda com o cluster ainda de pe.
    assert passos[-1] is diagnostico


def test_checkouts_do_platform_e_dos_vizinhos_nos_shas_e_sem_credencial() -> None:
    checkouts = [
        passo
        for passo in _passos("deploy-kind")
        if passo.get("uses", "").startswith("actions/checkout@")
    ]

    assert {c["with"]["path"]: c["with"]["ref"] for c in checkouts} == {
        "platform": "${{ steps.shas.outputs.sha_platform }}",
        "os-service": "${{ steps.shas.outputs.sha_os_service }}",
        "billing-service": "${{ steps.shas.outputs.sha_billing_service }}",
        "execution-service": "${{ steps.shas.outputs.sha_execution_service }}",
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
    # Nada de ${{ }} em script: input, saida de passo e contexto do github
    # entram por env, onde o shell os trata como dado.
    com_expressao = [
        (job, passo.get("name") or passo.get("id") or passo["run"][:40])
        for job, passo in _todos_os_passos()
        if "run" in passo and "${{" in passo["run"]
    ]

    assert com_expressao == []


def test_nenhum_arquivo_do_workflow_interpola_input_em_run() -> None:
    for arquivo in WORKFLOWS.glob("*.yml"):
        for job in _carrega(arquivo)["jobs"].values():
            for passo in job.get("steps", []):
                run = passo.get("run", "")
                assert "github.event.inputs" not in run, arquivo.name
                assert "${{ inputs." not in run, arquivo.name


# --- actions de terceiro por SHA ------------------------------------------

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
        if dono.startswith("actions/"):
            assert re.fullmatch(r"v\d+(\.\d+)*", versao), ref
        else:
            assert SHA_PADRAO.match(versao), f"{ref}: action de terceiro sem SHA"
            assert uso["tag"], f"{ref}: sem a tag em comentario"
            assert re.match(r"v\d+(\.\d+)*", uso["tag"]), ref


# --- o passo dos commits, executado ---------------------------------------

REPOS = {
    "platform": f"{DONO}/postech-sw-arch-p4-platform",
    "billing-service": f"{DONO}/postech-sw-arch-p4-billing-service",
    "execution-service": f"{DONO}/postech-sw-arch-p4-execution-service",
}
BASH = shutil.which("bash") or "/bin/bash"
PROPRIO = "a" * 40
VERDE = {
    "platform": "1" * 40,
    "billing-service": "2" * 40,
    "execution-service": "3" * 40,
}
MAIN = {
    "platform": "4" * 40,
    "billing-service": "5" * 40,
    "execution-service": "6" * 40,
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


class _Ambiente:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.fake = tmp_path / "gh"
        self.fake.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(GH_FALSO)
        gh.chmod(0o755)
        script = tmp_path / "shas.sh"
        script.write_text(_passo_com_id("deploy-kind", "shas")["run"])
        self.script = script
        self.saida = tmp_path / "saida"
        self.resumo = tmp_path / "resumo"
        self.saida.touch()
        self.resumo.touch()

    def _arquivo(self, endpoint: str, sufixo: str) -> Path:
        return self.fake / (endpoint.replace("/", "_") + sufixo)

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
            "SHA_PROPRIO": PROPRIO,
            "SHA_PLATFORM": "",
            "SHA_OS_SERVICE": "",
            "SHA_BILLING_SERVICE": "",
            "SHA_EXECUTION_SERVICE": "",
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
    if shutil.which("jq") is None:
        pytest.skip("jq nao esta no PATH")
    return _Ambiente(tmp_path)


def test_sem_input_usa_o_ultimo_cd_verde_de_cada_repositorio(amb: _Ambiente) -> None:
    amb.todos_verdes()

    resultado = amb.roda()

    assert resultado.returncode == 0, resultado.stderr
    assert amb.saidas() == {
        "sha_platform": VERDE["platform"],
        "sha_os_service": PROPRIO,
        "sha_billing_service": VERDE["billing-service"],
        "sha_execution_service": VERDE["execution-service"],
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
    billing = f"postech-sw-arch-p4-billing-service | `{VERDE['billing-service']}`"
    assert f"| {billing} | last green CD |" in tabela
    assert f"| postech-sw-arch-p4-os-service | `{PROPRIO}` | this run |" in tabela


def test_repositorio_sem_cd_yml_cai_na_main_head_com_aviso(amb: _Ambiente) -> None:
    amb.todos_verdes()
    for repo in ("billing-service", "execution-service"):
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
    assert saidas["sha_billing_service"] == MAIN["billing-service"]
    assert saidas["sha_execution_service"] == MAIN["execution-service"]
    avisos = [
        linha
        for linha in resultado.stdout.splitlines()
        if linha.startswith("::warning::")
    ]
    assert avisos == [
        f"::warning::postech-sw-arch-p4-{repo} has no green CD run on main yet: "
        f"using main HEAD {MAIN[repo]}"
        for repo in ("billing-service", "execution-service")
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

    resultado = amb.roda(SHA_BILLING_SERVICE=fixo, SHA_OS_SERVICE=PROPRIO)

    assert resultado.returncode == 0, resultado.stderr
    assert amb.saidas()["sha_billing_service"] == fixo
    assert not any("billing-service" in c for c in amb.chamadas())
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
    ["SHA_PLATFORM", "SHA_BILLING_SERVICE", "SHA_EXECUTION_SERVICE", "SHA_OS_SERVICE"],
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

    resultado = amb.roda(SHA_OS_SERVICE="b" * 40)

    assert resultado.returncode == 1
    assert "sha_os_service must be empty or the commit of this run" in resultado.stdout
    assert amb.chamadas() == []


def test_falha_da_api_que_nao_e_404_reprova_em_vez_de_cair_na_main(
    amb: _Ambiente,
) -> None:
    amb.todos_verdes()
    amb.falha_da_api("billing-service", "gh: API rate limit exceeded (HTTP 403)")

    resultado = amb.roda()

    assert resultado.returncode != 0
    assert "HTTP 403" in resultado.stderr
    assert "::warning::" not in resultado.stdout
    assert amb.saida.read_text() == ""


def test_resposta_que_nao_e_um_commit_reprova(amb: _Ambiente) -> None:
    amb.todos_verdes()
    amb.corridas("execution-service", "nao-e-um-sha")

    resultado = amb.roda()

    assert resultado.returncode == 1
    assert (
        "could not resolve a commit for postech-sw-arch-p4-execution-service"
        in resultado.stdout
    )
    assert amb.saida.read_text() == ""
