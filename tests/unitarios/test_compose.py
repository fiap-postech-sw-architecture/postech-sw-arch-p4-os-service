"""Stack local (docker-compose.yml): o que os servicos precisam ter em comum."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador

COMPOSE = Path(__file__).parents[2] / "docker-compose.yml"
# O temporario do container: TMPDIR nao e definido na imagem.
_TMP_DO_CONTAINER = Path("/tmp")  # noqa: S108  # o /tmp do container, nao deste processo


def _servicos() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_prazo_de_resposta_da_saga_e_o_mesmo_na_api_no_consumidor_e_no_prazos() -> None:
    # A API grava o prazo da primeira compensacao, o consumidor o das seguintes e
    # o `prazos` mede todos: um ponto so no compose, para nao gravarem prazos
    # diferentes para a saga.
    servicos = _servicos()

    prazos = {
        nome: servicos[nome]["environment"]["SAGA_PRAZO_RESPOSTA_SEGUNDOS"]
        for nome in ("api", "consumidor", "prazos")
    }

    assert set(prazos.values()) == {"${SAGA_PRAZO_RESPOSTA_SEGUNDOS:-120}"}
    definicoes = re.findall(
        r"^\s+SAGA_PRAZO_RESPOSTA_SEGUNDOS:", COMPOSE.read_text(), re.MULTILINE
    )
    assert len(definicoes) == 1


def test_prazos_roda_o_modulo_da_imagem_com_a_sonda_de_120_segundos() -> None:
    servicos = _servicos()
    prazos = servicos["prazos"]

    assert prazos["command"] == ["python", "-m", "src.prazos"]
    assert prazos["image"] == servicos["relay"]["image"]
    assert prazos["depends_on"]["rabbitmq-usuarios"] == {"condition": "service_healthy"}
    # O pronto e o heartbeat dele; a liveness tolera 120 s, como o manifesto.
    sonda = prazos["healthcheck"]["test"][-1]
    sinal = Sinalizador("prazos", _TMP_DO_CONTAINER)
    assert str(sinal.pronto) in sonda
    assert str(sinal.heartbeat) in sonda
    assert "< 120" in sonda
    ambiente = prazos["environment"]
    assert ambiente["SAGA_MAX_REENVIOS"] == "${SAGA_MAX_REENVIOS:-5}"
    assert ambiente["PRAZOS_INTERVALO_SEGUNDOS"] == "${PRAZOS_INTERVALO_SEGUNDOS:-30}"
    # Sem a chave de cifragem: o `prazos` nao decifra dado de cliente.
    assert not {"ENCRYPTION_KEY", "JWT_PRIVATE_KEY"} & ambiente.keys()


def test_notificador_so_enxerga_o_banco_e_o_smtp() -> None:
    # Sem broker e sem a chave de cifragem: o contato do e-mail sai em claro, por
    # projecao de duas colunas, e quem so envia e-mail nao decifra o documento.
    ambiente = _servicos()["notificador"]["environment"]

    assert ambiente["SMTP_HOST"] == "mailpit"
    assert ambiente["SMTP_PORT"] == "1025"
    assert not {"RABBITMQ_URL", "ENCRYPTION_KEY", "JWT_PRIVATE_KEY"} & ambiente.keys()


def test_notificador_roda_o_modulo_da_imagem_e_espera_o_mailpit() -> None:
    servicos = _servicos()
    notificador = servicos["notificador"]

    assert notificador["command"] == ["python", "-m", "src.notificador"]
    assert notificador["image"] == servicos["relay"]["image"]
    assert notificador["depends_on"]["mailpit"] == {"condition": "service_healthy"}
    # Mesma sonda dos outros processos: o pronto e o heartbeat dele.
    sonda = notificador["healthcheck"]["test"][-1]
    sinal = Sinalizador("notificador", _TMP_DO_CONTAINER)
    assert str(sinal.pronto) in sonda
    assert str(sinal.heartbeat) in sonda


def test_mailpit_usa_a_imagem_do_platform_e_publica_so_no_loopback() -> None:
    mailpit = _servicos()["mailpit"]

    assert mailpit["image"] == "axllent/mailpit:v1.31.4"
    # 1026 e 8026, e nao 1025 e 8025 do compose do platform: os dois convivem.
    assert mailpit["ports"] == [
        "127.0.0.1:${MAILPIT_SMTP_PORT:-1026}:1025",
        "127.0.0.1:${MAILPIT_UI_PORT:-8026}:8025",
    ]


def test_mailpit_e_os_processos_rodam_endurecidos() -> None:
    # A caixa de entrada guarda os links de decisao e de pagamento e a UI nao pede
    # login: o Mailpit tem o mesmo endurecimento dos processos, como nobody (a
    # imagem nao define usuario) e com o /tmp, onde ele guarda o banco, em tmpfs.
    servicos = _servicos()

    for nome in ("relay", "consumidor", "prazos", "notificador", "mailpit"):
        servico = servicos[nome]
        assert servico["read_only"] is True, nome
        assert servico["tmpfs"] == ["/tmp"], nome  # noqa: S108
        assert servico["cap_drop"] == ["ALL"], nome
        assert servico["security_opt"] == ["no-new-privileges:true"], nome
        assert {"mem_limit", "cpus", "pids_limit"} <= servico.keys(), nome
    assert servicos["mailpit"]["user"] == "65534:65534"


def test_api_nao_tem_flag_para_o_metrics() -> None:
    # O /metrics da API esta sempre ligado: com a flag desligada (o padrao de
    # antes) o alvo respondia 404 e o alerta de servico fora do ar disparava.
    ambientes = [servico.get("environment", {}) for servico in _servicos().values()]

    assert not [a for a in ambientes if "API_METRICS_ENABLED" in a]


def test_api_e_os_processos_com_span_exportam_com_o_mesmo_prazo() -> None:
    # Com o Jaeger fora do ar e a fila cheia, o SDK espera o exportador em cada
    # lote: o prazo padrao do OTLP (10 s) fez a API levar 30,5 s para sair, mais
    # que o grace period do pod; com 2 s, 8,1 s (medido no container).
    servicos = _servicos()

    prazos = {
        (
            servicos[nome]["environment"]["OTEL_BSP_EXPORT_TIMEOUT"],
            servicos[nome]["environment"]["OTEL_EXPORTER_OTLP_TIMEOUT"],
            servicos[nome]["environment"]["OTEL_ENABLED"],
        )
        for nome in ("api", "relay", "consumidor", "prazos")
    }

    assert prazos == {("2000", "2", "${OTEL_ENABLED:-false}")}
