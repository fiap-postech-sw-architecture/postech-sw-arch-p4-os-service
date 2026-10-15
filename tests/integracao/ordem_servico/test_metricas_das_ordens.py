"""Metricas das OS contra o PostgreSQL: a leitura, a rota e os gauges (RF-027).

A permanencia de cada status sai do historico (``para`` e ``ocorrido_em`` ate a
linha seguinte da mesma OS), nas entradas dos ultimos 30 dias. A leitura, a rota
``GET /api/v1/ordens-de-servico/metricas`` e os gauges do coletor da API sao o
mesmo numero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from prometheus_client import CollectorRegistry
from sqlalchemy import text

from src.autenticacao.dominio.papel import Papel
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.coletor_da_api import ColetorDaApi
from src.ordem_servico.infraestrutura.consultas import (
    JANELA_DAS_PERMANENCIAS,
    ler_metricas_das_ordens,
)
from tests.integracao.seed_helpers import (
    SENHA_PADRAO,
    criar_ordens_com_historicos,
    criar_usuario,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.autenticacao.dominio.usuario import Usuario
    from tests.integracao.seed_helpers import Passos

AGORA = datetime(2026, 10, 14, 12, tzinfo=UTC)
_METRICAS = "/api/v1/ordens-de-servico/metricas"


def _s(segundos: float) -> timedelta:
    return timedelta(seconds=segundos)


def _fixture_conhecida(agora: datetime) -> list[Passos]:
    """Tres OS: duas dentro da janela e uma com entradas dentro e fora dela."""
    a = [
        ("recebida", agora - _s(5000)),
        ("em_diagnostico", agora - _s(4900)),
        ("aguardando_aprovacao", agora - _s(4600)),
        ("cancelada", agora - _s(4000)),
    ]
    b = [("recebida", agora - _s(3000)), ("em_diagnostico", agora - _s(2800))]
    c = [
        # Entrada fora da janela: a permanencia dela nao entra.
        ("recebida", agora - JANELA_DAS_PERMANENCIAS - _s(3600)),
        ("em_diagnostico", agora - JANELA_DAS_PERMANENCIAS + _s(3600)),
        ("aguardando_aprovacao", agora - timedelta(days=29)),
    ]
    return [a, b, c]


class TestLeitura:
    def test_total_por_status_e_permanencia_media(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        criar_ordens_com_historicos(session_factory, *_fixture_conhecida(AGORA))

        with session_factory() as sess:
            metricas = ler_metricas_das_ordens(sess, AGORA)

        assert metricas.total == 3
        assert dict(metricas.por_status) == {
            "recebida": 0,
            "em_diagnostico": 1,
            "aguardando_aprovacao": 1,
            "aguardando_pagamento": 0,
            "aguardando_execucao": 0,
            "em_execucao": 0,
            "finalizada": 0,
            "entregue": 0,
            "cancelada": 1,
        }
        # recebida: A 100 s e B 200 s (a de C esta fora da janela); em_diagnostico:
        # A 300 s e C 23 h; aguardando_aprovacao: A 600 s. B e C seguem abertas
        # e a cancelada e final: nenhuma delas tem permanencia.
        assert dict(metricas.tempo_medio_por_status_segundos) == pytest.approx(
            {
                "recebida": 150.0,
                "em_diagnostico": (300 + 23 * 3600) / 2,
                "aguardando_aprovacao": 600.0,
            }
        )

    def test_banco_vazio_tem_os_nove_status_em_zero_e_nenhuma_media(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as sess:
            metricas = ler_metricas_das_ordens(sess, AGORA)

        assert metricas.total == 0
        assert dict(metricas.por_status) == dict.fromkeys(
            (s.value for s in StatusOrdem), 0
        )
        assert dict(metricas.tempo_medio_por_status_segundos) == {}

    @pytest.mark.parametrize(
        ("deslocamento", "entra"),
        [
            pytest.param(timedelta(hours=1), True, id="uma-hora-dentro"),
            pytest.param(timedelta(0), True, id="exatamente-na-borda"),
            pytest.param(-timedelta(hours=1), False, id="uma-hora-fora"),
        ],
    )
    def test_janela_de_30_dias_pela_entrada_no_status(
        self,
        session_factory: sessionmaker[Session],
        deslocamento: timedelta,
        entra: bool,
    ) -> None:
        entrada = AGORA - JANELA_DAS_PERMANENCIAS + deslocamento
        criar_ordens_com_historicos(
            session_factory,
            [("recebida", entrada), ("em_diagnostico", entrada + _s(600))],
        )

        with session_factory() as sess:
            medias = ler_metricas_das_ordens(
                sess, AGORA
            ).tempo_medio_por_status_segundos

        assert (dict(medias) == {"recebida": 600.0}) is entra
        assert (dict(medias) == {}) is not entra

    def test_permanencia_negativa_por_desvio_de_relogio_conta_zero(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        # Duas replicas gravam o `ocorrido_em` com relogios diferentes: a segunda
        # linha pode ser anterior a primeira, e a media nao pode ficar negativa.
        criar_ordens_com_historicos(
            session_factory,
            [("recebida", AGORA - _s(1000)), ("em_diagnostico", AGORA - _s(1010))],
        )

        with session_factory() as sess:
            medias = ler_metricas_das_ordens(
                sess, AGORA
            ).tempo_medio_por_status_segundos

        assert dict(medias) == {"recebida": 0.0}

    def test_status_de_outra_versao_fica_fora_sem_derrubar_a_leitura(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        (ordem_id,) = criar_ordens_com_historicos(
            session_factory,
            [
                ("recebida", AGORA - _s(900)),
                ("status_de_outra_versao", AGORA - _s(600)),
                ("em_diagnostico", AGORA - _s(300)),
            ],
        )
        with session_factory() as sess:
            sess.execute(
                text("UPDATE ordens_de_servico SET status = 'outro' WHERE id = :id"),
                {"id": ordem_id},
            )
            sess.commit()
            metricas = ler_metricas_das_ordens(sess, AGORA)

        assert metricas.total == 0
        assert set(metricas.por_status) == {s.value for s in StatusOrdem}
        assert dict(metricas.tempo_medio_por_status_segundos) == {"recebida": 300.0}

    def test_ordem_que_volta_ao_status_conta_cada_permanencia(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        criar_ordens_com_historicos(
            session_factory,
            [
                ("aguardando_pagamento", AGORA - _s(5000)),
                ("aguardando_execucao", AGORA - _s(4000)),
                ("aguardando_pagamento", AGORA - _s(3000)),
                ("aguardando_execucao", AGORA - _s(1000)),
            ],
        )

        with session_factory() as sess:
            medias = ler_metricas_das_ordens(
                sess, AGORA
            ).tempo_medio_por_status_segundos

        assert dict(medias) == pytest.approx(
            {"aguardando_pagamento": 1500.0, "aguardando_execucao": 1000.0}
        )


@pytest.fixture
def admin(api_client: TestClient, admin_user: Usuario) -> dict[str, str]:
    return _cabecalhos(api_client, admin_user.email)


def _cabecalhos(api: TestClient, email: str) -> dict[str, str]:
    login = api.post(
        "/api/v1/autenticacao/login", json={"email": email, "senha": SENHA_PADRAO}
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


class TestRota:
    def test_admin_recebe_total_por_status_e_permanencia_media(
        self,
        api_client: TestClient,
        admin: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        agora = datetime.now(UTC)
        criar_ordens_com_historicos(session_factory, *_fixture_conhecida(agora))

        resposta = api_client.get(_METRICAS, headers=admin)

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert set(corpo) == {"total", "por_status", "tempo_medio_por_status_segundos"}
        assert corpo["total"] == 3
        assert corpo["por_status"]["cancelada"] == 1
        assert corpo["por_status"]["recebida"] == 0
        # Os mesmos 150 s, 300 s + 23 h e 600 s da leitura (a janela anda com o
        # relogio real, e a folga das entradas e de horas).
        assert corpo["tempo_medio_por_status_segundos"] == pytest.approx(
            {
                "recebida": 150.0,
                "em_diagnostico": (300 + 23 * 3600) / 2,
                "aguardando_aprovacao": 600.0,
            }
        )

    def test_sem_historico_responde_zeros_e_nenhuma_media(
        self, api_client: TestClient, admin: dict[str, str]
    ) -> None:
        corpo = api_client.get(_METRICAS, headers=admin).json()

        assert corpo["total"] == 0
        assert set(corpo["por_status"].values()) == {0}
        assert len(corpo["por_status"]) == 9
        assert corpo["tempo_medio_por_status_segundos"] == {}

    def test_sem_token_e_401(self, api_client: TestClient) -> None:
        assert api_client.get(_METRICAS).status_code == 401

    def test_atendente_e_403(
        self, api_client: TestClient, session_factory: sessionmaker[Session]
    ) -> None:
        atendente = criar_usuario(
            session_factory, email="atendente-metricas@test.com", papel=Papel.ATENDENTE
        )

        resposta = api_client.get(
            _METRICAS, headers=_cabecalhos(api_client, atendente.email)
        )

        assert resposta.status_code == 403
        assert resposta.json()["erro"]["codigo"] == "ACESSO_NEGADO"

    def test_metricas_nao_e_lida_como_id_de_ordem(
        self, api_client: TestClient, admin: dict[str, str]
    ) -> None:
        # Declarada antes de /{ordem_id}: do contrario "metricas" cairia no UUID.
        assert api_client.get(_METRICAS, headers=admin).status_code != 422


# Os nomes que a API expoe logo depois do boot, sem trafego (secao 1 do catalogo).
_NOMES_DA_API = (
    "http_request_duration_seconds",
    "pytstop_os_criadas_total",
    "pytstop_saga_iniciadas_total",
    "pytstop_saga_finalizadas_total",
    "pytstop_saga_etapa_duracao_segundos",
    "pytstop_saga_compensacoes_total",
    "pytstop_saga_ativas",
    "pytstop_saga_etapa_mais_antiga_segundos",
    "pytstop_saga_prazo_vencido_segundos",
    "pytstop_saga_coletor_disponivel",
    "pytstop_saga_coletor_falhas_total",
    "pytstop_outbox_mais_antiga_segundos",
    "pytstop_os_por_status",
    "pytstop_os_tempo_medio_status_segundos",
)


def test_metrics_da_api_lista_os_nomes_do_processo_logo_depois_do_boot(
    api_client: TestClient,
) -> None:
    texto = api_client.get("/metrics").text

    faltam = [n for n in _NOMES_DA_API if f"# TYPE {n} " not in texto]
    assert not faltam
    # O histograma por status saiu: a conta do historico e a do coletor.
    assert "pytstop_os_duracao_status_segundos" not in texto
    assert "pytstop_saga_coletor_disponivel 1.0" in texto


def _registro(engine: Engine, **kwargs: object) -> CollectorRegistry:
    registro = CollectorRegistry()
    registro.register(ColetorDaApi(engine.connect, **kwargs))  # type: ignore[arg-type]  # kwargs do coletor
    return registro


def test_gauge_do_tempo_medio_guarda_a_fracao(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Duas permanencias em "recebida", 100,25 s e 200,5 s: a media e 150,375 s, e o
    # gauge nao pode truncar para 150 (a rota e a mesma leitura, com a fracao).
    criar_ordens_com_historicos(
        session_factory,
        [("recebida", AGORA - _s(500)), ("em_diagnostico", AGORA - _s(500 - 100.25))],
        [("recebida", AGORA - _s(400)), ("em_diagnostico", AGORA - _s(400 - 200.5))],
    )

    registro = _registro(engine, agora=lambda: AGORA)

    assert registro.get_sample_value(
        "pytstop_os_tempo_medio_status_segundos", {"status": "recebida"}
    ) == pytest.approx(150.375, abs=1e-3)


def test_gauges_do_coletor_sao_os_numeros_da_rota(
    api_client: TestClient,
    admin: dict[str, str],
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    criar_ordens_com_historicos(session_factory, *_fixture_conhecida(datetime.now(UTC)))

    corpo = api_client.get(_METRICAS, headers=admin).json()
    registro = _registro(engine)

    for status, total in corpo["por_status"].items():
        assert registro.get_sample_value(
            "pytstop_os_por_status", {"status": status}
        ) == (total)
    medias = corpo["tempo_medio_por_status_segundos"]
    for status in (s.value for s in StatusOrdem):
        gauge = registro.get_sample_value(
            "pytstop_os_tempo_medio_status_segundos", {"status": status}
        )
        if status in medias:
            assert gauge == pytest.approx(medias[status])
        else:
            # Sem permanencia concluida a serie falta: nunca zero.
            assert gauge is None
