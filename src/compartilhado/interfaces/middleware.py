from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

import structlog
from cryptography.fernet import Fernet
from fastapi import FastAPI
from limits import parse_many
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.compartilhado.interfaces.error_handler import resposta_erro_interno

_CSP_DEFAULT = "default-src 'none'"
# Paths that serve Swagger UI / ReDoc / OpenAPI schema. The default CSP blocks
# the inline scripts and styles those tools rely on, so we skip CSP there.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def _caminho_de_docs(path: str) -> bool:
    """True para o proprio path de docs ou um filho dele (barra obrigatoria).

    Igualdade exata ou prefixo TERMINADO em "/": ``/docs`` e ``/docs/oauth2``
    casam; ``/docsarquivo`` (prefixo acidental) NAO casa e mantem o CSP.
    """
    return any(path == p or path.startswith(p + "/") for p in _DOCS_PATHS)


# Correlacao fim-a-fim: o X-Request-ID gerado na borda (Kong, plugin
# correlation-id, brief secao 5) e aceito quando "sano" — ate 128 chars de um
# charset seguro para logs e headers. Qualquer outra coisa (vazio, longo
# demais, espacos, CRLF, unicode) e descartada e um uuid4 novo assume: nunca
# ecoamos lixo nem injecao de log de volta no header.
_REQUEST_ID_EXTERNO_VALIDO = re.compile(r"[A-Za-z0-9._=-]{1,128}")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Anexa headers de seguranca em toda resposta e propaga o request_id.

    Inclusive no 500 de erro nao tratado, que este middleware converte no
    envelope do contrato (``resposta_erro_interno``).

    Aceita o ``X-Request-ID`` vindo de fora quando valido (correlacao
    gateway -> servico); ausente ou invalido, gera um uuid4.
    Vincula o request_id ao contexto do structlog para correlacionar logs
    dentro do mesmo request. Os headers aplicados estao listados no metodo
    dispatch.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        recebido = request.headers.get("X-Request-ID", "")
        request_id = (
            recebido if _REQUEST_ID_EXTERNO_VALIDO.fullmatch(recebido) else str(uuid4())
        )
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001  # vira o 500 do contrato
            # O handler de Exception do app roda no ServerErrorMiddleware, por
            # fora deste: sem converter aqui, o 500 sairia sem estes headers.
            response = resposta_erro_interno(request, exc)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["Cache-Control"] = "no-store"
        if not _caminho_de_docs(request.url.path):
            response.headers["Content-Security-Policy"] = _CSP_DEFAULT
        response.headers["X-Request-ID"] = request_id
        return response


# Ambientes que NAO exercem a guarda de segredos: dev local, docker compose e
# a suite de testes sobem deliberadamente com segredos de demonstracao.
# Qualquer outro valor de ENVIRONMENT (notadamente "production") dispara a
# validacao -- mesma postura de `resolver_database_url` (database.py).
_AMBIENTES_SEM_VALIDACAO_DE_SEGREDOS = frozenset({"development", "test"})

# HS256 exige chave com no minimo 32 BYTES; abaixo disso e forjavel.
_JWT_SECRET_MIN_BYTES = 32

# Literais de segredo de DEMONSTRACAO publicos no git -- proibidos em producao.
# Fonte: docker-compose.yml. Mantenha em sincronia quando um default de demo
# mudar ou quando k8s/ ganhar um Secret de demo.
_SEGREDOS_DEMO_PROIBIDOS = frozenset(
    {
        "demo-jwt-secret-os-service-fase4-nao-usar-em-producao",
        # Chave Fernet de demo (base64 de 44 chars dispara o generic-api-key).
        "Chqh4o4QURACWBUSdtXjAxhOQt6HhxAfEg9rtvsABKU=",  # gitleaks:allow
        "admin-demo-os-2026",  # gitleaks:allow
    }
)
# Senha do Postgres de demonstracao (compose), comparada com a da DATABASE_URL.
_SENHA_DO_BANCO_DEMO = "pytstop"  # gitleaks:allow - senha do compose local

