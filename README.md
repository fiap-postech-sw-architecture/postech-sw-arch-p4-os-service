# PytStop fase 4: OS Service

[![CD](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service/actions/workflows/cd.yml/badge.svg?branch=main)](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service/actions/workflows/cd.yml)

Serviço de ordens de serviço (OS) do PytStop, o sistema de gestão da oficina mecânica. Abre e acompanha a OS, cadastra clientes e veículos (com LGPD) e emite o JWT dos usuários internos. Na saga de atendimento ele é o orquestrador: envia os comandos ao [Billing](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) e à [Execução](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) pelo RabbitMQ e conduz a OS da abertura à entrega, com compensações quando um passo falha. Banco próprio: PostgreSQL 16. Integrações: o RabbitMQ (outbox transacional, relay e consumidor idempotente, sem chamada HTTP a outro serviço), o Mailpit (e-mail ao cliente, por SMTP), o Jaeger, o Prometheus e o Loki da plataforma e o Kong, que é a borda. Faz parte da entrega da fase 4 do Tech Challenge, e o [índice da entrega](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/INDICE-DA-ENTREGA.md) liga os quatro repositórios, os requisitos e as evidências.

**Sumário**: [Como rodar](#como-rodar), [Como testar](#como-testar), [Cobertura](#cobertura), [API](#api), [Mensageria](#mensageria), [Arquitetura](#arquitetura), [Observabilidade](#observabilidade), [CI/CD](#cicd), [Segurança](#segurança) e [Decisões e rastreabilidade](#decisões-e-rastreabilidade).

**Documentos em `docs/`**: [arquitetura](docs/arquitetura.md), [saga](docs/saga.md), [mensageria, e-mail e LGPD](docs/mensageria.md), [API](docs/api/) e [exemplos da API](docs/api/exemplos.md), [como rodar e implantar](docs/como-rodar.md), [testes](docs/testes.md), [observabilidade](docs/observabilidade.md), [CI/CD](docs/ci-cd.md), [autenticação](docs/autenticacao.md) e [escopo e origem do código](docs/escopo-e-origem.md).

## Como rodar

Requisitos: Docker com Compose e [uv](https://docs.astral.sh/uv/).

```bash
make compose-up
curl -s localhost:8000/api/v1/saude
make compose-down
```

O `make compose-up` faz o build da imagem e sobe o PostgreSQL 16, o RabbitMQ, o Mailpit, as migrações, o admin de demonstração e os processos `relay`, `consumidor`, `prazos` e `notificador`. A API sobe em `http://localhost:8000` (porta em `APP_PORT`) e o Swagger em `http://localhost:8000/docs`. O `make compose-down` derruba tudo e apaga os volumes. O usuário de demonstração é `admin@pytstop.dev`, com a senha de `ADMIN_PASSWORD` no `docker-compose.yml`. O login, o primeiro `curl` autenticado e como rodar a API e os processos fora do compose estão em [docs/como-rodar.md](docs/como-rodar.md#como-rodar-local).

No kind, com o repositório [platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) clonado ao lado deste e com kind, kubectl, jq e curl instalados:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy
make kind-deploy
../postech-sw-arch-p4-platform/scripts/ci/smoke-servicos.sh os-service
```

O primeiro comando sobe o cluster, a plataforma e os Secrets do serviço, o segundo implanta a imagem deste commit e o terceiro roda o smoke. O Swagger fica em `https://localhost/os/docs`. Os manifestos, os overlays, a troca de senha e de chave e o rollback estão em [docs/como-rodar.md](docs/como-rodar.md#implantação).

## Como testar

```bash
uv sync --all-extras --frozen
make check
make smoke
```

O `uv sync --all-extras --frozen` é o mesmo passo do CI: o extra `otel` (o exportador OTLP) é preciso para o mypy do `make check`, que sem ele para em `src/compartilhado/infraestrutura/observability.py`.

- **`make check`**: roda os gates do CI: `uv.lock` em dia, ruff, import-linter, actionlint, mypy strict, bandit e o `pytest` com gate de cobertura de 95% e `diff-cover` de 90% no código novo.
- **Testes**: unitários, de contrato (a cópia dos contratos do platform é conferida byte a byte), de integração com PostgreSQL e RabbitMQ 4.3.6 efêmeros (testcontainers) e BDD de componente da saga (`tests/bdd/`). O BDD do fluxo completo, pela borda do kind, é do platform e roda no `deploy-kind` do CD.
- **`make smoke`**: prova a imagem pelo entrypoint real. Os dois pedem Docker, e o `make check` pede também o `kubectl` no PATH (os testes dos manifestos leem o `kubectl kustomize`).
- **Outros gates**: `make manifests`, `make audit`, `make lint-workflows` e `make docs-api`. O detalhe de cada um e dos testes da saga está em [docs/testes.md](docs/testes.md).

### Mutação

São 60 mutantes escritos à mão nos módulos críticos (`scripts/mutantes.toml`), e a suíte matou os 60 em 18/10/2026 (`make mutacao`). Não é gate de CI e não tem pontuação mínima. A tabela por área, o motivo de não usar o `mutmut` e os códigos de saída estão em [docs/testes.md](docs/testes.md#mutação).

## Cobertura

O gate é de 95% no total, com linhas e ramos somados (`fail_under` no `.coveragerc`), e o código novo precisa de 90% (`make diff-cover`), como pede o ADR-041 (RNF-050). As tabelas não são digitadas: o [`scripts/cobertura_resumo.py`](scripts/cobertura_resumo.py) as escreve a partir do `coverage.xml` de um run do CI.

<!-- cobertura:inicio -->
**99,63% no gate** (linhas e ramos somados): 6.282 linhas, 6.266 cobertas (99,7%), e 930 ramos, 919 cobertos (98,8%), medidos no [run 39952450189 do CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service/actions/runs/39952450189). Tabelas geradas por `scripts/cobertura_resumo.py` a partir do `coverage.xml` desse run.

| Contexto | Linhas | Cobertas | Cobertura |
|---|---:|---:|---:|
| `src` (processos) | 240 | 235 | 97,9% |
| `src/autenticacao` | 569 | 569 | 100,0% |
| `src/cliente_veiculo` | 909 | 908 | 99,9% |
| `src/compartilhado` | 1.861 | 1.851 | 99,5% |
| `src/ordem_servico` | 2.703 | 2.703 | 100,0% |
| **total** | 6.282 | 6.266 | 99,7% |

| Camada | Linhas | Cobertas | Cobertura |
|---|---:|---:|---:|
| `dominio` | 943 | 940 | 99,7% |
| `aplicacao` | 1.790 | 1.790 | 100,0% |
| `infraestrutura` | 2.492 | 2.488 | 99,8% |
| `interfaces` | 817 | 813 | 99,5% |

<!-- cobertura:fim -->

Para refazer depois de mudar `src/`: `make cobertura-readme RUN=<id do run do CI>`. Oscilação: com o mesmo código, o número varia cerca de 0,02 ponto, por um ramo de `encryption.py` que só roda quando as threads do teste de concorrência se cruzam ([como ler a diferença](docs/testes.md#cobertura)). O gate, o `diff-cover` e o que fica fora da medição estão em [docs/testes.md](docs/testes.md#cobertura).

## API

- **Swagger**: `http://localhost:8000/docs` (`https://localhost/os/docs` no kind).
- **OpenAPI e Postman (RNF-052)**: [`docs/api/openapi.json`](docs/api/openapi.json) e a [coleção Postman](docs/api/os-service.postman_collection.json), com um exemplo de corpo e de resposta por rota, ficam em [`docs/api/`](docs/api/). O `make docs-api` os gera de novo a partir do app, e o `make test` reprova os arquivos atrás do código. A coleção do fluxo completo, que roda com newman, é do platform.
- **Exemplos**: um `curl` por rota, agrupado por fluxo, em [docs/api/exemplos.md](docs/api/exemplos.md).
- **Erros**: os erros saem no envelope `{"erro": {"codigo", "mensagem", "id_requisicao"}}`, menos o 422 de validação de schema (`{detail, id_requisicao}`, o formato dos três serviços) e a readiness 503 (`{"status": "indisponivel"}`). O OpenAPI declara os erros que cada rota devolve, menos o 500 ([detalhe](docs/testes.md#qualidade)).
- **Rotas principais**:
  - OS: `POST` e `GET /api/v1/ordens-de-servico`, `GET /{id}` (com a etapa da saga), `GET /{id}/historico`, `POST /{id}/cancelamento` (202 ou 409) e `POST /{id}/entrega`.
  - Saga (admin): `GET /api/v1/sagas/{ordem_id}` e `POST /api/v1/sagas/{ordem_id}/compensacao`.
  - Clientes e veículos, com as rotas da LGPD: `/api/v1/clientes`.
  - Autenticação: `/api/v1/autenticacao/*` e `GET /.well-known/jwks.json`. Acompanhamento público: `POST /api/v1/publico/acompanhamento`.
  - Operação: `GET /api/v1/saude`, `GET /api/v1/saude/pronto`, `GET /api/v1/ordens-de-servico/metricas` (admin) e `GET /metrics`.

## Mensageria

O OS orquestra a saga pelo RabbitMQ da plataforma ([ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md), [RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) seção 5). Publica 11 comandos para o Billing e a Execução e consome 23 eventos deles (13 do Billing e 10 da Execução), todos no envelope do contrato. A entrega é pelo menos uma vez e o efeito acontece uma vez (RN-028).

- **Outbox e relay**: o caso de uso grava o comando na tabela `outbox`, no mesmo commit do efeito. O `relay` (`python -m src.relay`) o publica com *publisher confirms* e `mandatory`. A falha volta depois de 1, 4, 16 e 64 s e, na quinta, a linha vira `dead`.
- **Consumidor** (`python -m src.consumidor`): lê `os.eventos` uma mensagem por vez, com ack manual, e comita o efeito, os comandos e o registro em `mensagens_processadas` de uma vez. A idempotência é a chave primária dessa tabela: o mesmo `id` recebe ack sem efeito.
- **Retry e DLQ**: o erro transitório volta pela fila de retry (1, 5, 15, 60 e 300 s). A sexta falha, o erro permanente e a mensagem fora do contrato vão para `os.eventos.dlq` (7 dias), de onde o redrive as trata de novo.
- **Contratos**: `contratos/` é cópia dos arquivos do platform no SHA de `contratos/ORIGEM`: o [AsyncAPI](contratos/asyncapi.yaml), um JSON Schema por mensagem e os exemplos. Um teste compara cada arquivo byte a byte.
- Catálogo das mensagens, envelope, variáveis de ambiente, comportamento com o broker fora e o e-mail ao cliente: [docs/mensageria.md](docs/mensageria.md) e [docs/arquitetura.md](docs/arquitetura.md#5-mensageria).

### Evento fora de ordem

O consumidor entrega cada evento ao orquestrador, que o classifica pela etapa da saga antes de tocar no domínio (RFC-004 seção 4.5). O evento de etapa já passada ou repetido é ignorado com log e nunca vai para a DLQ. O adiantado volta pela fila de retry nas cinco cópias (381 s ao todo) e, se a saga não o alcançar, a sexta falha vai para a DLQ, com alerta.

A recusa permanente vai para a DLQ com o motivo em código no log `message rejected to dlq` (como `saga_inexistente`, `ordem_encerrada`, `transicao_invalida`, `relogio_defasado` e `erro_no_handler`), com alerta. A tabela completa, com um exemplo e o destino de cada caso, está em [docs/saga.md](docs/saga.md#evento-fora-de-ordem).

### Operação do e-mail

O e-mail ao cliente não passa pelo broker: a transição da OS grava um pedido na tabela `notificacoes`, no mesmo commit, e o processo `notificador` o envia por SMTP ao Mailpit. A linha que esgota as cinco tentativas fica `dead`, sem métrica nem alerta. A consulta das linhas `dead` e o `UPDATE` que as devolve à fila estão em [docs/mensageria.md](docs/mensageria.md#operação-do-e-mail).

## Arquitetura

DDD com arquitetura em camadas (onion) em cada contexto: `interfaces` → `aplicacao` → `dominio`, com a `infraestrutura` implementando as portas. O mapeamento do SQLAlchemy é imperativo, e as classes do domínio são dataclasses puras. Os contextos são `autenticacao`, `cliente_veiculo` e `ordem_servico`, mais o núcleo técnico `compartilhado`, sem regra de negócio. As regras de dependência são contratos do import-linter (`make lint-arch`): `dominio` e `aplicacao` não importam a `infraestrutura`, o `pika` nem o `opentelemetry`, a saga não importa o SQLAlchemy, e um contexto não importa o núcleo de outro.

Cinco processos saem da mesma imagem, e só o comando muda: a API, o `relay`, o `consumidor`, o `prazos` e o `notificador`, mais o Job de migração. A saga é um *process manager* (`ordem_servico/aplicacao/saga/`): estado e regras sem I/O, persistido como o agregado `Saga` na tabela `sagas`, na mesma transação da OS e da outbox.

As responsabilidades, as camadas, o modelo completo do banco, os fluxos passo a passo, a mensageria, a segurança e a LGPD estão em [docs/arquitetura.md](docs/arquitetura.md) (RNF-051). A saga tem o fluxo, as [compensações e o cancelamento](docs/saga.md#compensações-e-cancelamento) e os [prazos e a retomada](docs/saga.md#prazos-falha-na-compensação-e-retomada) em [docs/saga.md](docs/saga.md). As decisões globais estão na [RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) e nos [ADR-034 a ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura/adr/fase4) do platform.

### Por que orquestração

- **O tamanho da saga.** São nove passos, T1 a T9 (as transações locais de cada serviço, da abertura à finalização), em três serviços, mais a entrega local (T10). Há esperas humanas (mecânico e cliente), um provedor externo (Mercado Pago) e seis compensações. Um dos passos é o *pivot*, o ponto sem retorno (RN-029): o início da execução física, depois do qual não há compensação.
- **Por que não coreografia.** A Aula 02 de SAGA Pattern, citando Richardson, recomenda a coreografia para sagas simples, e esta não é. Com coreografia, os passos ficariam espalhados nos três serviços, sem um lugar que diga em que etapa a OS está. Prazos, ordem das compensações e o pivot exigiriam que cada serviço assinasse eventos dos outros dois, com risco de ciclo.
- **Por que no OS, e não num quarto serviço.** Como nos exemplos do material, o orquestrador fica no serviço que inicia a saga: o fluxo inteiro fica num lugar só, testável sem broker, e etapa e status mudam na mesma transação local.
- **Os dois contras da aula, contidos.** O orquestrador só conhece a ordem dos passos, as compensações e os prazos: preço, validade e pagamento são dos participantes, que respondem a comandos sem conhecer a saga. E ele não é ponto único de falha: o estado fica no banco e as mensagens, em filas duráveis. O consumidor roda com uma réplica; se o pod cai, o Kubernetes o recria, a mensagem sem confirmação volta à fila e a reentrega não repete efeito (RN-028). As alternativas descartadas (coreografia, orquestrador como quarto serviço, motor de workflow) estão no ADR-035.

## Observabilidade

Segue o [ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md).

- **Métricas**: cada processo serve o `/metrics`. A API o serve na porta 8000, fora da borda, e o `relay`, o `consumidor`, o `prazos` e o `notificador`, na 9100. O Prometheus do platform raspa cada pod. Os gauges por consulta (saga, outbox e OS por status) saem de um coletor da API e ficam ausentes, nunca zerados, quando a leitura falha.
- **Traces**: os spans do OpenTelemetry vão ao Jaeger com `OTEL_ENABLED=true`. A requisição de abertura é a raiz do trace da saga, que atravessa os três serviços pelas mensagens.
- **Logs**: JSON com `correlation_id` (o id da OS), `trace_id` e `span_id`, sem dado pessoal. O Promtail do platform os leva ao Loki.
- **No platform**: o catálogo das métricas dos três serviços, os painéis do Grafana e os alertas estão na [observabilidade do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#catálogo-de-métricas).
- A tabela das séries, os spans, o que não é exportado, os logs e como ver no compose e no kind estão em [docs/observabilidade.md](docs/observabilidade.md).

## CI/CD

- **CI e segurança**: o `ci.yml` roda `lint`, `type-check`, `security`, `test`, `sonarqube` (com quality gate versionado) e `build` (imagem e `make smoke`), e o `security.yml` roda `pip-audit`, `gitleaks` e `trivy`, em todo PR e toda segunda-feira. Esses nove nomes são os checks obrigatórios da `main`, que é protegida: toda mudança entra por PR, com squash.
- **CD**: o [`cd.yml`](.github/workflows/cd.yml) roda a cada push na `main` e no `workflow_dispatch`. Com o CI e a segurança verdes, o job `cd` chama o workflow reutilizável do platform ([ADR-042](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/042-cicd-e-deploy-kubernetes.md)): o `image` publica a imagem no GHCR, com SBOM e proveniência, e o `deploy-kind` implanta a plataforma e os três serviços num kind efêmero do runner e roda o smoke e o E2E da saga. O `deploy-remote` é opcional e vem desligado.
- **Dependabot**: uma vez por semana, para o uv, as actions e as imagens do `Dockerfile` ([`dependabot.yml`](.github/dependabot.yml)).
- Jobs, permissões, artefatos, como reexecutar e os limites conhecidos: [docs/ci-cd.md](docs/ci-cd.md).

### O que o `deploy-kind` faz, na ordem

1. Escolhe os commits do platform e dos dois serviços vizinhos (o do dispatch, senão o do último CD verde, senão a `main`) e faz o checkout de cada um.
2. Baixa o artefato `imagem`, o tar da imagem que o `image` acabou de publicar: o E2E testa a mesma imagem que vai ao GHCR.
3. Sobe o kind e a plataforma (`scripts/ci/deploy-kind.sh` do platform), com os Secrets gerados no run.
4. Implanta os três serviços no overlay `kind-ci` e espera o banco, o Job de migração e os Deployments.
5. Roda o smoke de cada namespace.
6. Roda o E2E BDD da saga pela borda.
7. Sempre, mesmo com falha: o resumo de minutos e memória, a varredura de segredos nos relatórios, o upload do artefato `e2e-kind` e, em falha, o diagnóstico do cluster.

O job só fica verde se o E2E passar. O texto completo está em [docs/ci-cd.md](docs/ci-cd.md#o-que-o-deploy-kind-faz-na-ordem).

### Deploy remoto (opcional)

Leva o mesmo digest que passou no kind a um cluster persistente que o dono do repositório configura, sem ligar o CD a nenhum provedor. Vem desligado: com `DEPLOY_REMOTO_HABILITADO` diferente de `true`, o `deploy-remote` fica de fora e o `remote-skipped` registra um aviso. O caller não tem credencial de provedor, `environment` nem `id-token`. Os passos, as guardas e a receita de acesso estão em [docs/ci-cd.md](docs/ci-cd.md#deploy-remoto-opcional) e em [Nuvem opcional](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/nuvem-opcional.md), no platform.

## Segurança

- **Autenticação**: o OS é o único emissor de JWT ([ADR-039](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/039-autenticacao-entre-servicos.md)). O access token vale 15 min e o refresh, 7 dias, assinados com RS256 e chave RSA de 2048 bits ou mais. O JWKS público (`GET /.well-known/jwks.json`) deixa o Billing e a Execução validarem o token localmente. Toda falha de credencial responde 401 `NAO_AUTENTICADO`, com a mesma mensagem e o motivo só no log. Papel válido sem permissão responde 403 `ACESSO_NEGADO`. Fora de `development` e `test`, o boot recusa a chave de demonstração, a ausente e a fraca.
- **Papéis**: `admin`, `atendente` (o `admin` herda) e `mecanico`, que não tem rota de negócio no OS e trabalha pelas filas da Execução. O SlowAPI limita por IP as rotas sem token e as de credencial (5 chamadas por minuto no login e no registro), e o resto é limitado na borda pelo Kong.
- **LGPD**: o documento do cliente fica cifrado (Fernet), com consentimento, exportação e eliminação dos dados pessoais. A eliminação anonimiza o cliente, os veículos, o texto livre das OS e a placa na outbox e na Execução. Os logs mascaram CPF, CNPJ, e-mail e telefone, e os spans não levam o texto das exceções.
- **Infraestrutura**: pods sem root, NetworkPolicy, papéis do banco separados e nenhum segredo nos manifestos. O CI roda `gitleaks` no histórico e `trivy` na imagem.
- Detalhe em [docs/autenticacao.md](docs/autenticacao.md), [docs/mensageria.md](docs/mensageria.md#eliminação-de-dados-lgpd) e [docs/arquitetura.md](docs/arquitetura.md#6-segurança-e-lgpd).

### Rotação da chave

A chave nova só assina depois que todos os pods e consumidores a conhecem, por isso a troca tem etapas:

1. Publicar a nova: a pública dela entra em `JWT_PREVIOUS_PUBLIC_KEY` e a atual segue assinando. Espere o rollout e os 10 min do cache do JWKS dos consumidores.
2. Trocar: a nova passa a assinar, e a pública da antiga fica em `JWT_PREVIOUS_PUBLIC_KEY`.
3. Aposentar a antiga: só depois de 7 dias, a validade do refresh.

Se a chave atual vazar, assina-se com a nova e esvazia-se `JWT_PREVIOUS_PUBLIC_KEY`, o que derruba as sessões. As chaves nascem fora do repositório e vão direto para o Secret. A tabela das etapas está em [docs/autenticacao.md](docs/autenticacao.md#rotação-da-chave), e os comandos de Secret e a troca de senha de um papel do banco, em [docs/como-rodar.md](docs/como-rodar.md#troca-de-senha-e-de-chave).

## Decisões e rastreabilidade

Este repositório é um dos quatro da entrega da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT). Nessa fase o PytStop, sistema de gestão de oficina mecânica das fases anteriores, virou microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

- **ADRs e RFC-004**: o serviço segue a [RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) e os [ADR-034 a ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura/adr/fase4), citados no código como `RFC-004 secao N` e `ADR-0NN`. A tabela de decisões deste serviço, com o ADR e o código de cada uma, está em [docs/arquitetura.md](docs/arquitetura.md#8-decisões).
- **Requisitos**: a [matriz de rastreabilidade](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/matriz-de-rastreabilidade.md) do platform liga cada RF, RNF e RN aos PRs e às evidências, e o [documento de entrega](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/fase4/entrega-fase-4.md) reúne o conjunto. Neste README: a justificativa da saga (RNF-037) em [Por que orquestração](#por-que-orquestração), a cobertura (RNF-050) em [Cobertura](#cobertura), a arquitetura (RNF-051) em [Arquitetura](#arquitetura) e a API (RNF-052) em [API](#api).
- **Origem do código e escopo**: o código parte de um recorte da `main` do PytStop da fase 3 (`postech-sw-arch-p3`), e o escopo do serviço está em [docs/escopo-e-origem.md](docs/escopo-e-origem.md).

### Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.
