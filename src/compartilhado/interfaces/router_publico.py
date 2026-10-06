"""Router publico compartilhado: probes de saude fora do middleware de auth.

A consulta publica de acompanhamento da OS vive no proprio contexto
(``ordem_servico/interfaces/router.py``).
"""

from __future__ import annotations

import asyncio
from typing import Final

import structlog
from fastapi import APIRouter
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.compartilhado.interfaces.dependencies import abrir_session
from src.compartilhado.interfaces.middleware import limiter

router = APIRouter(tags=["publico"])
_log = structlog.get_logger(__name__)

# A readiness responde 503 em ate 2 s com o banco fora, preso ou lento.
_TIMEOUT_PRONTO_S: Final = 2.0


@router.get("/api/v1/saude", summary="Liveness probe")
@limiter.exempt  # type: ignore[untyped-decorator]  # slowapi 0.1.9 nao tem stubs
async def saude() -> dict[str, str]:
    """Liveness: 200 enquanto o processo responde, sem tocar dependencias.

    E o HEALTHCHECK da imagem: banco fora nao deve reiniciar o pod (isso e
    papel da readiness, que tira o pod do balanceamento).

    ``async def`` de proposito: as rotas da API sao sync (``def``) e rodam no
    threadpool do anyio; sob carga que satura esse pool, uma ``saude`` sync
    ficaria na fila e estouraria o timeout das probes -> liveness reiniciaria
    o pod exatamente no pico. Como ``async``, responde direto no event loop.

    Isenta do rate limit: as probes do kubelet saem todas do IP do no (chave
    do limiter) e, sob HPA, estourariam o limite global -> 429 no health check
    -> restart storm.
    """
    return {"status": "ok"}


def _consultar_banco() -> None:
    with abrir_session() as sessao:
        sessao.execute(text("SELECT 1"))


@router.get(
    "/api/v1/saude/pronto",
    summary="Readiness probe (banco acessivel)",
    responses={503: {"description": "Banco inacessivel ou acima de 2 s."}},
)
@limiter.exempt  # type: ignore[untyped-decorator]  # slowapi 0.1.9 nao tem stubs
async def pronto() -> JSONResponse:
    """Readiness: 200 so com o banco respondendo ``SELECT 1`` em ate 2 s.

    503 tira o pod do balanceamento (rolling update, banco fora) sem
    reinicia-lo. Isenta do rate limit pelo mesmo motivo da liveness.
    """
    try:
        await asyncio.wait_for(
            run_in_threadpool(_consultar_banco), timeout=_TIMEOUT_PRONTO_S
        )
    except Exception as exc:  # noqa: BLE001  # qualquer falha = nao pronto
        _log.warning("readiness_failed", error=type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "indisponivel"})
    return JSONResponse(content={"status": "ok"})
