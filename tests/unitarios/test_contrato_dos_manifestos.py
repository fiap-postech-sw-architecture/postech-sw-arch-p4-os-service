"""O que os manifestos de ``k8s/`` prometem, lido do render dos tres overlays.

O ``make manifests`` so confere o schema e os achados do trivy; estes testes
prendem o que o servico combinou com o resto do sistema (ADR-042): qual papel do
banco e qual chave de Secret cada processo usa, o ``restricted`` em todo
container, a NetworkPolicy e a configuracao de cada overlay. Sem cluster; o
``kubectl kustomize`` e o mesmo do ``make manifests``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.manifestos import (
    OVERLAYS,
    containers,
    do_render_por_tipo,
    expandir_ambiente,
)

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
