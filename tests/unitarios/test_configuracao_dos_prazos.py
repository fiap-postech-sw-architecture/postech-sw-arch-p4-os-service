"""A janela de reenvio e maior que a de retry do consumidor (RFC-004 secao 4.6).

O prazo de resposta de 120 s e os cinco reenvios dao ao participante 720 s antes
de a saga desistir do passo; o retry do consumidor, com os atrasos de 1, 5, 15, 60
e 300 s, termina em 381 s. Se a soma dos atrasos passasse da janela, o OS
compensaria um passo cuja resposta ainda esta no retry. Os atrasos vem da topologia
do platform (``contratos/rabbitmq/definitions.json``, o TTL de cada fila de retry),
e os prazos, dos padroes do codigo e do que o compose e o Kubernetes configuram.
"""

from __future__ import annotations

import json
import re

import pytest

from src.compartilhado.infraestrutura.mensageria.consumidor import NIVEIS_DE_RETRY
from src.compartilhado.infraestrutura.mensageria.contratos import CONTRATOS, FILA
from src.ordem_servico.infraestrutura.prazo_da_saga import (
    INTERVALO_DOS_PRAZOS_PADRAO_S,
    MAX_REENVIOS_PADRAO,
    PRAZO_RESPOSTA_PADRAO_S,
)
from tests.manifestos import OVERLAYS, RAIZ, literais_do_servico


def _atrasos_do_broker_s() -> list[float]:
    """O TTL de cada fila ``os.eventos.retry.*`` do platform, em segundos."""
    filas = json.loads((CONTRATOS / "rabbitmq/definitions.json").read_text())["queues"]
    return sorted(
        fila["arguments"]["x-message-ttl"] / 1000
        for fila in filas
        if fila["name"].startswith(f"{FILA}.retry.")
    )


def _janela_s(prazo_s: float, reenvios: int) -> float:
    """O que o participante tem antes de a saga desistir: o original e os reenvios."""
    return (reenvios + 1) * prazo_s


def test_atrasos_de_retry_do_broker_e_do_consumidor_sao_os_mesmos() -> None:
    do_codigo = sorted(float(nivel.removesuffix("s")) for nivel in NIVEIS_DE_RETRY)

    assert _atrasos_do_broker_s() == do_codigo == [1, 5, 15, 60, 300]


def test_soma_dos_atrasos_de_retry_e_menor_que_a_janela_de_reenvio() -> None:
    soma = sum(_atrasos_do_broker_s())
    janela = _janela_s(PRAZO_RESPOSTA_PADRAO_S, MAX_REENVIOS_PADRAO)

    assert (soma, janela) == (381, 720)
    assert soma < janela


def test_o_reenvio_do_ultimo_nivel_ainda_cabe_antes_de_desistir() -> None:
    # Com os atrasos nominais, o retry do comando original (381 s) termina antes de
    # o quinto reenvio vencer (600 s) e de a saga desistir (720 s).
    assert sum(_atrasos_do_broker_s()) < MAX_REENVIOS_PADRAO * PRAZO_RESPOSTA_PADRAO_S


def test_o_intervalo_padrao_do_ciclo_e_bem_menor_que_o_prazo_de_resposta() -> None:
    # O alerta "Saga parada" do platform usa 60 s, duas vezes o intervalo: um
    # ciclo perdido nao o dispara. O limiar mora no platform; aqui fica o que e
    # do OS, o intervalo de 30 s dentro do prazo de 120 s.
    assert INTERVALO_DOS_PRAZOS_PADRAO_S == 30
    assert INTERVALO_DOS_PRAZOS_PADRAO_S < PRAZO_RESPOSTA_PADRAO_S


def test_o_compose_configura_o_prazo_de_resposta_do_padrao() -> None:
    compose = (RAIZ / "docker-compose.yml").read_text()

    assert re.search(
        rf"SAGA_PRAZO_RESPOSTA_SEGUNDOS: \$\{{SAGA_PRAZO_RESPOSTA_SEGUNDOS:-"
        rf"{PRAZO_RESPOSTA_PADRAO_S}\}}",
        compose,
    )
    assert re.search(
        rf"SAGA_MAX_REENVIOS: \$\{{SAGA_MAX_REENVIOS:-{MAX_REENVIOS_PADRAO}\}}",
        compose,
    )
    assert re.search(
        r"PRAZOS_INTERVALO_SEGUNDOS: \$\{PRAZOS_INTERVALO_SEGUNDOS:-"
        rf"{int(INTERVALO_DOS_PRAZOS_PADRAO_S)}\}}",
        compose,
    )


@pytest.mark.parametrize("overlay", [None, *OVERLAYS])
def test_o_kubernetes_configura_prazos_que_deixam_o_retry_caber_na_janela(
    overlay: str | None,
) -> None:
    caminho = RAIZ / "k8s" / ("base" if overlay is None else f"overlays/{overlay}")
    literais = literais_do_servico(caminho / "kustomization.yaml")
    base = literais_do_servico()
    prazo = float(
        literais.get(
            "SAGA_PRAZO_RESPOSTA_SEGUNDOS", base["SAGA_PRAZO_RESPOSTA_SEGUNDOS"]
        )
    )
    reenvios = int(literais.get("SAGA_MAX_REENVIOS", base["SAGA_MAX_REENVIOS"]))

    assert sum(_atrasos_do_broker_s()) < _janela_s(prazo, reenvios)


def test_o_env_example_traz_os_tres_parametros_com_os_padroes() -> None:
    exemplo = (RAIZ / ".env.example").read_text()

    for nome, padrao in (
        ("SAGA_PRAZO_RESPOSTA_SEGUNDOS", PRAZO_RESPOSTA_PADRAO_S),
        ("SAGA_MAX_REENVIOS", MAX_REENVIOS_PADRAO),
        ("PRAZOS_INTERVALO_SEGUNDOS", int(INTERVALO_DOS_PRAZOS_PADRAO_S)),
    ):
        assert f"\n{nome}={padrao}\n" in exemplo
