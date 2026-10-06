# Gates do OS Service. `make check` espelha o CI: lint, formato, contratos de
# camada, tipos, seguranca e testes (unitarios + integracao com testcontainers)
# com gate de cobertura (.coveragerc).
PY := uv run
PY_PATHS := src/ scripts/ migrations/
PY_PATHS_COM_TESTS := $(PY_PATHS) tests/

GIT_SHA  := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_DATE := $(shell git show -s --format=%cI HEAD 2>/dev/null || echo unknown)
DOCKER_COMPOSE := GIT_SHA=$(GIT_SHA) GIT_DATE=$(GIT_DATE) docker compose

.PHONY: lint format lint-arch typecheck security test check compose-up compose-down

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

# Suite completa com cobertura (addopts do pyproject liga --cov=src). O
# conftest de integracao acha sozinho o socket do colima no macOS.
test:
	$(PY) pytest

check: lint lint-arch typecheck security test
	@echo "All checks passed"

# Stack local do servico: API + PostgreSQL 16 (migracoes e admin seed no boot).
compose-up:
	$(DOCKER_COMPOSE) up -d --build --wait

compose-down:
	$(DOCKER_COMPOSE) down -v
