# Project Memory -- postech-sw-arch-p4-os-service

<!-- last-consolidated: 2026-10-06 -->

Add-only log of project-specific learnings. New entries go to the top of each section. Never edit historical entries -- add a contradicting entry above instead.

Updated by AI agents at task end per `postech-ai-helper/ai/canonical/task-end-review.md`. The `last-consolidated` marker above is updated only when `/consolidate-memory` runs, not on every append.

## Recent decisions

- 2026-10-06 - 401 uniforme (ADR-039): toda falha de credencial (token ausente, malformado, expirado, outro algoritmo, refresh no lugar de access, sem jti, revogado; login com senha errada ou e-mail desconhecido; refresh de usuario inexistente) responde a mensagem `Credenciais invalidas` (`FalhaAutenticacaoException.MENSAGEM`). O motivo vai so para o log (`authentication_failed`/`dominio_excecao_tratada`, chave `reason`). O gate continua com `{"detail": ...}` e os casos de uso com o envelope `erro`; papel insuficiente segue 403 - review deep do PR #2
- 2026-10-06 - Desativacao e erasure LGPD travam o cliente (`ClienteRepository.bloquear_cliente`, `FOR UPDATE`) e so entao checam OS ativa, na mesma transacao da escrita; `ClientePort.cliente_existe` da abertura de OS le o cliente com `FOR SHARE`. Os dois locks se excluem, entao nao ha janela check-then-act entre abrir OS e apagar/desativar o cliente. O erasure tambem sobe `versao` e `atualizado_em` das OS - review deep do PR #2
- 2026-10-06 - RBAC da OS: toda rota de `/api/v1/ordens-de-servico` e do atendente (admin herda); o mecanico nao tem rota no OS Service (ADR-039, celula vazia = nenhuma rota) e trabalha pela fila do Execution Service. A primeira versao do PR deixava o mecanico ler OS e historico (texto livre e, com a saga, `link_decisao`/`checkout_url`) - review deep do PR #2
- 2026-10-06 - Acompanhamento publico valida documento (digito verificador, inclusive CNPJ alfanumerico) e placa pelos VOs antes de qualquer acesso ao banco: invalido devolve o mesmo 404 do "nao encontrado" sem consulta (feedback da banca da fase 3). A leitura e o query service `ConsultaAcompanhamento` (projeta so status e timestamps), fora do repositorio do agregado; `CriarCliente` e `AdicionarVeiculo` tambem montam os VOs antes do repositorio - review deep do PR #2
- 2026-10-06 - Proveniencia do recorte passa a `p3 @ 08dcffe + fc06263` (p3 #32, normalizacao ASCII de CPF/CNPJ, mergeado depois do ponto de corte). CPF, CNPJ, Placa e o contrato `Documento` moram em `compartilhado.dominio` com um helper unico de normalizacao (`documento.py`): o acompanhamento publico da OS valida com eles sem importar o nucleo de `cliente_veiculo` (import-linter) - review deep do PR #2
- 2026-10-06 - Recorte do p3 @ 08dcffe (PR do recorte): contextos `compartilhado`, `cliente_veiculo`, `autenticacao`, `ordem_servico`; fora `catalogo_servicos`, `estoque`, `ui`, `relay`, `full-test`, `infra`, `k8s`, notificacao por e-mail (sem relay ninguem a dispara), DLQ admin da outbox, papel `CLIENTE` e `/minhas-ordens` (token da Lambda da fase 3), store Redis do rate limit (limite agregado e do Kong) e o JSON `/ordens-de-servico/metricas` (dashboards usam `/metrics`). Referencias `p3 #N` no codigo apontam para o repo do p3
- 2026-10-06 - Concorrencia da OS por lock otimista (`version_id_col` = `ordens_de_servico.versao`; `StaleDataError` -> `ConflitoDeConcorrenciaException` -> 409 no `salvar`), sem o `FOR UPDATE` do p3: e o *reread value* do brief secao 2. Um unico `StatusDaOrdemAlteradoEvent` (de, para, origem) por transicao + `OrdemAbertaEvent`; `motivo` e `descricao_problema` ficam fora do payload da outbox (texto livre, potencial PII)
- 2026-10-06 - Acompanhamento publico: `POST /api/v1/publico/acompanhamento` com placa+documento no corpo e 404 `{"detail": "Ordem nao encontrada"}` identico ao p3 (anti-enumeracao). O brief listava GET por engano (correcao do coordenador)
- 2026-10-06 - Idempotencia do consumidor: tabela `mensagens_processadas` (PK = id da mensagem, brief secao 4) no lugar do `processed_events (outbox_id, handler)` do relay do p3; a migracao 001 e a base limpa do servico (sem a cadeia 001-008 do p3)

- 2026-10-06 - Repo criado na fase 4 com branch protection na `main` desde o commit inicial (PR obrigatorio, admins incluidos, historico linear, conversas resolvidas, squash only). Motivo: a fase 3 perdeu ponto por commits diretos na main (29 no app, 11 na lambda) - spec `postech-sw-arch-p4/docs/superpowers/specs/2026-10-06-fase-4-bootstrap-design.md`

## Discovered conventions

- 2026-10-06 - CI vem dos templates do coordenador (`ci.yml`: lint, type-check, security, test, sonarqube, build; `security.yml`: pip-audit, gitleaks, trivy). Os nomes dos jobs sao os checks obrigatorios da branch protection: nao renomear. `make test` gera `coverage.xml`, `htmlcov/` e `reports/junit.xml` (o job `sonarqube` le o coverage.xml do artefato do `test`)

- 2026-10-06 - `uv run pytest` e o gate completo (addopts liga `--cov=src` e o `fail_under=90` do `.coveragerc`); para rodar subconjunto sem o gate use `--no-cov`. O schema da integracao vem de `alembic upgrade head` (nao `create_all`) e `test_migracao.py` compara migracao x metadata (`compare_metadata == []`): mudou mapping, escreva a migracao
- 2026-10-06 - `tests/fabricas.py` leva a OS a qualquer status so pelos fatos de dominio (`ordem_em(status)`); nunca monte o agregado por campos privados. O conftest de integracao detecta o socket do colima e exporta `DOCKER_HOST`/`TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE` sozinho

## Gotchas

- 2026-10-06 - O `\D`/`\d` do Python sao Unicode: sem `re.ASCII`, digitos arabe-indicos passavam na normalizacao de CPF/CNPJ e no formato da placa, e o `brutils` os aceita (no CNPJ, ate so no DV: `int()` le U+0668 como 8). O mesmo documento em outro alfabeto viraria outro `documento_hash` e furaria a UK. `compartilhado/dominio/documento.py` usa `re.ASCII` e exige resultado ASCII; a placa usa `fullmatch` (o `$` casa antes do `\n` final)
- 2026-10-06 - Falha de flush (`StaleDataError` do lock otimista) expira a instancia: ler `ordem.id` depois exige rollback (`PendingRollbackError`). Capture ids antes do `flush`
- 2026-10-06 - `structlog.testing.capture_logs` nao intercepta o `_log` de modulo ja cacheado por um `configurar_logging()` anterior (os testes de integracao rodam o lifespan antes dos unitarios): monkeypatch `<modulo>._log` com `structlog.get_logger()` no teste
- 2026-10-06 - `app.dependency_overrides[dep] = MagicMock` (a classe) faz o FastAPI ler `*args/**kw` da assinatura como query params obrigatorios (422 em tudo): use `lambda: MagicMock()`
- 2026-10-06 - `testcontainers.postgres` ficou deprecado no 4.15: importe de `testcontainers.community.postgres`. Starlette 1.7 avisa que `httpx` no TestClient esta deprecado em favor de `httpx2` (so warning)

- 2026-10-06 - PyJWT 2.13.x acumulou 27 advisories em out/2026: comecar em `pyjwt>=2.15.1` e `anyio>=4.15.1`. PyJWT 2.15 exige base64url valido na assinatura mesmo com `verify_signature=False` (JWT falso de teste precisa de segmento valido)

## Tech debt / TODO

- 2026-10-06 - LOW - O erasure faz UPDATE do `motivo` em `historico_status_ordem`, unica excecao a regra de historico so com insercao (ADR-037, RFC-004). O codigo documenta a excecao (`cliente_veiculo/infraestrutura/adapters.py`); falta registra-la no ADR/RFC do platform (repassado ao coordenador)
- 2026-10-06 - RESOLVIDO - CNPJ alfanumerico agora e aceito (normalizacao com `brutils.cnpj.remove_symbols` + `upper()`). Corrige a premissa da entrada abaixo: o `brutils` 2.5.0 do lock ja validava o formato novo; quem descartava as letras era a normalizacao `\D`
- 2026-10-06 - LOW - Eventos de log herdados do p3 estao em portugues (`dados_pessoais_exportados_via_admin`, `dominio_excecao_tratada`, ...) contra `canonical/language.md` (logs em ingles); eventos novos ja saem em ingles (`order_cancelled_via_api`). Renomear junto com as queries do Loki/dashboards quando a observabilidade entrar
- 2026-10-06 - MEDIUM - CNPJ alfanumerico (IN RFB 2.229/2024, emitido desde jul/2026) e rejeitado: a normalizacao herdada do p3 descarta letras antes do `brutils`. Avaliar suporte quando o brutils cobrir o formato novo
- 2026-10-06 - LOW - `StatusPagamento` so tem `SOLICITADO`: confirmado/recusado/expirado/estornado entram com os handlers da saga, junto do metodo de dominio que atualiza o resumo sem transicao de status
- 2026-10-06 - LOW - `/metrics` responde 307 para `/metrics/` (mount do `make_asgi_app`, herdado do p3); configurar o scrape com a barra final no PR de k8s/observabilidade

## Review lessons

- 2026-10-06 - Texto livre novo classificado como possivel PII (descricao do problema, motivos) tem de entrar no erasure LGPD do cliente na mesma transacao; o autor so tinha tirado o texto do payload da outbox - PR do recorte
- 2026-10-06 - Teste de listener `refresh` precisa mudar a linha por fora do ORM antes do `session.refresh`; comparar com o valor em memoria passa mesmo sem o listener (atributo nao mapeado) - PR do recorte
