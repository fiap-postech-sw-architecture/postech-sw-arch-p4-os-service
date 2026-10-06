# syntax=docker/dockerfile:1.7
# Builder e runtime usam a MESMA minor do Python (3.14, igual ao
# .python-version): o venv copiado carrega bytecode e wheels compilados para
# a versao do builder.
FROM ghcr.io/astral-sh/uv:0.9-python3.14-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Manifests primeiro: a layer de dependencias e reaproveitada enquanto
# pyproject/lock nao mudam.
COPY pyproject.toml uv.lock ./

# --frozen falha se o uv.lock divergir do pyproject; --extra otel deixa o SDK
# OpenTelemetry na imagem (a instrumentacao so liga por env).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra otel

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra otel

FROM python:3.14-slim AS runtime

ARG GIT_SHA=unknown
ARG GIT_DATE=unknown

LABEL org.opencontainers.image.title="pytstop-os-service" \
      org.opencontainers.image.source="https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service" \
      org.opencontainers.image.description="PytStop fase 4 - OS Service (FastAPI)." \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${GIT_DATE}"

# Pacotes do SO atualizados a cada build: a tag movel python:3.14-slim fica
# semanas sem rebuild e o trivy acusa CVEs do Debian ja corrigidos no apt.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# UID/GID numericos (1001): o kubelet so verifica runAsNonRoot com UID numerico.
RUN groupadd -r -g 1001 pytstop && useradd -r -u 1001 -g pytstop pytstop

# Sem pip no runtime: o app roda so pelo venv do uv e nunca instala nada em
# execucao; o pip da base traz pacotes vendorizados que o trivy audita.
RUN python -m pip uninstall -y pip

WORKDIR /app

COPY --from=builder --chown=pytstop:pytstop /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTSTOP_GIT_SHA="${GIT_SHA}" \
    PYTSTOP_GIT_DATE="${GIT_DATE}"

# Probe em Python + urllib porque a imagem slim nao tem curl/wget.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/saude', timeout=2).status==200 else 1)"]

RUN chmod +x entrypoint.sh

USER 1001
EXPOSE 8000
CMD ["./entrypoint.sh"]
