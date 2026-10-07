"""Refresh e logout simultaneos com o mesmo token nao viram 500.

Refresh: dois pedidos com o mesmo refresh passam juntos pela checagem de
revogacao e o que perde a corrida esbarraria no UNIQUE do ``jti`` ao revogar.
O banco decide quem revoga primeiro e o perdedor recebe o 401 de qualquer
refresh ja consumido. Logout: nao consulta a revogacao, entao todos os pedidos
respondem 200.
"""

from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import text

from src.autenticacao.infraestrutura.token_revogado_repository import (
    TokenRevogadoSQLAlchemyRepository,
)
from src.compartilhado.interfaces.middleware import limiter
from tests.chaves_jwt import jwt_service

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session, sessionmaker

    from src.autenticacao.dominio.usuario import Usuario

_CONCORRENTES = 6
_REPETICOES = 5


def _esperar_insert_bloqueado(session_factory: sessionmaker[Session]) -> None:
    """Espera um INSERT em ``tokens_revogados`` parar no lock da outra transacao.

    Cada leitura abre uma transacao nova: o ``pg_stat_activity`` fica fixo (em
    cache) dentro de uma transacao.
    """
    consulta = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
        "AND query LIKE 'INSERT INTO tokens_revogados%'"
    )
    prazo = time.monotonic() + 10
    while time.monotonic() < prazo:
        with session_factory() as sondagem:
            if sondagem.scalar(consulta):
                return
        time.sleep(0.02)
    msg = "o INSERT concorrente nao parou no lock"
    raise AssertionError(msg)


def test_perdedor_da_corrida_do_revogar_recebe_false(
    session_factory: sessionmaker[Session],
) -> None:
    jti = str(uuid4())
    with (
        session_factory() as sessao_a,
        session_factory() as sessao_b,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        # A insere e ainda nao commitou: B nao enxerga a linha e para no INSERT,
        # esperando a transacao de A.
        assert TokenRevogadoSQLAlchemyRepository(sessao_a).revogar(jti) is True
        perdedor = pool.submit(TokenRevogadoSQLAlchemyRepository(sessao_b).revogar, jti)
        _esperar_insert_bloqueado(session_factory)

        sessao_a.commit()

        assert perdedor.result(timeout=10) is False


def _simultaneos(chamada: Callable[[], httpx.Response]) -> list[httpx.Response]:
    """Dispara ``chamada`` em ``_CONCORRENTES`` threads, soltas ao mesmo tempo."""
    largada = Barrier(_CONCORRENTES)

    def _disparar() -> httpx.Response:
        largada.wait(timeout=10)
        return chamada()

    with ThreadPoolExecutor(max_workers=_CONCORRENTES) as pool:
        futuros = [pool.submit(_disparar) for _ in range(_CONCORRENTES)]
        return [futuro.result(timeout=30) for futuro in futuros]


def test_refresh_simultaneo_do_mesmo_token_da_um_200_e_o_resto_401(
    api_client: TestClient, admin_user: Usuario
) -> None:
    for _ in range(_REPETICOES):
        # O limite de 10/min do /refresh vale por IP: cada repeticao comeca limpa.
        limiter.reset()
        refresh = jwt_service().gerar_refresh_token(admin_user.id)

        respostas = _simultaneos(
            lambda token=refresh: api_client.post(
                "/api/v1/autenticacao/refresh", json={"refresh_token": token}
            )
        )

        assert Counter(r.status_code for r in respostas) == {
            200: 1,
            401: _CONCORRENTES - 1,
        }
        for perdedora in (r for r in respostas if r.status_code == 401):
            erro = perdedora.json()["erro"]
            assert (erro["codigo"], erro["mensagem"]) == (
                "NAO_AUTENTICADO",
                "Credencial ausente, invalida ou expirada",
            )
            assert perdedora.headers["WWW-Authenticate"] == "Bearer"


def test_logout_simultaneo_do_mesmo_token_responde_200_para_todos(
    api_client: TestClient, admin_user: Usuario
) -> None:
    access = jwt_service().gerar_access_token(admin_user.id, "admin")
    refresh = jwt_service().gerar_refresh_token(admin_user.id)

    respostas = _simultaneos(
        lambda: api_client.post(
            "/api/v1/autenticacao/logout",
            headers={"Authorization": f"Bearer {access}"},
            json={"refresh_token": refresh},
        )
    )

    # O logout e idempotente: o primeiro revoga o access e o refresh, e os outros
    # perdem a corrida no INSERT sem estourar o UNIQUE do jti.
    assert [r.status_code for r in respostas] == [200] * _CONCORRENTES
    revogado = api_client.post(
        "/api/v1/autenticacao/refresh", json={"refresh_token": refresh}
    )
    assert revogado.status_code == 401
