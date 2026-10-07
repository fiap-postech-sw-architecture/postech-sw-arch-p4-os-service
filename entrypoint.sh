#!/bin/bash
set -e

echo ">>> pytstop-os-service | commit ${PYTSTOP_GIT_SHA:0:12} | ${PYTSTOP_GIT_DATE:-unknown}"

# Migracao e seed ligados por env no compose (container unico, sem corrida).
# No Kubernetes a migracao roda num Job dedicado antes do rollout, entao o
# ConfigMap deixa RUN_MIGRATIONS_ON_STARTUP=false e este bloco vira no-op.
if [ "${RUN_MIGRATIONS_ON_STARTUP:-false}" = "true" ]; then
  echo "Running database migrations..."
  alembic upgrade head
else
  echo "Skipping migrations on startup (RUN_MIGRATIONS_ON_STARTUP != true)."
fi

if [ "${RUN_SEED_ON_STARTUP:-false}" = "true" ]; then
  echo "Running admin seed..."
  # Best-effort: falha do seed (credencial ausente, corrida de replica) nao
  # bloqueia o boot da API.
  python scripts/seed_admin.py || echo "Admin seed did not complete - continuing startup."
else
  echo "Skipping admin seed (RUN_SEED_ON_STARTUP != true)."
fi

# --no-proxy-headers: o trust de X-Forwarded-For e controlado pela app via
# TRUSTED_PROXIES (ProxyHeadersMiddleware proprio); a flag desliga a camada
# implicita do uvicorn, que confiaria no XFF de peers loopback.
# --no-server-header: sem `server: uvicorn` (nao anuncia a stack).
# --root-path: no cluster a API responde atras do Kong sob o prefixo do servico
# (ROOT_PATH=/os no ConfigMap, ADR-038): o Swagger em /os/docs busca o
# /os/openapi.json, e o OpenAPI declara o prefixo em `servers`. Vazio fora dele.
exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers \
  --no-server-header --root-path "${ROOT_PATH:-}"
