from __future__ import annotations

import os
from collections.abc import AsyncGenerator  # noqa: TC003
from contextlib import asynccontextmanager
from importlib.metadata import version

import uvicorn
from fastapi import FastAPI

from src.autenticacao.interfaces.dependencies import validar_chave_jwt_no_startup
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from src.compartilhado.interfaces.middleware import (
    SecurityHeadersMiddleware,
    configurar_cors,
    configurar_proxy_headers,
    configurar_rate_limiting,
    validar_segredos_no_startup,
)
from src.compartilhado.interfaces.router_publico import router as router_publico


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Ciclo de vida do app: inicializa os mappings, as guardas e a sessao."""
    # Banner de boot. SHA/data sao injetadas em todo log structlog pelo
    # processor `adicionar_versao_imagem` (configurado em `criar_app`) --
    # nao precisa de `bind_contextvars` aqui, que seria limpado pelo
    # SecurityHeadersMiddleware a cada request.
    git_sha = os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12]
    git_date = os.environ.get("PYTSTOP_GIT_DATE", "unknown")
    print(f">>> pytstop-os-service | commit {git_sha} | {git_date}", flush=True)

    # Registra os imperative mappings de todos os bounded contexts antes de
    # aceitar requisicoes (idempotente; a ordem correta entre contexts vive
    # em ``bootstrap.iniciar_todos_mapeamentos``, ponto unico de verdade).
    from src.compartilhado.infraestrutura.bootstrap import (
        iniciar_todos_mapeamentos,
    )

    iniciar_todos_mapeamentos()

    # Contratos de mensageria lidos no boot, como no relay e no consumidor: a
    # imagem sem `contratos/` falha aqui, e nao no primeiro comando publicado.
    from src.compartilhado.infraestrutura.mensageria.contratos import catalogo

    catalogo()

    # Configura a session factory global antes de aceitar requisicoes.
    # Sem isso, todo endpoint que depende de ``obter_session`` falha com
    # ``RuntimeError("Session factory nao configurada")``. O engine e
    # descartado no shutdown para liberar o pool de conexoes.
    from src.compartilhado.infraestrutura.database import (
        criar_engine,
        criar_session_factory,
        resolver_database_url,
    )
    from src.compartilhado.infraestrutura.observability import configurar_otel
    from src.compartilhado.interfaces.dependencies import (
        configurar_session_factory,
    )

    # DATABASE_URL explicita tem precedencia. Em dev/test, a URL pode ser
    # montada pelas variaveis POSTGRES_* do compose/env local; a senha nunca
    # fica hardcoded no codigo.
    database_url = resolver_database_url()

    # Guardas de segredos: fora de dev/test, aborta o boot com segredo
    # ausente, fraco ou de demonstracao (o JWT tem guarda propria, para a chave
    # RSA e a validade, no contexto que o usa). Rodam antes de criar o engine.
    validar_segredos_no_startup()
    validar_chave_jwt_no_startup()

    engine = criar_engine(database_url)
    configurar_session_factory(criar_session_factory(engine))

    # Observabilidade OTLP: unico ponto onde app + engine existem
    # juntos. Default OFF (OTEL_ENABLED ausente/false) — no-op sem custo.
    configurar_otel(app, engine)
    try:
        yield
    finally:
        engine.dispose()


def criar_app() -> FastAPI:
    """Fabrica o FastAPI com middleware de seguranca, CORS, rate limiting e handlers.

    Swagger (`/docs`, `/redoc`, `/openapi.json`) fica ligado em todo ambiente:
    a documentacao da API e entregavel e e publicada na borda (Kong).

    O uvicorn importa o app antes da primeira linha do servidor ("Started
    server process"), e o log JSON com o scrub de PII e configurado aqui, nao no
    lifespan: o log do uvicorn e o do app saem em JSON e mascarados desde a
    primeira linha.
    """
    configurar_logging()
    application = FastAPI(
        title="PytStop OS Service",
        description=(
            "Ordens de servico (abertura, status e historico), clientes e "
            "veiculos, usuarios internos. Fase 4 do PytStop."
        ),
        version=version("pytstop-os-service"),
        lifespan=lifespan,
        # Sem 307 para a variante com/sem barra: atras do Kong o Location
        # absoluto sairia com o esquema errado. Rotas de colecao nao tem barra.
        redirect_slashes=False,
    )

    # Imports locais: routers so carregam ao fabricar o app (sem instanciacao
    # precoce de dependencias no import do modulo).
    from src.autenticacao.interfaces.router import router as auth_router
    from src.autenticacao.interfaces.router import router_jwks
    from src.cliente_veiculo.interfaces.router import router as cliente_router
    from src.ordem_servico.interfaces.router import router as os_router
    from src.ordem_servico.interfaces.router import (
        router_publico as os_router_publico,
    )
    from src.ordem_servico.interfaces.router import router_sagas

    application.include_router(router_publico)
    application.include_router(auth_router)
    application.include_router(router_jwks)
    application.include_router(cliente_router)
    application.include_router(os_router)
    application.include_router(router_sagas)
    application.include_router(os_router_publico)

    # Ordem dos middlewares (Starlette: o ultimo adicionado e o mais externo).
    # Lado do request, de fora para dentro:
    # SecurityHeaders -> ProxyHeaders -> SlowAPI -> CORS -> rota.
    # SecurityHeaders por ultimo: estampa request_id e headers inclusive no
    # preflight CORS e nos 429. ProxyHeaders por fora do SlowAPI: reescreve o
    # client a partir de um X-Forwarded-For confiavel ANTES de o limiter ler o
    # IP (so instala com TRUSTED_PROXIES definido).
    configurar_cors(application)
    configurar_rate_limiting(application)
    configurar_proxy_headers(application)
    application.add_middleware(SecurityHeadersMiddleware)
    registrar_error_handlers(application)

    # Metricas Prometheus (API_METRICS_ENABLED, default off). Aqui e nao no
    # lifespan porque adiciona middleware; adicionado por ultimo, o
    # MetricasHTTPMiddleware fica o mais externo e mede a latencia completa.
    from src.compartilhado.infraestrutura.metrics import configurar_metricas_api

    if configurar_metricas_api(application):
        from src.ordem_servico.infraestrutura.metrics import (
            instrumentar_metricas_de_ordens,
        )

        instrumentar_metricas_de_ordens()

    return application


app = criar_app()


def executar_servidor_dev() -> None:
    """Executa o servidor local quando o modulo e chamado como script."""
    uvicorn.run(
        "src.main:app",
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ.get("UVICORN_PORT", "8000")),
        reload=True,
    )


if __name__ == "__main__":
    executar_servidor_dev()