# Validade dos tokens: inteiros positivos (minutos), lidos a cada request.
_VARIAVEIS_DE_MINUTOS_JWT = ("JWT_EXPIRATION_MINUTES", "JWT_REFRESH_EXPIRATION_MINUTES")


def validar_segredos_no_startup() -> None:
    """Valida os segredos sensiveis no startup; aborta o boot em producao.

    EM PRODUCAO (qualquer ENVIRONMENT que nao seja `development`/`test`):

    1. ``JWT_SECRET`` ausente ou com menos de 32 BYTES -> aborta.
    2. ``ENCRYPTION_KEY`` ausente -> aborta: a chave efemera de fallback
       tornaria os dados cifrados irrecuperaveis apos restart e divergiria o
       ``documento_hash`` entre replicas.
    3. ``JWT_SECRET`` / ``ENCRYPTION_KEY`` / ``ADMIN_PASSWORD`` iguais a um
       literal de demonstracao publico no git -> aborta (cada um so quando
       presente); idem para a senha do Postgres de demo na ``DATABASE_URL``.
    4. ``ENCRYPTION_KEY`` que nao e chave Fernet, ou minutos de JWT que nao
       sao inteiros positivos -> aborta.

    Falha o boot (``raise``), nao apenas avisa: pre-condicao de seguranca
    nao satisfeita nunca sobe aceitando requisicoes. Fora de producao e no-op.
    """
    environment = os.environ.get("ENVIRONMENT", "development").lower()
    if environment in _AMBIENTES_SEM_VALIDACAO_DE_SEGREDOS:
        return

    jwt_secret = os.environ.get("JWT_SECRET")
    if not jwt_secret:
        msg = (
            "JWT_SECRET nao configurado em producao. HS256 exige uma chave de "
            f">= {_JWT_SECRET_MIN_BYTES} bytes; defina um segredo forte via Secret."
        )
        raise RuntimeError(msg)
    if len(jwt_secret.encode("utf-8")) < _JWT_SECRET_MIN_BYTES:
        msg = (
            f"JWT_SECRET tem menos de {_JWT_SECRET_MIN_BYTES} bytes. HS256 exige "
            f">= {_JWT_SECRET_MIN_BYTES} bytes; gere um segredo forte "
            "(ex.: openssl rand -hex 32)."
        )
        raise RuntimeError(msg)

    if not os.environ.get("ENCRYPTION_KEY"):
        msg = (
            "ENCRYPTION_KEY nao configurada em producao. Sem ela o app sobe com "
            "uma chave Fernet efemera -> dados cifrados ficam irrecuperaveis "
            "apos restart e o documento_hash diverge entre replicas. Injete uma "
            "chave real via Secret (gere com Fernet.generate_key())."
        )
        raise RuntimeError(msg)

    for nome in ("JWT_SECRET", "ENCRYPTION_KEY", "ADMIN_PASSWORD"):
        valor = os.environ.get(nome)
        if valor and valor in _SEGREDOS_DEMO_PROIBIDOS:
            msg = (
                f"{nome} usa um segredo de demonstracao publico no git -- proibido "
                "em producao. Injete um segredo real via Secret."
            )
            raise RuntimeError(msg)

    url_do_banco = os.environ.get("DATABASE_URL", "")
    if url_do_banco and urlsplit(url_do_banco).password == _SENHA_DO_BANCO_DEMO:
        msg = (
            "DATABASE_URL usa a senha do Postgres de demonstracao -- proibido em "
            "producao. Injete a credencial real via Secret."
        )
        raise RuntimeError(msg)

    _validar_formatos_de_configuracao()


