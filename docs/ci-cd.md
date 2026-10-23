# CI/CD

Este documento detalha a seção [CI/CD](../README.md#cicd) do README.

## CI e segurança

No GitHub, o workflow `CI` (`.github/workflows/ci.yml`) roda os mesmos gates em todo PR, publica `coverage.xml`, `htmlcov/` e o JUnit como artefato com o resumo de cobertura por pacote no summary, roda o `make diff-cover` no PR (com o relatório dele no summary, logo abaixo), passa o SonarQube com quality gate versionado (`.sonar/quality-gate.json`), valida os manifestos (`make manifests`), builda a imagem e roda o `make smoke`. O workflow `Security` roda pip-audit (dependências de runtime com os extras da imagem), gitleaks (o histórico do commit testado, inclusive o que um commit seguinte apagou) e trivy (imagem), em todo PR e toda segunda-feira. O resultado do pip-audit e o do trivy aparecem no log enquanto rodam e também no summary do job (`scripts/ci/registra-no-summary.sh`, que mantém o código de saída da ferramenta); o resumo da cobertura mostra o número do gate (linhas e ramos), o que o `fail_under` compara.

## Dependências automatizadas

O Dependabot ([`.github/dependabot.yml`](../.github/dependabot.yml)) abre pull requests para três ecossistemas: `pyproject.toml` e `uv.lock` (uv), as actions dos workflows e as imagens base do `Dockerfile`.

- **Agenda:** uma vez por semana, às segundas às 06:00 (São Paulo), com no máximo 3 PRs abertos por ecossistema.
- **Grupos:** patch e minor de cada ecossistema chegam juntos num PR. Major abre PR próprio, para ser lido com o changelog.
- **OpenTelemetry:** as bibliotecas `opentelemetry-*` sobem juntas no grupo de patch e minor do uv. O SDK fixa `opentelemetry-api` e `opentelemetry-semantic-conventions` na mesma versão e cada instrumentação fixa a sua, então subir só uma parte não resolve. Um grupo só delas ficaria vazio, porque o Dependabot o trata como menos específico que o de patch e minor. A major da família fica fora, atrás do teto do `pyproject.toml`: `<1.46` no SDK e no exportador, `<0.67` nas instrumentações. O teto barra também a próxima minor, porque o `observability.py` troca `_status` e `_events` de um span, atributos privados do SDK que uma minor nova pode mudar.
- **FastAPI:** minor e major ficam de fora (`ignore` no bloco do uv, que o teste do arquivo confere) e sobem à mão; o patch sobe pelo grupo. O FastAPI é 0.x e traz mudança incompatível em minor: oito das treze minors entre a 0.144 e a 0.156 têm Breaking Changes nas notas de release (a 0.151, por exemplo, passou a distinguir `null` de campo omitido no corpo JSON). A subida fica segurada até a entrega da fase 4 (17/11/2026), com os três serviços na mesma versão, e é reavaliada depois. O pydantic não entra no `ignore`: a minor dele segue pelo grupo e pelo CI.
- **Commit e rótulos:** prefixo `chore(deps)`, ou `ci` nas actions. O arquivo não declara `labels`: os PRs levam os rótulos padrão do Dependabot (`dependencies` e o do ecossistema), que ele cria se faltarem. Declarar rótulo que não existe no repositório só faz o Dependabot comentar isso em cada PR.
- **Checks:** cada PR passa pelos mesmos checks do CI e da varredura de segurança, inclusive o pip-audit, o trivy e o gate de cobertura.
- **Python:** no bloco do Docker, major e minor do Python ficam de fora. A versão sobe à mão, de uma vez, em `.python-version`, `UV_PYTHON` e `python-version` dos workflows, `requires-python` e `python_version` (mypy) do `pyproject.toml`, `sonar.python.version` do `sonar-project.properties` e nas duas imagens do `Dockerfile`. Sem isso, o 3.14 → 3.15 chegaria como "minor" e trocaria só a imagem do runtime. O patch vem do rebuild da tag móvel `python:3.14-slim`, sem PR; hoje o bloco do Docker não abre PR, porque as duas linhas `FROM` são `python:3.14-slim` e a única atualização possível está no `ignore`.

Depois do congelamento do código em 06/11/2026, o `open-pull-requests-limit` de cada bloco passa de 3 para 0 (e o valor esperado em `tests/unitarios/test_cd.py` com ele). O 0 desliga só as atualizações de versão: os PRs de segurança do Dependabot, habilitados no repositório, não entram no limite e seguem abrindo. Sobem à mão, porque o Dependabot não os enxerga ou a versão está fixada de propósito: o uv do `Dockerfile` (entra por `COPY --from`, e o Dependabot só lê as linhas `FROM`), o PostgreSQL e o `postgres-exporter` de `k8s/`, o PostgreSQL, o RabbitMQ e o Mailpit do compose, o PostgreSQL e o RabbitMQ dos testes de integração (`tests/conftest.py` e `tests/integracao/broker.py`), a imagem do SonarQube do `ci.yml` e o scanner (`scripts/sonar/analisar.sh`), o trivy, o gitleaks, o pip-audit, o diff-cover, o kubeconform e o actionlint.

## CD

O workflow `CD` ([`cd.yml`](../.github/workflows/cd.yml), [ADR-042](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/042-cicd-e-deploy-kubernetes.md)) implanta o serviço depois do merge. **O ambiente Kubernetes do deploy automatizado é o kind efêmero do runner**: o `deploy-kind` sobe a plataforma e os três serviços num cluster que some com o job e valida o deploy com o E2E (teste ponta a ponta) da saga; o que fica são os relatórios, no artefato `e2e-kind`, por 90 dias. Um deploy remoto, num cluster persistente, é opcional e vem desligado ([abaixo](#deploy-remoto-opcional)). O caller conserva os gatilhos, a concorrência e os gates locais; o job `cd` chama o [workflow reutilizável do Platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/.github/workflows/cd-servico.yml), fixado no commit `3f7cca2eb555a038b0d2780058ef0e692f93ee8a` (S). Esse pin escolhe a definição do workflow; o input `sha_platform` escolhe separadamente os scripts e manifests usados no deploy (P).

O primeiro CD deste caller na `main` foi aprovado no [run 39233375064](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service/actions/runs/39233375064), sobre `5e4ed2a1c2d74bdc65cc207c3f5a208b4ea672ca`: CI, segurança, imagem com SBOM e digest conferido, implantação no kind, smoke, E2E, varredura e `remote-skipped` passaram.

A cada push na `main`, depois do merge de cada PR, e no `workflow_dispatch`:

1. `ci` e `security-scan` chamam o `ci.yml` e o `security.yml` como workflows reutilizáveis: o CI e a varredura de segurança não rodam duas vezes, e o PR continua rodando os dois pelo próprio gatilho.
2. `cd`, depois dos dois gates verdes, chama o reutilizável com `servico: os-service` e os quatro inputs de SHA. Nele, `image` constrói a imagem uma vez e a publica no GHCR (GitHub Container Registry), com o SHA do commit como tag.
3. `deploy-kind` carrega essa imagem num kind, roda o smoke de cada namespace e o E2E, e guarda os relatórios.
4. `deploy-remote`, se estiver ligado, implanta no cluster remoto o mesmo digest que passou no kind (o digest é o `sha256:...` que identifica o conteúdo da imagem). Desligado, o `remote-skipped` só registra o aviso.

Só roda um CD por vez no repositório, sem cancelar o que está em curso (`concurrency` por repositório). O GitHub guarda um único run pendente por grupo: um push ou um dispatch seguinte substitui o pendente, e o commit do run trocado fica sem CD. O run que sobra testa o estado mais novo da `main`.

### Jobs

| Job | O que faz | Depende de | Prazo | Permissões do job |
|---|---|---|---|---|
| `ci` | Chama o `ci.yml`: `lint`, `type-check`, `security`, `test`, `sonarqube` e `build` | | | `contents: read` |
| `security-scan` | Chama o `security.yml`: `pip-audit`, `gitleaks` e `trivy` | | | `contents: read` |
| `cd` | Chama o reutilizável do Platform pelo SHA S, sem herdar segredos | `ci`, `security-scan` | definidos nos jobs chamados | `contents: read`, `actions: read`, `packages: write` |
| `image` | Só roda na `main` (o primeiro passo reprova qualquer outra ref). Constrói a imagem uma vez e a publica em `ghcr.io/<organização>/pytstop-os-service:<sha do commit>`, com SBOM (a lista de pacotes da imagem) em SPDX (formato padrão de SBOM) e proveniência mínima (`provenance: mode=min`). Confere o digest com um `docker pull`, salva o tar `linux/amd64` e o SBOM | gates do caller `cd` | 30 min | `contents: read`, `packages: write` |
| `deploy-kind` | Sobe a plataforma e os três serviços num kind do runner, roda o smoke de cada namespace e o E2E da saga (abaixo) | `image` | 45 min | `contents: read`, `actions: read` |
| `deploy-remote` | Opcional, desligado: implanta no cluster remoto configurado o mesmo digest que passou no kind. Só roda na `main` e com `DEPLOY_REMOTO_HABILITADO` igual a `true` ([abaixo](#deploy-remoto-opcional)) | `image`, `deploy-kind` | 20 min | `contents: read`, `packages: read` |
| `remote-skipped` | O oposto: com a variável desligada, só registra o aviso de que o deploy remoto não rodou | `deploy-kind` | 5 min | nenhuma |

Os quatro jobs de imagem e deploy da tabela pertencem ao reutilizável do Platform. O caller concede a união de permissões, e cada job chamado a reduz conforme a tabela. O CD só começa com o CI e a varredura do commit verdes. Os jobs rodam no `ubuntu-24.04`, fixo, e o único que escreve pacote é o `image`. Nenhum job pede token de identidade, e o arquivo não guarda credencial de provedor nenhum. O `actions: read` do `deploy-kind` serve à consulta dos runs do `cd.yml` dos vizinhos. Como os dois repositórios são públicos, a leitura talvez funcione sem a permissão, mas só um run na `main` prova isso, então ela fica.

O `workflow_dispatch` aceita qualquer ref, e o `image` publica no GHCR a imagem que o deploy remoto implanta. Por isso o primeiro passo do `image` reprova fora de `refs/heads/main` (`images are published from main only`), e o `deploy-remote` repete a condição. O CI e a varredura de segurança rodam antes, então o erro de ref aparece só depois deles.

### O que o `deploy-kind` faz, na ordem

1. Escolhe os commits do platform e dos dois serviços vizinhos ([abaixo](#commits-do-platform-e-dos-vizinhos)) e faz o checkout de cada um, mais o deste repositório, sem guardar a credencial do GitHub.
2. Baixa o artefato `imagem`, o tar da imagem que o `image` acabou de publicar: o E2E testa a mesma imagem que vai ao GHCR.
3. `scripts/ci/deploy-kind.sh` do platform: kind e kubectl com versão e sha256 fixados, `make kind-up` e `make deploy` no overlay `kind-ci`, que gera os Secrets no run. Nada de segredo vem do GitHub.
4. `scripts/ci/implantar-servicos.sh --overlay kind-ci`: este serviço entra pelo tar (`kind load image-archive`); o Billing e a Execução são construídos do código no commit escolhido. Aplica o overlay `kind-ci` de cada serviço e espera o banco, o Job de migração e os Deployments, nessa ordem. O contrato está no [README do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#contrato-com-os-serviços).
5. `scripts/ci/smoke-servicos.sh`: por namespace, o Job completo, os rollouts, a saúde e o Swagger pela borda, o `/metrics` fora da borda, o `up` de cada pod e o `pg_up` ou `mongodb_up` no Prometheus e a NetworkPolicy do banco. A tabela `Service | Stage | Result` vai para o summary e o passo reprova nomeando o serviço.
6. `make -C platform e2e`: o BDD (cenários em Gherkin) da saga pela borda, com o resumo e os relatórios.
7. Sempre, mesmo com falha: o resumo de minutos por etapa e memória (`scripts/medir-kind.sh`) no summary, a varredura de segredos nos relatórios do E2E (`python -m e2e.varredura` do platform, que apaga o arquivo com segredo e reprova o passo) e o upload do artefato `e2e-kind`. Com falha ou cancelamento (inclusive o do `timeout-minutes`): `scripts/ci/diagnostico.sh`, que despeja pods, eventos e logs de todos os namespaces com o cluster ainda de pé.

O job fica verde só se o E2E passar. O resumo e o diagnóstico só rodam se o checkout do platform aconteceu: com um `sha_*` inválido o primeiro passo reprova antes de qualquer checkout, e o único vermelho é o dele.

### Commits do platform e dos vizinhos

O primeiro passo do `deploy-kind` escolhe o commit de cada um dos três repositórios e escreve a tabela no summary, com a origem de cada commit. Este serviço é sempre o commit do run.

1. O commit do input do dispatch, se houver.
2. Senão, o commit do último run verde do `cd.yml` daquele repositório, na `main` e disparado por push (`gh api repos/<organização>/<repositório>/actions/workflows/cd.yml/runs` com `branch=main`, `event=push` e `status=success`). O platform e os vizinhos entram assim, e não pelo último commit da `main`: uma `main` quebrada de um vizinho não derruba o CD dos outros, e o vermelho fica no CD de quem a quebrou.
3. Senão, a `main` HEAD do repositório, com um `::warning::` nomeando o repositório. É o caso de um repositório sem nenhum CD verde, o que inclui o repositório sem `cd.yml` (a API responde 404). Qualquer outra falha da API, como o limite de requisições, reprova o passo, em vez de cair na `main` calado.

### Imagem, SBOM e proveniência

- **Tag e digest:** a tag é o SHA do commit. O `image` confere que o digest que o `docker pull` imprime é o do build e usa esse digest no deploy remoto.
- **SBOM:** o `docker buildx imagetools inspect --format '{{ json .SBOM }}'` devolve o documento SPDX dentro de `{"SPDX": ...}`; o passo extrai o documento, reprova se ele não lista nenhum pacote e sobe o resultado como artefato.
- **Proveniência:** o atestado de proveniência (como a imagem foi construída) fica no índice da imagem, no registro público. É o mínimo (`mode=min`) e sem o payload do evento do push: o buildx o grava na proveniência em qualquer modo, com nome e e-mail dos autores, e só o `provenance-add-gha=false` do `setup-buildx-action` o tira.
- **Imagens do build fixadas por digest:** o BuildKit, o gerador do SBOM e a imagem do actionlint do Makefile. As tags delas andam; os comentários do reutilizável e do Makefile dizem como subir.
- **Visibilidade:** o pacote `pytstop-os-service` é público por herança, e nada neste repositório o configura: ele nasce vinculado a este repositório, que é público, pela label `org.opencontainers.image.source` do Dockerfile. Se a visibilidade mudar, o cluster do `deploy-remote` precisa de um segredo de pull do `ghcr.io`.
- **Gate no commit, não no artefato:** o `ci / build`, o `security-scan / trivy` e o `image` constroem o Dockerfile cada um no seu job, e o Dockerfile faz `apt-get upgrade` sobre `python:3.14-slim`, uma tag que anda. O digest publicado pode ter pacotes do sistema diferentes dos que passaram no smoke do CI e no trivy. O E2E do `deploy-kind` testa o tar do `image`, a mesma imagem que foi ao GHCR.
- **Registro antes do E2E:** a imagem vai ao GHCR antes do smoke e do E2E, então o registro tem imagens de commits cujo `deploy-kind` ficou vermelho. A tag não é um selo de CD verde; o deploy remoto implanta por digest, e só depois do kind verde.

### Artefatos e summary

| Artefato | Conteúdo | Retenção |
|---|---|---|
| `imagem` | `imagem.tar`, a imagem `linux/amd64` sem os manifestos de atestado (com eles, o `kind load` falha por digest ausente no image store do containerd) | 1 dia |
| `sbom-os-service` | O documento SPDX 2.3 da imagem publicada, com os pacotes dela | 90 dias |
| `e2e-kind` | `platform/reports/e2e/` depois da varredura de segredos: `resumo.md`, `junit.xml`, `cucumber.json`, `cenarios.json` e o trace do caminho feliz | 90 dias |

São os três artefatos do run. O registro do build (`*.dockerbuild`) que o `docker/build-push-action` anexaria sozinho está desligado (`DOCKER_BUILD_RECORD_UPLOAD`).

O summary do run traz, em ordem: os commits usados, a imagem (referência, digest e número de pacotes do SBOM), as tabelas do smoke e do E2E e os minutos por etapa e a memória do nó do kind.

### Reexecutar e acompanhar

Dispare o `workflow_dispatch` sempre com `--ref main`. Ele aceita `sha_platform`, `sha_billing_service` e `sha_execution_service`, todos opcionais, cada um com 40 caracteres hexadecimais minúsculos (um SHA abreviado, em maiúsculas, um nome de ramo ou qualquer outra coisa reprova o passo antes de qualquer checkout). O `sha_os_service` mantém as mesmas entradas dos CDs dos outros serviços e só aceita vazio ou o commit do run: este serviço é sempre o commit que iniciou o workflow. Os valores entram nos passos só por variável de ambiente, nunca dentro de um script.

```bash
BILLING=$(gh api repos/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/commits/main --jq .sha)
gh workflow run cd.yml --ref main -f sha_billing_service="$BILLING"
gh run watch "$(gh run list --workflow cd.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

O `gh run watch` sem id abre um seletor interativo; o comando acima segue o último run. Antes de reexecutar, quatro fatos:

1. O artefato `imagem` vive 1 dia. Depois disso, o "Re-run failed jobs" reprova no download dele: rode `gh workflow run cd.yml --ref main` no lugar do re-run.
2. O re-run do job `image` publica de novo a mesma tag (o SHA do commit) com outro digest. O digest que vale é o da tentativa mais recente, o do summary dela.
3. Um dispatch pendente é trocado por um push seguinte, e um push pendente, por um dispatch: o grupo de `concurrency` guarda um pendente só.
4. Um `sha_*` inválido só reprova no primeiro passo do `deploy-kind`, depois do CI e da imagem (cerca de 10 minutos). A mensagem nomeia a entrada.

### Dois serviços que mudam contrato juntos

O CD de cada serviço usa os vizinhos no último CD verde deles. Se o OS e o Billing mudam o mesmo contrato de forma incompatível, o CD de cada um sobe a versão nova com a antiga do outro e fica vermelho, e o último verde de nenhum dos dois avança. Para sair do impasse:

1. Prove a combinação nova com o `workflow_dispatch`: no OS, `sha_billing_service` com o commit novo do Billing; no Billing, `sha_os_service` com o commit novo do OS. Com o deploy remoto ligado, o mesmo run implanta o digest dela.
2. Esse run não vira "último CD verde", porque a consulta olha só os runs disparados por push.
3. Um dos dois precisa então de um push verde com o vizinho antigo, ou seja, de um PR que aceite os dois formatos. Depois o outro muda, e o primeiro retira o formato antigo (expandir e contrair, [ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md)).

Entrar em duas etapas desde o começo evita o impasse.

### Deploy remoto (opcional)

O deploy remoto leva o mesmo digest que passou no kind para um cluster persistente que o dono do repositório configura, sem ligar o CD a nenhum provedor. Ele substitui o `deploy-k3s` do ADR-042, e o `DEPLOY_REMOTO_HABILITADO` ocupa o lugar do `K3S_HABILITADO`: a emenda do ADR-042, da RFC-004 e da análise de lacunas já está no platform ([ADR-042](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/042-cicd-e-deploy-kubernetes.md)). Fica desligado: com `DEPLOY_REMOTO_HABILITADO` diferente de `true` (ou ausente), o `deploy-remote` fica de fora e o `remote-skipped` registra um `::notice::` no run.

O `deploy-remote` do reutilizável só roda na `main`. Antes do checkout ele compara o commit P do Platform usado no kind com a `main` do Platform (`compare main...<sha>`) e só aceita `identical` ou `behind`. Depois faz checkout do Platform em `platform/` e deste serviço em `servico/`, sem persistir credenciais, e executa `../platform/scripts/ci/deploy-remoto.sh` a partir de `servico/`, com `--contexto`, `--overlay`, `--servico os-service` e `--imagem <referência>@<digest>`.

O gancho permanece desligado: não há credencial de provedor, `environment`, `secrets: inherit` ou `id-token` no caller. Uma habilitação futura exige alterar deliberadamente o reutilizável no Platform, seus testes e o pin S deste caller; acrescentar passos ao job `cd` que usa `uses` não é permitido. A receita e os requisitos de acesso estão em [Nuvem opcional](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/docs/operacao/nuvem-opcional.md).

A plataforma precisa estar implantada antes. O script remoto implanta o overlay deste serviço, por digest do GHCR, na ordem banco, Job e Deployments; o smoke é executado pelo `deploy-kind` no runner. Se o commit P não tiver o script executável, o job reprova. O job remoto pede só `contents: read` e `packages: read`; se o pacote do GHCR deixar de ser público, o cluster precisa de um segredo de pull do `ghcr.io`.

### Conferir o workflow sem empurrar

O `cd.yml` só roda no GitHub, depois do merge ou no dispatch. Antes disso: `make lint-workflows` (actionlint e shellcheck), `uv run pytest tests/unitarios/test_cd.py` e `make check`. O contrato local confere os três jobs do caller, os nove checks locais, o needs que exige CI e segurança verdes, o pin S, serviço e quatro inputs, permissões, gatilhos, concorrência, Dependabot na raiz e actions locais. Os testes de actions por SHA e tag continuam disponíveis com o marcador `rede` (`git ls-remote`).

Os testes que executam os passos Bash de escolha dos commits, validação de inputs, guarda da main, SBOM, digest, varredura e deploy remoto ficam uma única vez no [teste compartilhado do Platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/tests/test_cd_servico_workflow.py). A atualização desse reutilizável exige seu próprio gate e revisão; o caller só muda de versão ao mudar S. O Dependabot `github-actions` cobre a raiz, inclusive essa referência; até haver prova de um bump reconhecido, a atualização do pin é supervisionada.
