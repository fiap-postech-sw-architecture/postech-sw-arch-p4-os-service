# Como rodar e implantar

Este documento detalha a seção [Como rodar](../README.md#como-rodar) do README.

## Como rodar local

Pré-requisitos: Docker com Compose e [uv](https://docs.astral.sh/uv/); o `make test` pede também o `kubectl` no PATH (os testes dos manifestos leem o `kubectl kustomize`). O `make compose-up` faz o build da imagem e sobe o PostgreSQL 16, o RabbitMQ, o Mailpit, as migrações, o admin de demonstração, o relay, o consumidor, o `prazos` e o notificador; o `make compose-down` derruba tudo e apaga os volumes.

```bash
make compose-up
curl -s localhost:8000/api/v1/saude
make compose-down
```

A API sobe em `http://localhost:8000` (porta configurável com `APP_PORT`) e o Swagger em `http://localhost:8000/docs`. O RabbitMQ do compose carrega a topologia de `contratos/rabbitmq/` e cria os usuários dos três serviços; o console fica em `http://localhost:15674` (usuário `admin`, senha de demonstração em `contratos/rabbitmq/rabbitmq-admin.json`) e o AMQP em `localhost:5674`, fora das portas do compose do platform. O usuário de demonstração é `admin@pytstop.dev` com a senha de `ADMIN_PASSWORD` no `docker-compose.yml` (valores só de dev; o boot com `ENVIRONMENT=production` recusa esses literais).

```bash
TOKEN=$(curl -s localhost:8000/api/v1/autenticacao/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@pytstop.dev","senha":"admin-demo-os-2026"}' | jq -r .access_token)
curl -s localhost:8000/api/v1/ordens-de-servico -H "Authorization: Bearer $TOKEN"
```

Fora do compose (API, relay, consumidor, `prazos` e notificador no host), o `.env.example` lista todas as variáveis lidas pelo serviço, com os mesmos valores de demonstração; o RabbitMQ pode ser o do compose (`docker compose up -d rabbitmq rabbitmq-usuarios`, AMQP em `localhost:5674`) e o SMTP do `.env.example` aponta para o Mailpit do compose (`docker compose up -d mailpit`). A API sobe em `http://127.0.0.1:8000`; o relay, o consumidor, o `prazos` e o notificador rodam cada um em outro terminal, com o mesmo `.env` e um `METRICS_PORT` diferente do dos outros:

```bash
docker run -d --name os-postgres -p 127.0.0.1:5432:5432 \
  -e POSTGRES_DB=os -e POSTGRES_USER=pytstop -e POSTGRES_PASSWORD=pytstop postgres:16
cp .env.example .env && set -a && . ./.env && set +a
uv run alembic upgrade head && uv run python scripts/seed_admin.py
uv run python -m src.main
uv run python -m src.relay
uv run python -m src.consumidor
uv run python -m src.prazos
uv run python -m src.notificador
```

## Implantação

Os manifestos Kubernetes ficam em `k8s/`: `base/` e três overlays, `kind` (local), `kind-ci` (o CD, com uma réplica só da API) e `k3s` (um cluster persistente genérico, o overlay padrão do [deploy remoto](ci-cd.md#deploy-remoto-opcional), que vem desligado). Tudo vai para o namespace `pytstop-os`, com Pod Security `restricted`. Nenhum Secret está nos manifestos: os do serviço nascem no `make deploy` do platform ([segredos gerados](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#segredos-gerados)) e entram por `secretKeyRef` ([ADR-042](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/042-cicd-e-deploy-kubernetes.md)).

| Objeto | O que é |
|---|---|
| Deployment e HPA `os-service-api` | A API na porta 8000, com o `/metrics` na mesma porta; o HPA, por CPU (70%), vai de 1 a 2 réplicas no `kind`, a 1 no `kind-ci` e a 3 no `k3s` |
| Deployments `os-service-relay`, `os-service-consumidor`, `os-service-prazos` e `os-service-notificador` | Relay, consumidor, prazos da saga e notificador dos e-mails, uma réplica cada, com o `/metrics` na 9100. O notificador só recebe a senha do banco e o `SMTP_*` do ConfigMap (o Mailpit da plataforma, na 1025): sem `RABBITMQ_URL` e sem `ENCRYPTION_KEY` |
| StatefulSet e Service headless `os-postgres` | PostgreSQL 16.15 com volume de 1Gi (5Gi no `k3s`) e o `postgres_exporter` como sidecar, na 9187 |
| Job `os-migracao` | `alembic upgrade head` e a semente do admin, antes dos Deployments |
| Service `os-service` | O endereço interno da API, o do `JWKS_URL` de Billing e Execução |
| `borda.yaml` | Os Services e Ingress do Kong, os mesmos objetos do [exemplo do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/k8s/exemplos/borda-os-service.yaml) com o cabeçalho deste repositório: `/os/api/v1`, `/os/docs`, `/os/openapi.json` e o JWKS pela borda, e `/os/api/v1/admin` e `/os/metrics` respondendo 404 sem chegar ao serviço |
| NetworkPolicy | Nega toda entrada no namespace e libera só a API para o Kong, o Prometheus e as APIs de Billing e Execução (que leem o JWKS), relay, consumidor, prazos e notificador para o Prometheus, o PostgreSQL para os pods do namespace e o exporter para o Prometheus. A saída do notificador também é restrita (`notificador-saida`): só o DNS, o PostgreSQL do namespace e o Mailpit da plataforma na 1025. É a única política de saída do serviço, por critério: o notificador é o único processo com destino de rede de terceiro configurável (`SMTP_HOST`) e escreve em todo o banco; os demais, o `prazos` inclusive, têm destinos fixos (o PostgreSQL do namespace e o RabbitMQ e o Jaeger da plataforma, e o `prazos` usa os três: exporta o span `saga prazo`). Trocar o `SMTP_HOST` exige ajustar essa política. A regra de entrada no Mailpit é do platform |

Todo pod roda sem root (uid 1001; 999 no banco e 65534 no exporter), sem escalar privilégio, sem capability, com seccomp `RuntimeDefault`, raiz somente leitura (`/tmp` em `emptyDir`) e sem token de ServiceAccount. O `make manifests` valida os três overlays com o kubeconform, contra os schemas do Kubernetes 1.35 e com `Secret` reprovado, e com o `trivy config`, sem achado HIGH nem CRITICAL; o job `build` do CI o roda em todo PR.

### Implantar no kind

Pré-requisitos: Docker, kind, kubectl, jq e curl (o `implantar-servicos.sh` e o smoke usam os cinco; o `make manifests` e o `make test` precisam só do kubectl e do Docker) e o platform clonado ao lado deste repositório. O primeiro comando sobe o cluster, a plataforma e os Secrets do serviço; o segundo implanta a imagem deste commit, com o Job de migração e os rollouts; o terceiro roda o smoke:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy
make kind-deploy
../postech-sw-arch-p4-platform/scripts/ci/smoke-servicos.sh os-service
```

O `make kind-deploy` chama o `scripts/ci/implantar-servicos.sh` do platform, o mesmo do CD (o contrato de implantação dos serviços está no [README do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#contrato-com-os-serviços)): ele constrói a imagem `pytstop-os-service` com o commit como tag, a carrega no kind, aplica o overlay `kind` com essa imagem e espera o banco, o Job e cada Deployment. `PLATFORM_DIR=<caminho>` aponta outro clone do platform (`PLATFORM`, o nome antigo, também vale) e `KIND_OVERLAY=kind-ci` troca o overlay, os mesmos nomes do Billing e da Execução. O smoke confere pela borda a saúde em 200 e o `/metrics` em 404, o `up` de cada pod no Prometheus e a NetworkPolicy barrando o banco a quem vem de outro namespace. O Swagger fica em `https://localhost/os/docs` (com o certificado padrão do Kong).

O overlay `kind` também pode ser aplicado sem o script: a tag `dev` dele é a de uma imagem que o script não constrói, então `docker build -t pytstop-os-service:dev .` e `kind load docker-image pytstop-os-service:dev --name pytstop-p4` vêm antes. O script apaga o Job `os-migracao` antes de cada aplicação (um Job é imutável); à mão, faça o mesmo.

- **Configuração:** o ConfigMap `os-service` sai do `configMapGenerator` de `k8s/base/kustomization.yaml`, com `ENVIRONMENT=production` também no kind (as guardas de boot valem com os segredos gerados), `ROOT_PATH=/os` (o Swagger acha o `/os/openapi.json` atrás do Kong) e a exportação OTLP para o Jaeger. Um teste (`tests/unitarios/test_contrato_dos_manifestos.py`) confere que o código lê cada variável do ConfigMap.
- **Confiança no `X-Forwarded-For`:** o `TRUSTED_PROXIES` leva o CIDR de pods do overlay (`10.244.0.0/16` no kind, `10.42.0.0/16` no k3s), e o `X-Forwarded-For` do Kong vale como IP do cliente no limite de login. Vale também o dos outros pods que a NetworkPolicy deixa chegar à API, o Prometheus e as APIs de Billing e Execução, que não são clientes.
- **Senhas:** nenhuma senha está nos manifestos. A `DATABASE_URL` se monta no pod com a senha do Secret (`postgresql://os_app:$(POSTGRES_APP_PASSWORD)@os-postgres...`), e o `kubectl describe` mostra o molde, não a senha. O gitleaks tem regras para URL com senha, com um teste que as faz reprovar (`scripts/ci/testa-gitleaks.sh`), e o `make manifests` reprova um `PASSWORD` literal no `papeis.sql`. Os testes de integração se conectam com a `DATABASE_URL` do próprio manifesto, e um teste do render dos três overlays confere o Secret e a chave de cada processo.
- **Papéis do banco:** criados pelo `k8s/base/papeis.sql` no primeiro init do volume. `postgres`, o superusuário, só inicializa; `os`, dono do banco e das tabelas (DDL), é o do Job; `os_app`, só DML nas tabelas do dono, é o da API, do relay, do consumidor e do notificador (este lê `clientes.contato` em claro, por projeção, sem a chave de cifragem), e lê a `alembic_version` sem escrevê-la (o `migrations/env.py` revoga a escrita a cada upgrade); `os_exporter`, com `pg_monitor`, é o do exporter. Nenhum tem `CREATEROLE`, `CREATEDB`, replicação nem `BYPASSRLS`, e o `PUBLIC` perde o `CONNECT` e o `TEMPORARY` dos bancos `os` e `postgres`.
- **Log do servidor:** o psql troca o `\getenv` pela senha antes de enviar o `CREATE ROLE`, e um `log_statement` ligado, a linha `STATEMENT` de um erro ou o log por duração ou por amostragem a gravariam no log do servidor. O script desliga esses caminhos na própria sessão.
- **Acesso ao banco:** o loopback e o socket também pedem senha (`POSTGRES_INITDB_ARGS` com `scram-sha-256`), e o exporter não entra como `postgres`. Para um `psql` no container, a senha vem do ambiente dele, não de argumento: `kubectl --context kind-pytstop-p4 -n pytstop-os exec -it os-postgres-0 -c postgres -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U postgres -d os'`.
- **Init do banco que falha:** a imagem roda o init uma vez por volume. Se ele falhar no meio, o volume fica sem os papéis, o StatefulSet fica pronto (o `pg_isready` não autentica) e o Job falha na autenticação do `os`. Apague o PVC e o pod do banco para o init rodar de novo, o que apaga os dados: `kubectl --context kind-pytstop-p4 -n pytstop-os delete pvc dados-os-postgres-0 --wait=false` e `kubectl --context kind-pytstop-p4 -n pytstop-os delete pod os-postgres-0`.
- **Migração e rollout:** o script do platform aplica tudo de uma vez e espera o banco, o Job (com o papel `os`) e os Deployments, nessa ordem. Os pods dos processos sobem junto e esperam, no initContainer `aguarda-migracao` (`python -m src.banco aguardar`), o banco chegar à última revisão do Alembic da imagem, com o motivo da espera no log dele (a revisão em que o banco está, nenhuma ou o erro de conexão): ele só lê o `alembic_version`, com o papel `os_app`, e não prepara o banco. No rolling update, os pods novos esperam a migração da versão deles enquanto os antigos seguem servindo.
- **Rollback:** `kubectl rollout undo`, sem descer migração (migrações em expandir e contrair, ADR-042). Com o banco adiante da imagem, a revisão que ela não conhece também libera o pod.
- **Processo `prazos`:** `k8s/base/prazos.yaml`, com o mesmo molde do relay e do consumidor (mesma imagem, `python -m src.prazos`, o initContainer `aguarda-migracao`, o Secret do banco e o do RabbitMQ, e sem a `ENCRYPTION_KEY`, que só a API recebe). Uma réplica: duas não se atropelam (`SKIP LOCKED`), mas uma basta para o intervalo de 30 s. `SAGA_PRAZO_RESPOSTA_SEGUNDOS`, `SAGA_MAX_REENVIOS` e `PRAZOS_INTERVALO_SEGUNDOS` vêm do ConfigMap `os-service`. No SIGTERM o `prazos` termina a saga em curso, sem começar outra, e sai; os 30 s do encerramento cobrem a transação dela. A compensação só vai para o cluster junto com ele ([Implantação e DLQ](#implantação-e-dlq)).
- **Sondas:** a API tem liveness em `/api/v1/saude` e readiness em `/api/v1/saude/pronto`; relay, consumidor, `prazos` e notificador, sem curl na imagem, têm liveness pela idade do heartbeat (até 90 s; 120 s no `prazos`, onde a startup é a mesma) e readiness pelo arquivo de pronto (o do notificador é o banco respondendo; o do `prazos`, o broker de pé), os de `/tmp` (seção [Observabilidade, saúde e encerramento](mensageria.md#observabilidade-saúde-e-encerramento)). A sonda de startup dá 1 min ao boot de cada processo e 2 min ao primeiro init do banco.
- **Imagens de terceiros:** o job `trivy` do workflow Security reprova a imagem do serviço com HIGH ou CRITICAL corrigíveis. As duas que o serviço põe no cluster, `postgres:16.15` e `postgres-exporter:v0.20.1`, só têm relatório no summary do job, sem reprovar, porque a correção depende de um build novo do fornecedor (`scripts/ci/relatorio-trivy-terceiros.sh`; a falha da varredura em si, como um pull recusado, vira aviso). O risco aceito, medido com o trivy 0.72.0: a `postgres:16.15` tem 1 CRITICAL e 24 HIGH, todos no binário Go `gosu`, que o pod, já como uid 999, não executa, e a chave de teste `ssl-cert-snakeoil` do Debian, que o PostgreSQL daqui não usa (sem SSL); o exporter tem 16 HIGH na biblioteca padrão do Go do próprio binário, e sua porta 9187 só chega ao Prometheus. A política é subir a versão quando o fornecedor publicar um build corrigido, junto da tabela de versões do platform.

### Implantação e DLQ

- **Ordem.** A migração primeiro (a `004` cria a tabela `notificacoes`, de que o `notificador` depende); depois a API, o relay, o consumidor e o `prazos`, juntos (no cluster, o mesmo `kubectl apply -k` aplica tudo, e os pods esperam a migração no `initContainer`) ou o consumidor antes: a compensação que a API inicia é respondida na `os.eventos`, e uma réplica do consumidor de versão anterior, sem tratador para a falha de negócio e para a resposta de compensação, a recusa com o motivo `sem_tratador_nesta_versao`. Durante o rollout, as respostas que uma réplica antiga consumir vão para a DLQ. O `notificador` sobe por último, depois que a API e o consumidor já gravam o pedido.
- **Janela do rolling update e o e-mail.** Durante o rollout o Deployment do consumidor tem a réplica antiga e a nova consumindo `os.eventos` juntas, e só a nova grava o pedido de e-mail na transação. A transição que a réplica antiga tratar nunca gera e-mail, e nada a reconcilia depois: é aceito (o e-mail é complementar e a OS segue o fluxo). O mesmo vale para o `kubectl rollout undo` do consumidor. O pedido que a API já nova gravar com modelo, status ou motivo que um `notificador` antigo não conhece fica `pendente` até a versão nova subir, sem gastar tentativa.
- **Redrive depois da versão nova, em todas as réplicas.** Antes disso a versão antiga recusa as mesmas mensagens de novo e o prazo de 7 dias recomeça. No clone do platform, `make redrive FILA=os.eventos`; se a implantação conviveu com réplicas antigas, repita-o no fim. O teste `test_redrive_so_trata_o_evento_depois_de_implantar_a_versao_nova` mostra a ordem com o shovel do platform.
- **Inspecione a DLQ antes.** O redrive devolve a fila inteira, sem filtrar por motivo: anote o `type`, o `message_id`, o `user_id` e o `correlation_id` de cada mensagem, como o [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md) manda.
- **Rollback.** Não volte a imagem com saga em `compensando` ou em `falha_na_compensacao`: a réplica anterior que tentar mudar essa saga viola a invariante dela, que exige prazo junto do comando em voo (`ValueError`, e a mensagem vai para a DLQ com `erro_no_handler`), e manda a resposta da compensação para a DLQ com `sem_tratador_nesta_versao`. Se voltar, faça o redrive depois de implantar de novo.
- **O processo `prazos` junto com a compensação.** O reenvio por prazo e a retomada (`POST /api/v1/sagas/{ordem_id}/compensacao`) vêm com o `prazos`: sem ele, um participante que nunca responde deixa a saga em `compensando` sem reenvio nem alerta de prazo, e a saga em `falha_na_compensacao` dispara o alerta "Saga parada" cujo procedimento termina na retomada. Por isso a versão que compensa e o `prazos` vão juntos para o cluster (os manifestos têm os dois), e o `make smoke` confere que o `prazos` fica pronto.

### Troca de senha e de chave

Os comandos usam o contexto do kind; no k3s, troque o `--context`. O pod lê o Secret só no start, por isso cada troca termina num restart.

1. Senha de um papel do PostgreSQL (o exemplo troca a do `os_app`; a chave do Secret de cada papel está na [tabela do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#segredos-gerados)). Grave a senha nova no Secret:

   ```bash
   senha=$(openssl rand -hex 24)
   kubectl --context kind-pytstop-p4 -n pytstop-os get secret os-postgres -o json \
     | SENHA="$senha" jq '.data.POSTGRES_APP_PASSWORD = (env.SENHA | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Aplique a senha no banco, como `postgres`, pela entrada padrão, como na [troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco) do platform: ela não passa por argumento, que fica no `ps` da máquina e no pedido ao apiserver. O `psql` troca o `:'senha'` pelo valor antes de enviar o comando, e os `SET`, que valem só para a sessão, tiram a senha do log do servidor: o de comandos, a linha `STATEMENT` de um comando que falha e o log por duração ou por amostragem. A saída esperada é `SET` cinco vezes e `ALTER ROLE`:

   ```bash
   { printf '%s\n' "$senha"; cat <<'SQL'
   SET log_statement = none;
   SET log_min_error_statement = panic;
   SET log_min_duration_statement = -1;
   SET log_min_duration_sample = -1;
   SET log_transaction_sample_rate = 0;
   \getenv senha SENHA_NOVA
   ALTER ROLE os_app PASSWORD :'senha';
   SQL
   } | kubectl --context kind-pytstop-p4 -n pytstop-os exec -i statefulset/os-postgres -c postgres -- \
       sh -c 'read -r SENHA_NOVA && [ -n "$SENHA_NOVA" ] && export SENHA_NOVA && PGPASSWORD=$POSTGRES_PASSWORD exec psql -w -U postgres -v ON_ERROR_STOP=1'
   ```

3. Reinicie quem usa o papel: o `os_app`, os Deployments (`kubectl --context kind-pytstop-p4 -n pytstop-os rollout restart deployment`); o `os_exporter`, o StatefulSet do banco, de que o exporter é sidecar; o `os`, ninguém, porque o Job a relê na próxima implantação; o `postgres`, o StatefulSet do banco, que lê a senha dele só no start (sem o restart, a troca seguinte não autentica). Até o restart, conexão nova com a senha antiga é recusada.

A chave RSA do JWT gira pelas etapas da [rotação da chave](autenticacao.md#rotação-da-chave), e cada etapa repete os mesmos três passos, com `$chaves` o diretório da rotação:

1. Grave as duas chaves da etapa no Secret `os-jwt`. Os PEM entram pelo ambiente do `jq`, nunca por argumento, que fica no `ps` e no pedido ao apiserver. `privada` é o arquivo da `JWT_PRIVATE_KEY` e `anterior` é a pública da `JWT_PREVIOUS_PUBLIC_KEY`, vazia na etapa 3. O exemplo é a etapa 1; nas outras só as duas primeiras linhas mudam, como na tabela da rotação (etapa 2: `nova.pem` e a pública da `atual.pem`; etapa 3: `nova.pem` e `anterior=""`):

   ```bash
   privada="$chaves/atual.pem"
   anterior="$(openssl pkey -in "$chaves/nova.pem" -pubout)"
   kubectl --context kind-pytstop-p4 -n pytstop-os get secret os-jwt -o json \
     | PRIVADA="$(cat "$privada")" ANTERIOR="$anterior" \
       jq '.data.JWT_PRIVATE_KEY = (env.PRIVADA | @base64) | .data.JWT_PREVIOUS_PUBLIC_KEY = (env.ANTERIOR | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Reinicie a API e espere o rollout:

   ```bash
   kubectl --context kind-pytstop-p4 -n pytstop-os rollout restart deployment/os-service-api
   kubectl --context kind-pytstop-p4 -n pytstop-os rollout status deployment/os-service-api
   ```

3. Espere os 10 min do cache do JWKS nos consumidores e confira o `kid` pela borda antes da etapa seguinte; a primeira chave da lista é a que assina:

   ```bash
   curl -sk https://localhost/os/.well-known/jwks.json | jq '.keys[].kid'
   ```

A `ENCRYPTION_KEY` (Secret `os-cripto`) não gira: uma chave nova deixaria ilegíveis os dados pessoais já cifrados. E a `ADMIN_PASSWORD` (Secret `os-admin`) não gira pelo Secret: a semente só cria o admin que ainda não existe.