def _validar_formatos_de_configuracao() -> None:
    """Configuracao malformada aborta o boot, nao vira 4xx na primeira request.

    Sem isto, uma ``ENCRYPTION_KEY`` invalida subia o app e o primeiro
    documento devolvia 422 (erro de configuracao com cara de erro do cliente),
    e minutos de JWT nao numericos virariam 500 em todo login.
    """
    try:
        Fernet(os.environ["ENCRYPTION_KEY"])
    except ValueError as exc:
        msg = (
            "ENCRYPTION_KEY invalida: precisa ser uma chave Fernet (32 bytes em "
            "base64 url-safe, gere com Fernet.generate_key())."
        )
        raise RuntimeError(msg) from exc
    for nome in _VARIAVEIS_DE_MINUTOS_JWT:
        bruto = os.environ.get(nome)
        if bruto is None:
            continue
        try:
            minutos = int(bruto)
        except ValueError:
            minutos = 0
        if minutos <= 0:
            msg = f"{nome} precisa ser um inteiro positivo (minutos)."
            raise RuntimeError(msg)


def configurar_cors(app: FastAPI) -> None:
    """Configura CORS a partir da variavel CORS_ORIGINS (lista separada por virgulas).

    Vazio significa nenhuma origem permitida. O wildcard `*` e rejeitado em
    startup: a API exige origens explicitas, sem allow-any.

    ``allow_credentials=False``: a API e bearer-only (token no header
    Authorization), sem cookies nem sessao -- nenhum fluxo depende de
    credenciais no CORS, entao mante-las desligadas reduz a superficie.
    """
    origens = os.environ.get("CORS_ORIGINS", "")
    lista_origens = [o.strip() for o in origens.split(",") if o.strip()]
    if "*" in lista_origens:
        msg = "CORS_ORIGINS='*' nao e suportado. Defina origens explicitas."
        raise ValueError(msg)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=lista_origens,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )


def _resolver_trusted_proxies() -> list[str]:
    """Resolve a lista de proxies confiaveis a partir do ambiente.

    Le ``TRUSTED_PROXIES`` (hosts separados por virgula) e devolve a lista de
    tokens nao vazios. Cada token e passado ao ``trusted_hosts`` do
    ``ProxyHeadersMiddleware`` do uvicorn, que aceita IP exato (``10.0.0.5``),
    rede CIDR (``10.0.0.0/8``) ou o wildcard ``*`` (confia em qualquer peer).

    PERIGO: ``*`` confia em QUALQUER peer e usa o XFF mais a esquerda
    (spoofavel) -- so com a app ESTRITAMENTE ClusterIP atras de proxy
    confiavel, nunca exposta direto.

    Vazio (ausente, em branco ou so virgulas) -> lista vazia -> o middleware
    NAO e instalado e o ``X-Forwarded-For`` e ignorado. Default SEGURO: sem
    configuracao explicita nunca se confia no XFF (sem risco de spoof do IP
    do cliente).
    """
    bruto = os.environ.get("TRUSTED_PROXIES", "")
    return [host.strip() for host in bruto.split(",") if host.strip()]


