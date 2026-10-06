# Gates do OS Service, os mesmos comandos do CI. `make check` espelha os jobs
# lint, type-check, security e test: uv.lock em dia, lint, formato, contratos
# de camada, tipos, seguranca e testes (unitarios + integracao com
# testcontainers) com gate de cobertura (.coveragerc). `make smoke` e o job
# build e `make audit` e o pip-audit do workflow Security.
PY := uv run
PY_PATHS := src/ scripts/ migrations/
PY_PATHS_COM_TESTS := $(PY_PATHS) tests/

GIT_SHA  := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
DOCKER_COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: lock-check lint format lint-arch typecheck security test check audit \
	smoke compose-up compose-down

lock-check:
	uv lock --check

lint:
	$(PY) ruff check $(PY_PATHS_COM_TESTS)
	$(PY) ruff format --check $(PY_PATHS_COM_TESTS)

format:
	$(PY) ruff format $(PY_PATHS_COM_TESTS)
	$(PY) ruff check $(PY_PATHS_COM_TESTS) --fix

lint-arch:
	$(PY) lint-imports

typecheck:
	$(PY) mypy src scripts

security:
	$(PY) bandit -r src scripts -c pyproject.toml -q

# Suite completa com cobertura (addopts do pyproject liga --cov=src). Gera os
# relatorios que o CI publica (coverage.xml para o SonarQube, htmlcov/ e o
# JUnit em reports/). O conftest de integracao acha o socket do colima.
test:
	$(PY) pytest --cov-report=xml:coverage.xml --cov-report=html:htmlcov \
		--junitxml=reports/junit.xml

check: lock-check lint lint-arch typecheck security test
	@echo "All checks passed"

# CVE nas dependencias de runtime, com os extras que a imagem instala: o
# mesmo conjunto do job pip-audit. Aqui o export leva hashes e o pip-audit
# roda com --disable-pip (o lock ja e o fechamento completo): sem o venv
# temporario, cujo ensurepip aborta no Python 3.14 do macOS.
audit:
	@mkdir -p reports
	uv export --frozen --no-emit-project --no-dev --all-extras \
		--format requirements-txt -o reports/requirements-prod.txt
	uvx pip-audit==2.10.1 -r reports/requirements-prod.txt --strict \
		--disable-pip --progress-spinner off

# Smoke da imagem pelo entrypoint real (migracao, seed, usuario 1001): sobe a
# stack, confere a readiness e o login do admin semeado e derruba tudo com os
# volumes, inclusive em falha (depois de mostrar os logs). Projeto e porta
# proprios para nao derrubar a stack do compose-up.
#
# O access token do login passa pelo validador independente
# (scripts/validar_token.py, so PyJWT): JWKS buscado por HTTP, RS256, iss, aud,
# exp e type, como Billing e Execucao validam. O JWKS publico tem de trazer so
# os membros publicos e o Cache-Control de 10 min.
#
# O compose sobe a API com ENVIRONMENT=development (a guarda de producao
# recusa os segredos de demonstracao), entao o que a imagem faz em producao e
# conferido a parte: usuario numerico 1001 e ENVIRONMENT=production no
# config da imagem, e nenhum header `server` na resposta (uvicorn com
# --no-server-header, do entrypoint).
APP_IMAGE ?= pytstop-os-service:dev
SMOKE_PORT ?= 18000
SMOKE_URL := http://127.0.0.1:$(SMOKE_PORT)
SMOKE_COMPOSE := APP_PORT=$(SMOKE_PORT) APP_IMAGE=$(APP_IMAGE) \
	$(DOCKER_COMPOSE) -p pytstop-os-smoke

smoke:
	@status=0; \
	$(SMOKE_COMPOSE) up -d --build --wait \
	&& curl -fsS --max-time 5 $(SMOKE_URL)/api/v1/saude/pronto && echo \
	&& { [ "$$(docker image inspect -f '{{.Config.User}}' $(APP_IMAGE))" = 1001 ] \
		|| { echo "smoke: a imagem nao roda como o usuario 1001" >&2; false; }; } \
	&& { docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' \
			$(APP_IMAGE) | grep -qx 'ENVIRONMENT=production' \
		|| { echo "smoke: a imagem nao traz ENVIRONMENT=production" >&2; false; }; } \
	&& cabecalhos="$$(curl -fsS --max-time 5 -D - -o /dev/null $(SMOKE_URL)/api/v1/saude)" \
	&& { ! printf '%s' "$$cabecalhos" | grep -qi '^server:' \
		|| { echo "smoke: a resposta traz o header server" >&2; false; }; } \
	&& email="$$($(SMOKE_COMPOSE) exec -T api printenv ADMIN_EMAIL)" \
	&& senha="$$($(SMOKE_COMPOSE) exec -T api printenv ADMIN_PASSWORD)" \
	&& access="$$(curl -fsS --max-time 10 -X POST $(SMOKE_URL)/api/v1/autenticacao/login \
		-H 'Content-Type: application/json' \
		-d "{\"email\": \"$$email\", \"senha\": \"$$senha\"}" \
		| jq -er '.access_token')" \
	&& curl -fsS --max-time 5 $(SMOKE_URL)/.well-known/jwks.json \
		| jq -e '(.keys | length) > 0 and all(.keys[]; keys == ["alg","e","kid","kty","n","use"])' \
		>/dev/null \
	&& { curl -fsS --max-time 5 -D - -o /dev/null $(SMOKE_URL)/.well-known/jwks.json \
			| grep -qi '^cache-control: public, max-age=600' \
		|| { echo "smoke: o JWKS nao traz o Cache-Control de 10 min" >&2; false; }; } \
	&& printf '%s' "$$access" \
		| $(SMOKE_COMPOSE) exec -T api python scripts/validar_token.py http://127.0.0.1:8000 \
	&& echo "smoke ok: readiness 200, login validado pelo JWKS e imagem de producao" \
	|| status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE_COMPOSE) logs --no-color --tail=200; fi; \
	$(SMOKE_COMPOSE) down -v; \
	exit $$status

# Stack local do servico: API + PostgreSQL 16 (migracoes e admin seed no boot).
compose-up:
	$(DOCKER_COMPOSE) up -d --build --wait

compose-down:
	$(DOCKER_COMPOSE) down -v
