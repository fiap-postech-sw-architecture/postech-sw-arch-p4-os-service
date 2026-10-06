"""Router publico compartilhado: health probe fora do middleware de auth.

A consulta publica de acompanhamento da OS vive no proprio contexto
(``ordem_servico/interfaces/router.py``).
"""

from __future__ import annotations

from fastapi import APIRouter

from src.compartilhado.interfaces.middleware import limiter

router = APIRouter(tags=["publico"])


@router.get("/api/v1/saude", summary="Health probe")
@limiter.exempt  # type: ignore[untyped-decorator]  # slowapi 0.1.9 nao tem stubs
async def saude() -> dict[str, str]:
    """Health probe para Kubernetes/load balancer. Retorna 200 quando o app sobe.

    ``async def`` de proposito: as rotas da API sao sync (``def``) e rodam no
    threadpool do anyio; sob carga que satura esse pool, uma ``saude`` sync
    ficaria na fila e estouraria o timeout das probes -> liveness reiniciaria
    o pod exatamente no pico. Como ``async``, responde direto no event loop.

    Isenta do rate limit: as probes do kubelet saem todas do IP do no (chave
    do limiter) e, sob HPA, estourariam o limite global -> 429 no health check
    -> restart storm. A isencao e so desta rota.
    """
    return {"status": "ok"}
