"""O pod do PostgreSQL de ``k8s/base`` na imagem real (testcontainers).

O banco sobe com a imagem, o ambiente, o ``papeis.sql`` e o securityContext do
StatefulSet; as senhas, que no cluster vem do Secret ``os-postgres``, sao geradas
aqui. Cada processo se conecta com a ``DATABASE_URL`` do proprio manifesto,
expandida como o kubelet a expande: quem decide o papel e a senha de cada um e o
manifesto, nao o teste.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.manifestos import (
    BASE,
    RAIZ,
    comando,
    container,
    expandir_ambiente,
    literais_do_servico,
    objeto,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from testcontainers.community.postgres import PostgresContainer

# Todo log do servidor que leva o texto do comando ligado, o pior caso para o
# script de init (a imagem passa os argumentos tambem ao servidor temporario em
# que ele roda): por comando, por duracao e por amostragem.
LOG_DE_TUDO = (
    "postgres -c log_statement=all -c log_min_duration_statement=0 "
    "-c log_min_duration_sample=0 -c log_statement_sample_rate=1 "
    "-c log_transaction_sample_rate=1"
)
_BANCO_NO_CLUSTER = "os-postgres.pytstop-os.svc.cluster.local:5432"


@dataclass(frozen=True)
class Banco:
    container: PostgresContainer
    senhas: dict[str, str]

    @property
    def endereco(self) -> str:
        host = self.container.get_container_host_ip()
        return f"{host}:{self.container.get_exposed_port(5432)}"

    def url_do_superusuario(self) -> str:
        return f"postgresql://postgres:{self.senhas['POSTGRES_PASSWORD']}@{self.endereco}/os"

    def ambiente_do(
        self, tipo: str, nome: str, nome_do_container: str
    ) -> dict[str, str]:
        """O ``env`` de um container do manifesto, com o banco deste container.

        A senha de cada ``secretKeyRef`` e a gerada para a chave; a
        ``DATABASE_URL`` troca o endereco do Service pelo do container de teste.
        """
        especificacao = container(objeto(tipo, nome), nome_do_container)
        ambiente = expandir_ambiente(
            especificacao, lambda _secret, chave: self.senhas.get(chave, "")
        )
        if "DATABASE_URL" in ambiente:
            url = ambiente["DATABASE_URL"]
            assert _BANCO_NO_CLUSTER in url, url
            ambiente["DATABASE_URL"] = url.replace(_BANCO_NO_CLUSTER, self.endereco)
        return ambiente

    def url_do(self, tipo: str, nome: str, nome_do_container: str) -> str:
        """A ``DATABASE_URL`` de um container do manifesto."""
        return self.ambiente_do(tipo, nome, nome_do_container)["DATABASE_URL"]

    def url_do_exporter(self) -> str:
        """A conexao do sidecar, pelos ``DATA_SOURCE_*`` do manifesto."""
        ambiente = self.ambiente_do("StatefulSet", "os-postgres", "exporter")
        _, _, banco = ambiente["DATA_SOURCE_URI"].partition("/")
        banco = banco.partition("?")[0]
        return (
            f"postgresql://{ambiente['DATA_SOURCE_USER']}:"
            f"{ambiente['DATA_SOURCE_PASS']}@{self.endereco}/{banco}"
        )

    def no_container(self, *comando: str, senha: str | None = None) -> Any:
        """Roda ``comando`` dentro do container do banco (resultado do exec)."""
        ambiente = {"PGPASSWORD": senha} if senha else {}
        return self.container.get_wrapped_container().exec_run(
            list(comando), environment=ambiente
        )

    def log_do_servidor(self) -> str:
        return str(self.container.get_wrapped_container().logs().decode())


def no_host(argumentos: list[str], ambiente: dict[str, str], prazo_s: float) -> int:
    """Roda o comando de um manifesto aqui, com o alembic e o python do venv."""
    caminho = f"{Path(sys.executable).parent}:{os.environ['PATH']}"
    # Argumentos fixos: o comando de um manifesto do repositorio.
    return subprocess.run(  # noqa: S603
        argumentos,
        env={"PATH": caminho, **ambiente},
        cwd=RAIZ,
        capture_output=True,
        timeout=prazo_s,
        check=False,
    ).returncode


def _container_do_banco(senhas: dict[str, str]) -> PostgresContainer:
    from testcontainers.community.postgres import PostgresContainer

    postgres = container(objeto("StatefulSet", "os-postgres"), "postgres")
    ambiente = {
        item["name"]: item["value"] for item in postgres["env"] if "value" in item
    }
    seguranca = postgres["securityContext"]
    assert seguranca["readOnlyRootFilesystem"] is True
    assert seguranca["capabilities"] == {"drop": ["ALL"]}
    pod = objeto("StatefulSet", "os-postgres")["spec"]["template"]["spec"]
    usuario = (
        f"{pod['securityContext']['runAsUser']}:{pod['securityContext']['runAsGroup']}"
    )
    # O pod do manifesto: usuario 999, raiz somente leitura com os emptyDir do
    # socket e do /tmp, sem capability; o volume de dados e o da imagem.
    banco_do_pod = (
        PostgresContainer(
            postgres["image"],
            username=ambiente["POSTGRES_USER"],
            password=senhas["POSTGRES_PASSWORD"],
            dbname=ambiente["POSTGRES_DB"],
        )
        .with_command(LOG_DE_TUDO)
        .with_volume_mapping(
            str(BASE / "papeis.sql"), "/docker-entrypoint-initdb.d/papeis.sql", "ro"
        )
        .with_tmpfs_mount("/var/run/postgresql")
        .with_tmpfs_mount("/tmp")  # noqa: S108  # o /tmp do container
        .with_kwargs(
            user=usuario,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )
    )
    for nome, valor in {**ambiente, **senhas}.items():
        banco_do_pod.with_env(nome, valor)
    return banco_do_pod


def _senhas_do_secret() -> dict[str, str]:
    """Uma senha gerada por chave do Secret que o container do banco le."""
    postgres = container(objeto("StatefulSet", "os-postgres"), "postgres")
    # As senhas vem do Secret os-postgres, uma variavel por chave, com o mesmo nome.
    do_secret = {
        item["name"]: item["valueFrom"]["secretKeyRef"]
        for item in postgres["env"]
        if "valueFrom" in item
    }
    assert do_secret == {
        chave: {"name": "os-postgres", "key": chave}
        for chave in (
            "POSTGRES_PASSWORD",
            "POSTGRES_OWNER_PASSWORD",
            "POSTGRES_APP_PASSWORD",
            "POSTGRES_EXPORTER_PASSWORD",
        )
    }
    return {chave: secrets.token_hex(24) for chave in [*do_secret, "ADMIN_PASSWORD"]}


@contextmanager
def subir_banco(*, migrar: bool) -> Iterator[Banco]:
    """O banco do StatefulSet; com ``migrar``, ja migrado e semeado pelo Job."""
    senhas = _senhas_do_secret()
    with _container_do_banco(senhas) as banco_do_pod:
        banco = Banco(banco_do_pod, senhas)
        if migrar:
            _rodar_o_job(banco)
        yield banco


def _rodar_o_job(banco: Banco) -> None:
    """O comando do Job de migracao, com o ``env`` dele, como o dono do banco."""
    job = objeto("Job", "os-migracao")
    # ENVIRONMENT e ADMIN_EMAIL vem do ConfigMap do servico (envFrom).
    configmap = literais_do_servico()
    ambiente = {
        "ENVIRONMENT": configmap["ENVIRONMENT"],
        "ADMIN_EMAIL": configmap["ADMIN_EMAIL"],
        **banco.ambiente_do("Job", "os-migracao", "migracao"),
    }
    migracao = ["/bin/sh", "-c", comando(job, "migracao")]
    # Duas vezes: o Job roda a cada implantacao e tem de ser idempotente.
    assert no_host(migracao, ambiente, prazo_s=120) == 0
    assert no_host(migracao, ambiente, prazo_s=120) == 0
