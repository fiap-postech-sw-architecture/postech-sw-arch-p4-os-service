"""O que os manifestos de ``k8s/`` prometem, lido do render dos tres overlays.

O ``make manifests`` so confere o schema e os achados do trivy; estes testes
prendem o que o servico combinou com o resto do sistema (ADR-042): qual papel do
banco e qual chave de Secret cada processo usa, o ``restricted`` em todo
container, a NetworkPolicy e a configuracao de cada overlay. Sem cluster; o
``kubectl kustomize`` e o mesmo do ``make manifests``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.manifestos import (
    OVERLAYS,
    RAIZ,
    containers,
    do_render,
    do_render_por_tipo,
    expandir_ambiente,
    literais_do_servico,
    pods,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# Todos os testes deste modulo leem o render (kubectl kustomize).
pytestmark = pytest.mark.usefixtures("kubectl")

_BANCO = "os-postgres.pytstop-os.svc.cluster.local:5432/os"
_API = ("Deployment", "os-service-api", "api")
_RELAY = ("Deployment", "os-service-relay", "relay")
_CONSUMIDOR = ("Deployment", "os-service-consumidor", "consumidor")
_ESPERA_DA_API = ("Deployment", "os-service-api", "aguarda-migracao")
_ESPERA_DO_RELAY = ("Deployment", "os-service-relay", "aguarda-migracao")
_ESPERA_DO_CONSUMIDOR = ("Deployment", "os-service-consumidor", "aguarda-migracao")
_BANCO_DO_POD = ("StatefulSet", "os-postgres", "postgres")
_EXPORTER = ("StatefulSet", "os-postgres", "exporter")
_ESPERA_DO_BANCO = ("Job", "os-migracao", "aguarda-postgres")
_MIGRACAO = ("Job", "os-migracao", "migracao")

# Cada Secret e cada chave que um container le: os nomes e as chaves que o
# make deploy do platform grava ("Segredos gerados" do README dele). A chave
# POSTGRES_PASSWORD (o superusuario) so chega ao container do banco, e a
# ENCRYPTION_KEY so a API (a unica que decifra o documento do cliente).
_APP = {"POSTGRES_APP_PASSWORD": ("os-postgres", "POSTGRES_APP_PASSWORD")}
_RABBITMQ = {"RABBITMQ_URL": ("rabbitmq", "RABBITMQ_URL")}
SEGREDOS_POR_CONTAINER = {
    _ESPERA_DA_API: _APP,
    _API: {
        **_APP,
        "JWT_PRIVATE_KEY": ("os-jwt", "JWT_PRIVATE_KEY"),
        "JWT_PREVIOUS_PUBLIC_KEY": ("os-jwt", "JWT_PREVIOUS_PUBLIC_KEY"),
        "ENCRYPTION_KEY": ("os-cripto", "ENCRYPTION_KEY"),
    },
    _ESPERA_DO_RELAY: _APP,
    _RELAY: {**_APP, **_RABBITMQ},
    _ESPERA_DO_CONSUMIDOR: _APP,
    _CONSUMIDOR: {**_APP, **_RABBITMQ},
    _BANCO_DO_POD: {
        chave: ("os-postgres", chave)
        for chave in (
            "POSTGRES_PASSWORD",
            "POSTGRES_OWNER_PASSWORD",
            "POSTGRES_APP_PASSWORD",
            "POSTGRES_EXPORTER_PASSWORD",
        )
    },
    _EXPORTER: {"DATA_SOURCE_PASS": ("os-postgres", "POSTGRES_EXPORTER_PASSWORD")},
    _ESPERA_DO_BANCO: {},
    _MIGRACAO: {
        "POSTGRES_OWNER_PASSWORD": ("os-postgres", "POSTGRES_OWNER_PASSWORD"),
        "ADMIN_PASSWORD": ("os-admin", "ADMIN_PASSWORD"),
    },
}

# A DATABASE_URL de cada container que fala com o banco, com a senha escrita
# como <Secret/chave>: o papel e a chave da senha de cada processo (D87). A API,
# o relay, o consumidor e a espera da migracao usam o papel da aplicacao; so o
# Job de migracao usa o dono.
_URL_DA_APLICACAO = f"postgresql://os_app:<os-postgres/POSTGRES_APP_PASSWORD>@{_BANCO}"
_URL_DO_DONO = f"postgresql://os:<os-postgres/POSTGRES_OWNER_PASSWORD>@{_BANCO}"
URL_DO_BANCO_POR_CONTAINER = {
    _ESPERA_DA_API: _URL_DA_APLICACAO,
    _API: _URL_DA_APLICACAO,
    _ESPERA_DO_RELAY: _URL_DA_APLICACAO,
    _RELAY: _URL_DA_APLICACAO,
    _ESPERA_DO_CONSUMIDOR: _URL_DA_APLICACAO,
    _CONSUMIDOR: _URL_DA_APLICACAO,
    _MIGRACAO: _URL_DO_DONO,
}
# Os containers que recebem o ConfigMap do servico por inteiro.
COM_CONFIGMAP = {_API, _RELAY, _CONSUMIDOR, _MIGRACAO}


def _referencia(nome: str, chave: str) -> str:
    return f"<{nome}/{chave}>"


def _segredos_do_container(especificacao: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {
        variavel["name"]: (
            variavel["valueFrom"]["secretKeyRef"]["name"],
            variavel["valueFrom"]["secretKeyRef"]["key"],
        )
        for variavel in especificacao.get("env", [])
        if "valueFrom" in variavel
    }


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_cada_container_le_so_os_segredos_do_seu_processo(overlay: str) -> None:
    lidos = {
        chave: _segredos_do_container(especificacao)
        for chave, especificacao, _ in containers(overlay)
    }

    assert lidos == SEGREDOS_POR_CONTAINER


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_cada_processo_conecta_no_banco_com_o_papel_e_a_senha_do_seu_uso(
    overlay: str,
) -> None:
    urls = {}
    for chave, especificacao, _ in containers(overlay):
        ambiente = expandir_ambiente(especificacao, _referencia)
        if "DATABASE_URL" in ambiente:
            urls[chave] = ambiente["DATABASE_URL"]

    assert urls == URL_DO_BANCO_POR_CONTAINER


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_exporter_entra_no_banco_como_os_exporter_pelo_loopback(overlay: str) -> None:
    exporter = next(e for chave, e, _ in containers(overlay) if chave == _EXPORTER)

    ambiente = expandir_ambiente(exporter, _referencia)

    assert ambiente == {
        "DATA_SOURCE_URI": "127.0.0.1:5432/os?sslmode=disable",
        "DATA_SOURCE_USER": "os_exporter",
        "DATA_SOURCE_PASS": "<os-postgres/POSTGRES_EXPORTER_PASSWORD>",
    }


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_so_a_api_recebe_a_chave_de_cifragem(overlay: str) -> None:
    # D94: so a API decifra o documento do cliente; relay e consumidor leem o
    # contato do e-mail por projecao SQL e nao recebem a chave.
    com_a_chave = {
        chave
        for chave, especificacao, _ in containers(overlay)
        if "ENCRYPTION_KEY" in _segredos_do_container(especificacao)
    }

    assert com_a_chave == {_API}


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_so_o_container_do_banco_recebe_a_senha_do_superusuario(overlay: str) -> None:
    com_a_senha = {
        chave
        for chave, especificacao, _ in containers(overlay)
        if ("os-postgres", "POSTGRES_PASSWORD")
        in _segredos_do_container(especificacao).values()
    }

    assert com_a_senha == {_BANCO_DO_POD}


# As variaveis com `value` literal. Nenhuma guarda segredo: as senhas vem de um
# secretKeyRef, e a DATABASE_URL as recebe por $(VAR). Uma variavel literal nova
# com nome de segredo (TOKEN, SENHA) passaria no gitleaks, porque o YAML separa
# `name` de `value`: entra nesta lista so depois de revisada.
ENV_LITERAL = frozenset(
    {
        "DATABASE_URL",
        "DATA_SOURCE_URI",
        "DATA_SOURCE_USER",
        "PGDATA",
        "POSTGRES_DB",
        "POSTGRES_INITDB_ARGS",
        "POSTGRES_USER",
    }
)


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_variavel_com_valor_literal_e_so_a_que_nao_guarda_segredo(overlay: str) -> None:
    literais = {
        variavel["name"]
        for _, especificacao, _ in containers(overlay)
        for variavel in especificacao.get("env", [])
        if "value" in variavel
    }

    assert literais == ENV_LITERAL


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_configmap_do_servico_so_chega_aos_processos_que_o_leem(overlay: str) -> None:
    existentes = {
        c["metadata"]["name"] for c in do_render_por_tipo(overlay, "ConfigMap")
    }
    referenciados = {
        chave: [origem["configMapRef"]["name"] for origem in especificacao["envFrom"]]
        for chave, especificacao, _ in containers(overlay)
        if "envFrom" in especificacao
    }

    assert set(referenciados) == COM_CONFIGMAP
    for nomes in referenciados.values():
        assert len(nomes) == 1
        assert nomes[0] in existentes
        assert nomes[0].startswith("os-service-")


def _efetivo(campo: str, especificacao: dict[str, Any], pod: dict[str, Any]) -> Any:
    """O valor do container, ou o do pod quando o container nao o define."""
    contexto = especificacao.get("securityContext", {})
    if campo in contexto:
        return contexto[campo]
    return pod.get("securityContext", {}).get(campo)


def _violacoes_do_restricted(
    chave: tuple[str, str, str], especificacao: dict[str, Any], pod: dict[str, Any]
) -> Iterator[str]:
    contexto = especificacao.get("securityContext", {})
    onde = "/".join(chave)
    if contexto.get("allowPrivilegeEscalation") is not False:
        yield f"{onde}: allowPrivilegeEscalation deve ser false"
    if contexto.get("capabilities") != {"drop": ["ALL"]}:
        yield f"{onde}: capabilities deve ser drop [ALL]"
    if contexto.get("readOnlyRootFilesystem") is not True:
        yield f"{onde}: readOnlyRootFilesystem deve ser true"
    if contexto.get("privileged"):
        yield f"{onde}: privileged"
    if _efetivo("runAsNonRoot", especificacao, pod) is not True:
        yield f"{onde}: runAsNonRoot deve ser true"
    usuario = _efetivo("runAsUser", especificacao, pod)
    if not isinstance(usuario, int) or usuario == 0:
        yield f"{onde}: runAsUser deve ser um uid numerico diferente de 0"
    seccomp = _efetivo("seccompProfile", especificacao, pod)
    if seccomp != {"type": "RuntimeDefault"}:
        yield f"{onde}: seccompProfile deve ser RuntimeDefault"
    for tipo in ("requests", "limits"):
        recursos = especificacao.get("resources", {}).get(tipo, {})
        if not {"cpu", "memory"} <= recursos.keys():
            yield f"{onde}: resources.{tipo} deve ter cpu e memory"


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_todo_container_cumpre_o_pod_security_restricted(overlay: str) -> None:
    violacoes = [
        violacao
        for chave, especificacao, pod in containers(overlay)
        for violacao in _violacoes_do_restricted(chave, especificacao, pod)
    ]

    assert violacoes == []


# Os tipos de volume que o perfil restricted permite e os pods daqui usam; o
# admission do cluster so recusaria um hostPath no deploy.
VOLUMES_DO_RESTRICTED = frozenset(
    {
        "configMap",
        "downwardAPI",
        "emptyDir",
        "persistentVolumeClaim",
        "projected",
        "secret",
    }
)


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_pod_so_declara_volume_de_tipo_que_o_restricted_permite(overlay: str) -> None:
    # Cada volume e {"name": ..., "<tipo>": {...}}.
    usados = {
        f"{tipo}/{nome}": {
            campo
            for volume in pod.get("volumes", [])
            for campo in volume.keys() - {"name"}
        }
        for tipo, nome, pod in pods(overlay)
    }

    fora = {
        onde: sorted(tipos - VOLUMES_DO_RESTRICTED)
        for onde, tipos in usados.items()
        if tipos - VOLUMES_DO_RESTRICTED
    }

    assert len(usados) == 5
    assert fora == {}


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_nenhum_pod_monta_o_token_da_service_account_nem_usa_a_rede_do_no(
    overlay: str,
) -> None:
    pods_do_servico = list(pods(overlay))
    sem_a_regra = [
        f"{tipo}/{nome}"
        for tipo, nome, pod in pods_do_servico
        if pod.get("automountServiceAccountToken") is not False
        or any(pod.get(campo) for campo in ("hostNetwork", "hostPID", "hostIPC"))
    ]

    assert len(pods_do_servico) == 5
    assert sem_a_regra == []


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_namespace_aplica_o_restricted_em_enforce_warn_e_audit(overlay: str) -> None:
    rotulos = do_render(overlay, "Namespace", "pytstop-os")["metadata"]["labels"]

    assert {
        modo: rotulos[f"pod-security.kubernetes.io/{modo}"]
        for modo in ("enforce", "warn", "audit")
    } == dict.fromkeys(("enforce", "warn", "audit"), "restricted")


def _container_do_render(overlay: str, chave: tuple[str, str, str]) -> dict[str, Any]:
    return next(e for c, e, _ in containers(overlay) if c == chave)


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_readiness_da_api_olha_a_prontidao_e_a_liveness_olha_so_o_processo(
    overlay: str,
) -> None:
    # A liveness na rota de prontidao reiniciaria a API a cada queda do banco.
    api = _container_do_render(overlay, _API)

    assert api["readinessProbe"]["httpGet"] == {
        "path": "/api/v1/saude/pronto",
        "port": "http",
    }
    for sonda in ("startupProbe", "livenessProbe"):
        assert api[sonda]["httpGet"] == {"path": "/api/v1/saude", "port": "http"}


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_service_interno_da_api_e_o_da_8000_que_billing_e_execucao_leem_no_jwks(
    overlay: str,
) -> None:
    # JWKS_URL deles: http://os-service.pytstop-os.svc.cluster.local:8000.
    servico = do_render(overlay, "Service", "os-service")["spec"]
    api = _container_do_render(overlay, _API)

    assert servico["selector"] == {"app": "os-service-api"}
    assert servico["ports"] == [{"name": "http", "port": 8000, "targetPort": "http"}]
    assert api["ports"] == [{"name": "http", "containerPort": 8000}]


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_prazo_de_encerramento_cobre_o_que_cada_processo_tem_em_curso(
    overlay: str,
) -> None:
    # Relay e consumidor: 45 s cobrem o publish que espera o confirm do broker (ate
    # 30 s com o broker em alarme) e a mensagem em curso, que o SIGTERM conclui.
    prazos = {
        nome: pod["terminationGracePeriodSeconds"]
        for tipo, nome, pod in pods(overlay)
        if tipo == "Deployment"
    }

    assert prazos == {
        "os-service-api": 30,
        "os-service-relay": 45,
        "os-service-consumidor": 45,
    }


def _rotulos_dos_templates(overlay: str) -> dict[str, dict[str, str]]:
    """Os rotulos do template de cada workload, pelo nome do objeto."""
    return {
        objeto["metadata"]["name"]: objeto["spec"]["template"]["metadata"]["labels"]
        for tipo in ("Deployment", "StatefulSet", "Job")
        for objeto in do_render_por_tipo(overlay, tipo)
    }


def _portas_dos_workloads(overlay: str) -> dict[str, set[int]]:
    """Os ``containerPort`` de cada workload, pelo nome do objeto."""
    return {
        nome: {
            porta["containerPort"]
            for secao in ("initContainers", "containers")
            for especificacao in pod.get(secao, [])
            for porta in especificacao.get("ports", [])
        }
        for _, nome, pod in pods(overlay)
    }


def _casa(seletor: dict[str, Any], rotulos: dict[str, str]) -> bool:
    if not seletor.get("matchLabels", {}).items() <= rotulos.items():
        return False
    for expressao in seletor.get("matchExpressions", []):
        assert expressao["operator"] == "In"
        if rotulos.get(expressao["key"]) not in expressao["values"]:
            return False
    return True


def _alvos(overlay: str, politica: dict[str, Any]) -> set[str]:
    """Os workloads cujo template a politica seleciona."""
    seletor = politica["spec"]["podSelector"]
    return {
        nome
        for nome, rotulos in _rotulos_dos_templates(overlay).items()
        if _casa(seletor, rotulos)
    }


def _par(item: dict[str, Any]) -> tuple[str, str]:
    """(namespace, app) de um item ``from`` com os dois seletores no mesmo item."""
    assert item.keys() == {"namespaceSelector", "podSelector"}
    return (
        item["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
        item["podSelector"]["matchLabels"]["app"],
    )


_KONG = ("pytstop-plataforma", "kong")
_PROMETHEUS = ("pytstop-plataforma", "prometheus")


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_namespace_nega_toda_entrada_e_so_as_politicas_nomeadas_liberam(
    overlay: str,
) -> None:
    politicas = {
        p["metadata"]["name"]: p for p in do_render_por_tipo(overlay, "NetworkPolicy")
    }

    assert set(politicas) == {"nega-entrada", "api", "metricas-dos-processos", "banco"}
    nega = politicas["nega-entrada"]["spec"]
    assert nega == {"podSelector": {}, "policyTypes": ["Ingress"]}
    assert all(p["spec"]["policyTypes"] == ["Ingress"] for p in politicas.values())


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_politica_da_api_libera_so_kong_prometheus_e_as_apis_dos_outros_servicos(
    overlay: str,
) -> None:
    politica = do_render(overlay, "NetworkPolicy", "api")

    assert _alvos(overlay, politica) == {"os-service-api"}
    (regra,) = politica["spec"]["ingress"]
    # Namespace e pod no mesmo item (E): em itens separados seria OU, e qualquer
    # pod do namespace do Kong, ou qualquer pod chamado kong, passaria.
    assert [_par(item) for item in regra["from"]] == [
        _KONG,
        _PROMETHEUS,
        ("pytstop-billing", "billing-service-api"),
        ("pytstop-execucao", "execution-service-api"),
    ]
    assert regra["ports"] == [{"port": 8000, "protocol": "TCP"}]
    assert 8000 in _portas_dos_workloads(overlay)["os-service-api"]


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_politica_dos_processos_libera_so_o_prometheus_na_porta_das_metricas(
    overlay: str,
) -> None:
    politica = do_render(overlay, "NetworkPolicy", "metricas-dos-processos")

    assert _alvos(overlay, politica) == {"os-service-relay", "os-service-consumidor"}
    (regra,) = politica["spec"]["ingress"]
    assert [_par(item) for item in regra["from"]] == [_PROMETHEUS]
    assert regra["ports"] == [{"port": 9100, "protocol": "TCP"}]
    portas = _portas_dos_workloads(overlay)
    assert 9100 in portas["os-service-relay"]
    assert 9100 in portas["os-service-consumidor"]


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_politica_do_banco_libera_o_namespace_no_5432_e_so_o_prometheus_no_exporter(
    overlay: str,
) -> None:
    politica = do_render(overlay, "NetworkPolicy", "banco")

    assert _alvos(overlay, politica) == {"os-postgres"}
    banco, metricas = politica["spec"]["ingress"]
    # Os pods do proprio namespace: sem namespaceSelector, que de {} liberaria
    # qualquer pod de qualquer namespace.
    assert banco == {
        "from": [{"podSelector": {}}],
        "ports": [{"port": 5432, "protocol": "TCP"}],
    }
    assert [_par(item) for item in metricas["from"]] == [_PROMETHEUS]
    assert metricas["ports"] == [{"port": 9187, "protocol": "TCP"}]
    assert {5432, 9187} <= _portas_dos_workloads(overlay)["os-postgres"]


# O CIDR de pods de cada cluster: o Kong chega a API com o IP de um pod dele, e
# o X-Forwarded-For que ele manda vale como IP do cliente so se vier desta rede.
CIDR_DE_PODS = {
    "kind": "10.244.0.0/16",
    "kind-ci": "10.244.0.0/16",
    "k3s": "10.42.0.0/16",
}


def _configmap_do_servico(overlay: str) -> dict[str, str]:
    (configmap,) = (
        c
        for c in do_render_por_tipo(overlay, "ConfigMap")
        if c["metadata"]["name"].startswith("os-service-")
    )
    return dict(configmap["data"])


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_confianca_no_x_forwarded_for_e_so_do_cidr_de_pods_do_cluster(
    overlay: str,
) -> None:
    configuracao = _configmap_do_servico(overlay)

    assert configuracao["TRUSTED_PROXIES"] == CIDR_DE_PODS[overlay]
    assert "*" not in configuracao["TRUSTED_PROXIES"]


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_root_path_e_o_prefixo_que_a_borda_roteia_e_producao_nao_cai_em_dev(
    overlay: str,
) -> None:
    configuracao = _configmap_do_servico(overlay)
    prefixos = {
        "/" + caminho["path"].split("/")[1]
        for ingress in do_render_por_tipo(overlay, "Ingress")
        for regra in ingress["spec"]["rules"]
        for caminho in regra["http"]["paths"]
    }

    assert prefixos == {"/os"}
    assert configuracao["ROOT_PATH"] == "/os"
    assert configuracao["ENVIRONMENT"] == "production"


@pytest.mark.parametrize(
    ("overlay", "maximo_da_api", "disco_do_banco"),
    [
        pytest.param("kind", 2, "1Gi", id="kind"),
        pytest.param("kind-ci", 1, "1Gi", id="kind-ci"),
        pytest.param("k3s", 3, "5Gi", id="k3s"),
    ],
)
def test_overlay_dimensiona_a_api_e_o_disco_do_banco(
    overlay: str, maximo_da_api: int, disco_do_banco: str
) -> None:
    hpa = do_render(overlay, "HorizontalPodAutoscaler", "os-service-api")
    banco = do_render(overlay, "StatefulSet", "os-postgres")

    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (
        1,
        maximo_da_api,
    )
    (volume,) = banco["spec"]["volumeClaimTemplates"]
    assert volume["spec"]["resources"]["requests"]["storage"] == disco_do_banco


def test_toda_variavel_do_configmap_e_lida_pelo_codigo() -> None:
    # Uma variavel renomeada no codigo passa no make manifests (o kubeconform so
    # confere o schema) e so cairia no deploy.
    literais = literais_do_servico()
    for overlay in OVERLAYS:
        literais |= literais_do_servico(
            RAIZ / "k8s/overlays" / overlay / "kustomization.yaml"
        )
    codigo = "\n".join(
        arquivo.read_text()
        for pasta in ("src", "scripts")
        for arquivo in (RAIZ / pasta).rglob("*.py")
    )
    entrypoint = (RAIZ / "entrypoint.sh").read_text()

    sem_leitor = [
        variavel
        for variavel in literais
        if f'"{variavel}"' not in codigo and "${" + variavel not in entrypoint
    ]

    assert len(literais) > 19
    assert sem_leitor == []
