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
# os membros publicos e o Cache-Control de exatamente 10 min. Como controle
# negativo, o mesmo token com a assinatura adulterada tem de ser recusado: um
# validador que nao confere a assinatura aceitaria os dois.
#
# O log do uvicorn sai em JSON desde a primeira linha ("Started server
# process"): o `configurar_logging` roda na fabrica do app, que o uvicorn importa
# antes do servidor subir. O smoke falha se aparecer linha de texto puro dele.
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
	&& { curl -fsS --max-time 5 $(SMOKE_URL)/.well-known/jwks.json \
			| jq -e '(.keys | length) > 0 and all(.keys[]; keys == ["alg","e","kid","kty","n","use"])' \
			>/dev/null \
		|| { echo "smoke: o JWKS esta vazio ou traz membro fora de alg, e, kid, kty, n e use" >&2; false; }; } \
	&& { curl -fsS --max-time 5 -D - -o /dev/null $(SMOKE_URL)/.well-known/jwks.json \
			| tr -d '\r' | grep -qix 'cache-control: public, max-age=600' \
		|| { echo "smoke: o JWKS nao traz o Cache-Control de 10 min" >&2; false; }; } \
	&& printf '%s' "$$access" \
		| $(SMOKE_COMPOSE) exec -T api python scripts/validar_token.py http://127.0.0.1:8000 \
	&& adulterado="$$(printf '%s' "$$access" | awk -F. '{ c = substr($$3, 1, 1); \
		printf "%s.%s.%s%s", $$1, $$2, (c == "A" ? "B" : "A"), substr($$3, 2) }')" \
	&& erro="$$(printf '%s' "$$adulterado" \
		| $(SMOKE_COMPOSE) exec -T api python scripts/validar_token.py http://127.0.0.1:8000 \
			2>&1 >/dev/null; true)" \
	&& { printf '%s' "$$erro" | grep -q 'Signature verification failed' \
		|| { echo "smoke: o validador nao recusou o token de assinatura adulterada" >&2; false; }; } \
	&& logs="$$($(SMOKE_COMPOSE) logs --no-color --no-log-prefix api)" \
	&& { printf '%s\n' "$$logs" | grep -q '"event": "Started server process' \
		|| { echo "smoke: o log de boot do uvicorn nao saiu em JSON" >&2; false; }; } \
	&& { ! printf '%s\n' "$$logs" | grep -Eq '^(INFO|WARNING|ERROR|CRITICAL): ' \
		|| { echo "smoke: o uvicorn escreveu log em texto puro" >&2; false; }; } \
	&& echo "smoke ok: readiness 200, login validado pelo JWKS (e a assinatura adulterada recusada), log em JSON e imagem de producao" \
	|| status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE_COMPOSE) logs --no-color --tail=200; fi; \
	$(SMOKE_COMPOSE) down -v; \
	exit $$status

# Stack local do servico: API + PostgreSQL 16 (migracoes e admin seed no boot).
compose-up:
	$(DOCKER_COMPOSE) up -d --build --wait

compose-down:
	$(DOCKER_COMPOSE) down -v
