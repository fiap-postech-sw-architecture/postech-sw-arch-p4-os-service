# PytStop fase 4: OS Service

Serviço de ordens de serviço: abertura, status e histórico da OS, cadastro de clientes e veículos (com LGPD), usuários internos (emissão de JWT) e orquestração da saga de atendimento. Banco próprio: PostgreSQL 16.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

**Status:** em construção. Este README será substituído pela documentação completa do serviço (arquitetura, fluxos, exemplos de API, testes e cobertura, pipelines).

**Proveniência:** recorte da `main` do PytStop fase 3 (`postech-sw-arch-p3` @ [`08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) + [`fc06263`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/fc06263d1f99747145e5a354b817905c59637ebd), a normalização ASCII de CPF/CNPJ do p3 #32), só com os contextos deste serviço (`compartilhado`, `cliente_veiculo`, `autenticacao`, `ordem_servico`). Referências `p3 #N` no código apontam para issues e PRs daquele repositório; IDs como `RF-018`, `ADR-020` e `TD-017` são da numeração contínua das fases anteriores ([requisitos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/requisitos), [ADRs](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/arquitetura/adr) e [dívida técnica](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/tech-debt) do p3); a fase 4 começa em RF-028 e ADR-034.

**Arquitetura da fase 4:** a RFC-004 e os ADR-034 a ADR-043, citados nos comentários do código como `RFC-004 secao N` e `ADR-0NN`, ficam em [`docs/arquitetura` do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura).

## O que já existe

- OS da fase 4: `RECEBIDA → EM_DIAGNOSTICO → AGUARDANDO_APROVACAO → AGUARDANDO_PAGAMENTO → AGUARDANDO_EXECUCAO → EM_EXECUCAO → FINALIZADA → ENTREGUE`, com `CANCELADA` antes do início da execução. A OS guarda o histórico de mudanças de status, o resumo do orçamento e do pagamento (que vivem no Billing) e uma versão para lock otimista (escrita concorrente responde 409).
- API: `POST/GET /api/v1/ordens-de-servico`, `GET /{id}`, `GET /{id}/historico`, `POST /{id}/cancelamento`, `POST /{id}/entrega`, clientes e veículos com rotas LGPD, autenticação (`/api/v1/autenticacao/*` e o JWKS em `GET /.well-known/jwks.json`), acompanhamento público (`POST /api/v1/publico/acompanhamento`, placa e documento no corpo), `GET /api/v1/saude` (liveness), `GET /api/v1/saude/pronto` (readiness: 503 se o banco não responder em 2 s) e `GET /metrics` (com `API_METRICS_ENABLED=true`, ligado no compose). Swagger em `/docs`.
- Outbox transacional: todo evento da OS é gravado na tabela `outbox` no mesmo commit da mudança.
- Ainda não: saga, mensageria (RabbitMQ) e manifestos Kubernetes, desenhados na RFC-004.

## Autenticação

O OS Service é o único emissor de tokens (ADR-039). `POST /api/v1/autenticacao/login` devolve um access token de 15 min e um refresh de 7 dias; `/refresh` troca o refresh por um par novo (uso único); `/logout` revoga o `jti` (identificador do token) do access do cabeçalho e, se o `refresh_token` vier no corpo, o do refresh também: sem ele, o refresh continua valendo até expirar. A revogação só vale neste serviço: nos outros, o limite é a expiração do access.

- **Assinatura:** JWT (JSON Web Token) assinado com RS256 (RSA com SHA-256), com chave RSA de 2048 bits ou mais. Claims: `iss=pytstop-os-service`, `aud=pytstop`, `sub` (UUID do usuário), `papel` (só no access: `admin`, `atendente` ou `mecanico`), `type` (`access` ou `refresh`), `jti`, `iat` e `exp`, sem e-mail.
- **JWKS (JSON Web Key Set):** `GET /.well-known/jwks.json`, público, com `Cache-Control: public, max-age=600` e limite de 60 chamadas por minuto por IP. Traz só a parte pública de cada chave (`kty`, `use`, `alg`, `kid`, `n`, `e`); o `kid` (identificador da chave, repetido no cabeçalho de cada token) é a impressão digital (*thumbprint*, RFC 7638) da chave pública.

  ```bash
  curl -s localhost:8000/.well-known/jwks.json | jq
  ```

- **Quem valida:** Billing e Execução validam o access token localmente, com o JWKS que leem de `JWKS_URL`, sem chamar o OS a cada requisição. Conferem a assinatura RS256 (algoritmo fixo), `iss`, `aud` e `exp` (com 10 s de tolerância entre relógios), `type=access`, `sub` (UUID) e `papel` (um dos três acima); o cache do JWKS é de cada consumidor (README de [Billing](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) e de [Execução](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service)). O `scripts/validar_token.py` é o modelo dessa conferência, só com o PyJWT, e o `make smoke` o roda com o token do login.
- **Erros:** toda falha de credencial responde 401 `NAO_AUTENTICADO` com a mensagem `Credencial ausente, invalida ou expirada`, a mesma dos três serviços; o motivo (assinatura, `aud`, `iss`, claim ausente, `iat` no futuro, token malformado, expirado, revogado) fica só no log. Um refresh reapresentado depois de usado, ou de revogado no logout, gera também o evento `refresh_reuse_detected`. Papel válido sem permissão responde 403 `ACESSO_NEGADO`.

| Variável | Uso |
|---|---|
| `JWT_PRIVATE_KEY` | chave privada RSA em PEM; no cluster vem de um Secret, no compose e no `.env.example` é uma chave de demonstração |
| `JWT_PREVIOUS_PUBLIC_KEY` | opcional: a chave pública que entra (etapa 1 da rotação) ou que sai (etapa 2), só publicada no JWKS e aceita na validação, nunca usada para assinar |
| `JWT_EXPIRATION_MINUTES`, `JWT_REFRESH_EXPIRATION_MINUTES` | validade do access (15, no máximo 60) e do refresh (10080, no máximo 43200 = 30 dias) |

Fora de `development` e `test`, o boot aborta se a chave estiver ausente, ilegível, não for RSA, tiver menos de 2048 bits ou for a de demonstração (como atual ou anterior), ou se uma validade estiver fora dos limites.

### Rotação da chave

A chave privada não entra no repositório nem na imagem (`*.pem` e `*.key` são ignorados pelo git e pelo Docker); gere as chaves num diretório temporário e leve-as direto para o Secret:

```bash
chaves="$(mktemp -d)"   # fora do repositório; apague depois de atualizar o Secret
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$chaves/nova.pem"
openssl pkey -in "$chaves/nova.pem" -pubout     # pública da nova
# A pública da atual sai da chave que está no Secret, gravada no mesmo diretório:
openssl pkey -in "$chaves/atual.pem" -pubout
```

A chave nova só assina depois que todos os pods e consumidores a conhecem; por isso a troca tem etapas. Com mais de uma réplica, um pod antigo recusaria o token de um pod novo que assinasse com uma chave que ele ainda não publica. Chame de K1 a chave atual e de K2 a nova:

| Etapa | `JWT_PRIVATE_KEY` | `JWT_PREVIOUS_PUBLIC_KEY` | Depois |
|---|---|---|---|
| 1. Publicar a nova | K1 | pública da K2 | Espere o rollout e os 10 min do cache dos consumidores; confira com `curl -s localhost:8000/.well-known/jwks.json \| jq '.keys[].kid'` (a primeira chave da lista é a que assina) |
| 2. Trocar | K2 | pública da K1 | Os tokens emitidos com a K1 seguem válidos |
| 3. Aposentar a antiga | K2 | vazio | Só depois de 7 dias, a validade do refresh: a partir daqui os tokens da K1 deixam de valer |
| Emergência (K1 comprometida) | K2 | vazio | Derruba as sessões: o OS recusa a K1 assim que o rollout termina e Billing e Execução, quando renovarem o JWKS (até 10 min; até 1 h com o OS fora do ar). É o único jeito de uma chave vazada deixar de forjar tokens `admin` aceitos pelos três serviços |

## Como rodar local

Pré-requisitos: Docker com Compose e [uv](https://docs.astral.sh/uv/).

```bash
make compose-up      # build da imagem, PostgreSQL 16, migrações e admin de demonstração
curl -s localhost:8000/api/v1/saude
make compose-down    # derruba e apaga o volume
```

A API sobe em `http://localhost:8000` (porta configurável com `APP_PORT`) e o Swagger em `http://localhost:8000/docs`. O usuário de demonstração é `admin@pytstop.dev` com a senha de `ADMIN_PASSWORD` no `docker-compose.yml` (valores só de dev; o boot com `ENVIRONMENT=production` recusa esses literais).

```bash
TOKEN=$(curl -s localhost:8000/api/v1/autenticacao/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@pytstop.dev","senha":"admin-demo-os-2026"}' | jq -r .access_token)
curl -s localhost:8000/api/v1/ordens-de-servico -H "Authorization: Bearer $TOKEN"
```

Fora do compose (API no host, com `--reload`), o `.env.example` lista todas as variáveis lidas pelo serviço, com os mesmos valores de demonstração:

```bash
docker run -d --name os-postgres -p 127.0.0.1:5432:5432 \
  -e POSTGRES_DB=os -e POSTGRES_USER=pytstop -e POSTGRES_PASSWORD=pytstop postgres:16
cp .env.example .env && set -a && . ./.env && set +a
uv run alembic upgrade head && uv run python scripts/seed_admin.py
uv run python -m src.main   # http://127.0.0.1:8000
```

## Qualidade

```bash
make check   # uv.lock em dia, ruff (lint e formato), import-linter, mypy strict, bandit e pytest
make audit   # pip-audit das dependências de runtime (com os extras da imagem)
make smoke   # imagem pelo entrypoint real: readiness, login do admin semeado validado pelo JWKS
             # (e a assinatura adulterada recusada), log de boot em JSON e a imagem de produção
             # (usuário 1001, ENVIRONMENT=production, sem header server), depois down -v
```

`make test` (ou `uv run pytest`) roda os testes unitários e os de integração contra um PostgreSQL efêmero (testcontainers, Docker necessário) com gate de cobertura de 90% (`.coveragerc`). O schema dos testes de integração é criado pela própria migração Alembic.

No GitHub, o workflow `CI` (`.github/workflows/ci.yml`) roda os mesmos gates em todo PR, publica `coverage.xml`, `htmlcov/` e o JUnit como artefato com o resumo de cobertura por pacote no summary, passa o SonarQube com quality gate versionado (`.sonar/quality-gate.json`), builda a imagem e roda o `make smoke`. O workflow `Security` roda pip-audit (dependências de runtime com os extras da imagem), gitleaks e trivy (imagem), em todo PR e toda segunda-feira.

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.