def configurar_proxy_headers(app: FastAPI) -> None:
    """Instala o ``ProxyHeadersMiddleware`` do uvicorn quando ha proxy confiavel.

    Quando ``TRUSTED_PROXIES`` esta definido, o middleware reescreve
    ``scope["client"]`` a partir do ``X-Forwarded-For`` SOMENTE quando o peer
    imediato (o IP da conexao TCP) esta na lista de confianca — entao
    ``get_remote_address`` (chave do rate limiter) passa a devolver o IP real
    do cliente, e nao o IP do proxy/ingress. Vazio (default) -> nao
    instala -> comportamento atual preservado (XFF ignorado, sem spoof).

    O servidor uvicorn roda com ``--no-proxy-headers`` (``entrypoint.sh``), o
    que DESLIGA o proxy-headers embutido dele (ligado por padrao com
    ``forwarded_allow_ips="127.0.0.1"``, que confiaria no XFF de peers
    loopback). Assim ``TRUSTED_PROXIES`` -- via este middleware -- e o UNICO
    controle de confianca no ``X-Forwarded-For``, e o default vazio ignora o
    XFF de TODOS os peers (inclusive loopback), batendo com os testes.

    ORDEM (Starlette: o ULTIMO middleware adicionado e o MAIS EXTERNO e roda
    PRIMEIRO no request). Este precisa rodar ANTES do ``SlowAPIMiddleware``
    para que o ``client`` ja esteja reescrito quando o limiter ler
    ``request.client.host``. ``criar_app`` adiciona este middleware DEPOIS de
    ``configurar_rate_limiting`` justamente para coloca-lo por fora do
    SlowAPI. ``SecurityHeadersMiddleware``, adicionado por ultimo, fica ainda
    mais externo (so estampa request_id/headers, nao depende do client).

    PERIGO: ``*`` em ``TRUSTED_PROXIES`` confia em QUALQUER peer e usa o XFF
    mais a esquerda (spoofavel) -- so com a app ESTRITAMENTE ClusterIP atras
    de proxy confiavel, nunca exposta direto.
    """
    trusted = _resolver_trusted_proxies()
    if not trusted:
        return
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)


# Singleton compartilhado entre todos os routers que aplicam limite por
# endpoint via ``@limiter.limit("...")``. CRITICO: o decorator precisa usar a
# MESMA instancia que o ``SlowAPIMiddleware`` le de ``app.state.limiter``,
# senao os contadores ficam em instancias diferentes e o limite nao vale.
#
# O limite padrao vem de ``RATE_LIMIT`` no import, validado EAGER: valor
# malformado aborta o boot em vez de virar 500 na primeira request.
#
# ponytail: contador em memoria, por processo. O limite agregado entre
# replicas e do API Gateway (Kong, brief secao 5); este fica como defesa em
# profundidade. Storage compartilhado (ex.: Redis) entra se o gateway sair.
_default_limit = os.environ.get("RATE_LIMIT", "60/minute")
try:
    parse_many(_default_limit)
except ValueError as _exc:
    _dica = "Use a notacao do pacote `limits` (ex.: '60/minute', '100/hour')."
    _msg = f"RATE_LIMIT invalido: {_default_limit!r}. {_dica}"
    raise RuntimeError(_msg) from _exc
limiter = Limiter(key_func=get_remote_address, default_limits=[_default_limit])


def handler_rate_limit_excedido(request: Request, exc: Exception) -> Response:
    """429 no envelope de erro do contrato da API (em vez do default do SlowAPI).

    O ``_rate_limit_exceeded_handler`` do SlowAPI responde ``{"error": ...}``,
    fora do envelope ``{erro: {codigo, mensagem, id_requisicao}}`` que todos
    os outros status usam (``error_handler.py``). Este handler devolve o
    envelope do contrato e PRESERVA a injecao de headers do limiter
    (``Retry-After``/``X-RateLimit-*`` quando ``headers_enabled``), fazendo o
    mesmo pos-processamento do handler default.
    """
    detalhe = getattr(exc, "detail", "limite de requisicoes excedido")
    response: Response = JSONResponse(
        status_code=429,
        content={
            "erro": {
                "codigo": "RATE_LIMIT_EXCEDIDO",
                "mensagem": f"Limite de requisicoes excedido: {detalhe}",
                "id_requisicao": getattr(request.state, "request_id", "desconhecido"),
            }
        },
    )
    # Mesmo pos-processamento do handler default do SlowAPI (que tambem chama
    # o metodo privado _inject_headers do limiter).
    com_headers: Response = request.app.state.limiter._inject_headers(
        response, request.state.view_rate_limit
    )
    return com_headers


def configurar_rate_limiting(app: FastAPI) -> None:
    """Anexa o ``limiter`` compartilhado ao app e instala o SlowAPIMiddleware.

    O 429 responde no envelope de erro do contrato via
    ``handler_rate_limit_excedido``.
    """
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, handler_rate_limit_excedido)
