# PytStop fase 4: OS Service

Serviço de ordens de serviço: abertura, status e histórico da OS, cadastro de clientes e veículos (com LGPD), usuários internos (emissão de JWT) e orquestração da saga de atendimento. Banco próprio: PostgreSQL 16.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

**Status:** em construção. Este README será substituído pela documentação completa do serviço (arquitetura, fluxos, exemplos de API, testes e cobertura, pipelines).

**Proveniência:** recorte da `main` do PytStop fase 3 (`postech-sw-arch-p3` @ [`08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) + [`fc06263`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/fc06263d1f99747145e5a354b817905c59637ebd), a normalização ASCII de CPF/CNPJ do p3 #32), só com os contextos deste serviço (`compartilhado`, `cliente_veiculo`, `autenticacao`, `ordem_servico`). Referências `p3 #N` no código apontam para issues e PRs daquele repositório; IDs como `RF-018`, `ADR-020` e `TD-017` são da numeração contínua das fases anteriores ([requisitos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/requisitos), [ADRs](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/arquitetura/adr) e [dívida técnica](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/tech-debt) do p3); a fase 4 começa em RF-028 e ADR-034.

**Arquitetura da fase 4:** a RFC-004 e os ADR-034 a ADR-043, citados nos comentários do código como `RFC-004 secao N` e `ADR-0NN`, ficam em [`docs/arquitetura` do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura).

## O que já existe

- OS da fase 4: `RECEBIDA → EM_DIAGNOSTICO → AGUARDANDO_APROVACAO → AGUARDANDO_PAGAMENTO → AGUARDANDO_EXECUCAO → EM_EXECUCAO → FINALIZADA → ENTREGUE`, com `CANCELADA` antes do início da execução. A OS guarda o histórico de mudanças de status, o resumo do orçamento e do pagamento (que vivem no Billing) e uma versão para lock otimista (escrita concorrente responde 409).
- API: `POST/GET /api/v1/ordens-de-servico`, `GET /{id}`, `GET /{id}/historico`, `POST /{id}/cancelamento`, `POST /{id}/entrega`, clientes e veículos com rotas LGPD, autenticação (`/api/v1/autenticacao/*` e o JWKS em `GET /.well-known/jwks.json`), acompanhamento público (`POST /api/v1/publico/acompanhamento`, placa e documento no corpo), `GET /api/v1/saude` (liveness), `GET /api/v1/saude/pronto` (readiness: 503 se o banco não responder em 2 s) e `GET /metrics` (com `API_METRICS_ENABLED=true`, ligado no compose). Swagger em `/docs`.
- Mensageria com RabbitMQ: outbox transacional no envelope do contrato, relay com confirmação do broker, consumidor idempotente da fila `os.eventos` com retry por atraso e DLQ ([seção abaixo](#mensageria)).
- Ainda não: os handlers da saga (hoje cada evento recebido só é registrado) e os manifestos Kubernetes, desenhados na RFC-004.

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

## Mensageria

O OS Service orquestra a saga pelo RabbitMQ da plataforma, com AMQP (o protocolo do broker): publica comandos para Billing e Execução e consome os eventos que eles publicam ([ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md), [RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) seção 5). A entrega é pelo menos uma vez e o consumidor é idempotente: cada efeito acontece uma vez (RN-028).

```mermaid
flowchart LR
    uc["caso de uso<br/>(publicar_comando)"] -->|"mesmo commit do efeito"| ob[("outbox")]
    ob -->|"NOTIFY + poll"| relay["relay<br/>python -m src.relay"]
    relay -->|"confirm + mandatory"| cmd{{"pytstop.comandos"}}
    cmd --> filas["billing.comandos<br/>execucao.comandos"]
    evt{{"pytstop.eventos"}} --> q["os.eventos"]
    q --> cons["consumidor<br/>python -m src.consumidor"]
    cons -->|"um commit"| mp[("mensagens_processadas<br/>+ efeito + comandos")]
    cons -.->|"erro transitório"| rt{{"pytstop.retry"}} -.-> rq["os.eventos.retry.1s ... .300s"] -.->|"TTL vence"| q
    cons -.->|"erro permanente ou 6ª falha"| dlq["os.eventos.dlq"]
```

### Contratos

`contratos/` é cópia de arquivos do platform no SHA gravado em `contratos/ORIGEM` (um commit da `main` de lá): do `contratos/` de lá, o AsyncAPI (`asyncapi.yaml`), um JSON Schema por mensagem e os exemplos; em `contratos/rabbitmq/`, a topologia do broker do `k8s/base/rabbitmq/` (definitions, permissões, configuração e o script que cria os usuários) e o admin de demonstração do `compose/`. O `tests/contratos/test_copia_do_platform.py` baixa o platform nesse SHA uma vez (com novas tentativas) e compara cada arquivo byte a byte; atualizar a cópia é copiar de novo e trocar o `ORIGEM`.

O catálogo sai do próprio `asyncapi.yaml`: quem publica cada tipo (o `userId` da operação de envio), em que exchange e routing key, e o que a fila `os.eventos` recebe. Os eventos internos da OS (abertura e mudança de status) ficam no histórico e não vão para o broker.

| | Mensagens |
|---|---|
| O OS publica (11 comandos, enum `Comando`) | `SolicitarDiagnostico`, `DescartarDiagnostico`, `GerarOrcamento`, `CancelarOrcamento`, `ReservarPecas`, `LiberarReserva`, `SolicitarPagamento`, `EstornarPagamento`, `AgendarExecucao`, `CancelarExecucao`, `AnonimizarVeiculo` |
| O OS consome do Billing (13) | `OrcamentoGerado`, `GeracaoDeOrcamentoFalhou`, `OrcamentoAprovado`, `OrcamentoRecusado`, `OrcamentoExpirado`, `OrcamentoCancelado`, `PagamentoSolicitado`, `PagamentoConfirmado`, `PagamentoRecusado`, `PagamentoExpirado`, `PagamentoCancelado`, `PagamentoEstornado`, `EstornoDePagamentoFalhou` |
| O OS consome da Execução (10) | `DiagnosticoIniciado`, `DiagnosticoConcluido`, `DiagnosticoDescartado`, `PecasReservadas`, `ReservaDePecasFalhou`, `ReservaLiberada`, `ExecucaoAgendada`, `ExecucaoCancelada`, `ExecucaoIniciada`, `ExecucaoFinalizada` |

A validação usa o payload de cada mensagem no AsyncAPI: envelope, `tipo`, `versao` e `origem` constantes e o schema de `dados`. O leitor é tolerante (campo novo não invalida) e `versao` desconhecida reprova. O erro aponta o caminho e a regra (`const em $.versao`), nunca o valor, que pode ser a placa ou texto livre.

### Publicar um comando

Na API (e em todo processo sem mensagem de entrada), o caso de uso grava o comando na outbox pela própria unidade de trabalho, no mesmo commit do efeito. O trecho roda com o banco do compose (`set -a && . ./.env && set +a` antes):

```python
from uuid import uuid4

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.infraestrutura.database import (
    criar_engine,
    criar_session_factory,
    resolver_database_url,
)
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork

ordem_id = uuid4()
uow = SQLAlchemyUnitOfWork(criar_session_factory(criar_engine(resolver_database_url())))
with uow:
    uow.publicar_comando(
        Comando.DESCARTAR_DIAGNOSTICO,
        {"ordem_id": ordem_id, "motivo": "cancelamento"},
        correlation_id=ordem_id,  # o id da OS, que tambem identifica a saga
    )
    uow.commit()
```

No consumidor, o handler recebe a mensagem e a `TransacaoDaMensagem`: monta os repositórios sobre a `session` dela, grava o efeito e publica os comandos nela, sem comitar. O consumidor comita uma vez efeito, comandos e o registro em `mensagens_processadas`; commit, rollback ou close pela `session` dentro do handler, ou um retorno que não é `Desfecho`, mandam a mensagem para a DLQ (fila de mensagens mortas) sem gravar nada. A guarda não alcança o que passa por baixo da `session`, que grava o efeito antes do consumidor: o `commit()` da transação raiz com um savepoint aberto manda a mensagem para a DLQ (o redrive vira `duplicada`), o `session.connection().commit()` faz o commit do consumidor falhar e a mensagem volta pela retry como `duplicada` (o que o handler gravasse depois se perde), e o `COMMIT` em SQL literal (`session.execute(text("COMMIT"))`) passa sem guarda nenhuma. Por isso o handler usa só a `session` e os repositórios:

```python
def tratar_diagnostico_concluido(
    mensagem: MensagemRecebida, transacao: TransacaoDaMensagem
) -> Desfecho:
    ordens = OrdemDeServicoSQLAlchemyRepository(session=transacao.session)
    if ordens.obter_por_id(mensagem.correlation_id) is None:
        return Desfecho.IGNORADA
    transacao.publicar_comando(
        Comando.GERAR_ORCAMENTO,
        {"ordem_id": mensagem.correlation_id, "itens": mensagem.dados["itens"]},
        correlation_id=mensagem.correlation_id,
        causation_id=mensagem.id,
    )
    return Desfecho.PROCESSADA
```

O envelope (`id`, `tipo`, `versao`, `origem=os-service`, `correlation_id`, `causation_id`, `ocorrido_em` em UTC e `dados`) é validado na hora: comando fora do contrato é bug e sobe como 500. A linha guarda o envelope, o exchange, a routing key e o contexto W3C (`traceparent`, `tracestate`) do span corrente; os tempos dela são do relógio do banco.

### Relay (`python -m src.relay`)

- Acorda com o `NOTIFY` da outbox e, por segurança, a cada `OUTBOX_POLL_SEGUNDOS`. Reivindica lotes com `FOR UPDATE SKIP LOCKED` e um lease, na ordem de cada OS: uma linha em espera segura as seguintes da mesma OS, e `dead` não segura.
- Nenhuma transação fica aberta durante a publicação: claim, renovação do lease antes de publicar e desfecho são transações curtas, e o fim do lease gravado no claim é o token da réplica. Quem perdeu a linha (o lease venceu e outra réplica a reivindicou) não grava o desfecho por cima. A entrega é pelo menos uma vez: um publish que passa do lease pode sair também pela outra réplica, e o consumidor do destino descarta a repetição pelo `id`.
- Publica com *publisher confirms* e `mandatory`, com as propriedades AMQP do contrato (`message_id`, `correlation_id`, `type`, `user_id=os`, `content_type`, `delivery_mode=2`) e o `traceparent` no header. A linha só vira `entregue` depois da confirmação.
- Mensagem devolvida (sem fila para a routing key), recusada (nack), canal fechado pelo broker ou qualquer outra falha da própria linha conta tentativa e volta depois de 1, 4, 16 e 64 s; na quinta falha vira `dead` (métrica `outbox_dead`). Envelope fora do contrato vira `dead` direto.
- Broker fora do ar não conta tentativa: sem conexão o relay não reivindica linhas e reconecta com backoff (espera sorteada entre 0 e um teto que dobra de 1 s até 30 s), e as linhas de um lote interrompido voltam na hora. Com o broker em alarme de memória ou disco, a conexão bloqueada para os claims até o desbloqueio, e o timeout do bloqueio (30 s) derruba a conexão como uma queda.
- Uma vez por hora apaga, em lotes de 1000, as linhas entregues há mais de 7 dias e as `dead` há mais de 30, contados da morte da linha e não da criação (a janela para o redrive); `pendente` nunca expira.

### Consumidor (`python -m src.consumidor`)

Para cada mensagem da fila `os.eventos`, uma por vez (prefetch 1) e com ack manual:

1. Origem: o `user_id` (o broker garante que é o usuário da conexão de quem publicou) tem de ser o produtor do `tipo` no catálogo; a cópia que volta da fila de retry traz o `user_id` do próprio consumidor e só é aceita com `x-tentativa` de 1 a 5. Sem `user_id`, ou qualquer outro caso, vai para a DLQ.
2. Trace: o span CONSUMER é filho do `traceparent` recebido.
3. Contrato: corpo de até 64 KiB, JSON, envelope e `dados` validados; `message_id`, `correlation_id` e `type` têm de bater com o envelope. Antes da validação, só o `message_id` e o `correlation_id` convertidos em UUID vão para o log e o span.
4. Efeito: grava o `id` em `mensagens_processadas` e chama o handler do `tipo` no `DESPACHANTE` (`src/consumidor.py`) com a transação da mensagem; o consumidor comita e só então dá ack. Id repetido recebe ack sem efeito.

| Situação | Resultado |
|---|---|
| Handler concluiu | ack (`processada`, ou `ignorada` quando a mensagem não corresponde ao estado atual) |
| Mesmo `id` de novo | ack sem efeito (`duplicada`) |
| Erro transitório (banco fora, `FalhaTransitoriaError`, conflito de versão) | cópia em `pytstop.retry` com `x-tentativa` + 1 e a routing key da fila do nível (`os.eventos.retry.1s`, `.5s`, `.15s`, `.60s` e `.300s`), com confirmação e `mandatory`, e só então o ack (`retry`); a cópia leva as propriedades da original e o contexto de trace do consumo |
| Sexta falha transitória, tipo, versão, contrato ou origem inválidos, cópia de retry devolvida ou recusada, ou outra exceção do handler | `reject` sem requeue, e a fila manda para `os.eventos.dlq` (`dlq`) |

Cada nível de atraso tem a sua fila, com o TTL (tempo de vida da mensagem) como argumento dela: uma fila só, com `expiration` por mensagem, seguraria a cópia de 1 s atrás da de 300 s, porque a mensagem só expira na cabeça da fila. Uma mensagem que o cliente AMQP não consegue nem decodificar (um header de timestamp fora do intervalo, por exemplo) derruba a conexão a cada entrega: com prefetch 1 só ela cai, e o `delivery-limit` de 5 da fila (policy do platform) a manda para a DLQ sem levar as seguintes. Os handlers da saga entram com o orquestrador; até lá cada evento só registra o recebimento no log. Uma vez por hora o consumidor apaga, em lotes, as linhas de `mensagens_processadas` com mais de 30 dias.

### Observabilidade, saúde e encerramento

- Spans: `publish <tipo>` (PRODUCER, filho do span que gravou a outbox) e `process <tipo>` (CONSUMER, filho da publicação), com `correlation_id` como atributo e o status de erro só com o tipo da exceção; os laços ociosos não abrem span. O SDK do OpenTelemetry fica sempre ligado no relay e no consumidor, e `OTEL_ENABLED=true` liga a exportação OTLP (o protocolo do OpenTelemetry) para o Jaeger. O recurso leva `service.name` (de `OTEL_SERVICE_NAME`) e `pytstop.processo` (`api`, `relay` ou `consumidor`).
- Logs JSON com `correlation_id`, `trace_id` e `span_id`, sem dados pessoais nem texto livre: o envelope não vai para o log, e do cliente AMQP só sai o nível ERROR (em WARNING ele imprime o corpo da mensagem devolvida).
- Métricas no `/metrics` de cada processo (porta `METRICS_PORT`, 9100): `pytstop_mensagens_publicadas_total{tipo}`, `pytstop_mensagens_consumidas_total{tipo,resultado}`, `pytstop_reconexoes_ao_broker_total{processo}` (para o alerta de processo preso reconectando), `outbox_pendentes` e `outbox_dead`.
- Saúde: cada processo toca `/tmp/<processo>-heartbeat` a cada volta do laço, a cada linha do lote do relay e entre os lotes da limpeza (liveness), e mantém `/tmp/<processo>-pronto` enquanto está conectado ao broker (readiness). Broker fora, inclusive com o nome dele sem resolução no DNS (o Service headless sem pod pronto), tira o processo de pronto sem reiniciá-lo, e ele reconecta com backoff. A sonda de liveness tolera 90 s sem heartbeat: com o broker mudo um publish fica até cerca de 70 s preso antes de o heartbeat AMQP (30 s) derrubar a conexão, e com o broker em alarme até 30 s. No SIGTERM o processo termina a mensagem em curso e fecha as conexões; no consumidor, as demais seguem na fila.
- Banco: relay e consumidor usam um pool de 2 + 2 conexões e sessões com `statement_timeout` de 15 s e `lock_timeout` de 10 s, abaixo da janela do heartbeat do broker (o handler roda na thread da conexão AMQP).

### Configuração

| Variável | Padrão | Uso |
|---|---|---|
| `RABBITMQ_URL` | obrigatória | URL AMQP com o usuário `os`; fora de `development` e `test`, a senha de demonstração é recusada no boot (a do Postgres também) |
| `METRICS_PORT` | 9100 | porta do `/metrics` do relay e do consumidor |
| `OUTBOX_POLL_SEGUNDOS` | 5 | poll de segurança do relay, de 0,1 a 15 s (o laço ocioso atende o heartbeat AMQP) |
| `OUTBOX_LOTE` | 10 | linhas por lote do relay (1 ou mais) |
| `OUTBOX_LEASE_SEGUNDOS` | 60 | lease de cada linha reivindicada (45 ou mais: cobre o publish bloqueado por alarme do broker, de até 30 s, e a marcação da linha) |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` | `false`, `http://jaeger:4317` | exportação OTLP dos spans |
| `OTEL_SERVICE_NAME` | `pytstop-os-service` | `service.name` dos spans |

Valor fora da faixa aborta o boot com a variável e o valor na mensagem.

## Como rodar local

Pré-requisitos: Docker com Compose e [uv](https://docs.astral.sh/uv/).

```bash
make compose-up      # build da imagem, PostgreSQL 16, RabbitMQ, migrações, admin de demonstração, relay e consumidor
curl -s localhost:8000/api/v1/saude
make compose-down    # derruba e apaga os volumes
```

A API sobe em `http://localhost:8000` (porta configurável com `APP_PORT`) e o Swagger em `http://localhost:8000/docs`. O RabbitMQ do compose carrega a topologia de `contratos/rabbitmq/` e cria os usuários dos três serviços; o console fica em `http://localhost:15674` (usuário `admin`, senha de demonstração em `contratos/rabbitmq/rabbitmq-admin.json`) e o AMQP em `localhost:5674`, fora das portas do compose do platform. O usuário de demonstração é `admin@pytstop.dev` com a senha de `ADMIN_PASSWORD` no `docker-compose.yml` (valores só de dev; o boot com `ENVIRONMENT=production` recusa esses literais).

```bash
TOKEN=$(curl -s localhost:8000/api/v1/autenticacao/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@pytstop.dev","senha":"admin-demo-os-2026"}' | jq -r .access_token)
curl -s localhost:8000/api/v1/ordens-de-servico -H "Authorization: Bearer $TOKEN"
```

Fora do compose (API, relay e consumidor no host), o `.env.example` lista todas as variáveis lidas pelo serviço, com os mesmos valores de demonstração; o RabbitMQ pode ser o do compose (`docker compose up -d rabbitmq rabbitmq-usuarios`, AMQP em `localhost:5674`):

```bash
docker run -d --name os-postgres -p 127.0.0.1:5432:5432 \
  -e POSTGRES_DB=os -e POSTGRES_USER=pytstop -e POSTGRES_PASSWORD=pytstop postgres:16
cp .env.example .env && set -a && . ./.env && set +a
uv run alembic upgrade head && uv run python scripts/seed_admin.py
uv run python -m src.main         # API em http://127.0.0.1:8000
uv run python -m src.relay        # outro terminal, com o mesmo .env
uv run python -m src.consumidor   # outro terminal (METRICS_PORT diferente do relay)
```

## Qualidade

```bash
make check   # uv.lock em dia, ruff (lint e formato), import-linter, mypy strict, bandit e pytest
make audit   # pip-audit das dependências de runtime (com os extras da imagem)
make smoke   # imagem pelo entrypoint real: readiness, login do admin semeado validado pelo JWKS
             # (e a assinatura adulterada recusada), log de boot em JSON, a imagem de produção
             # (usuário 1001, ENVIRONMENT=production, sem header server), relay e consumidor
             # prontos (/metrics respondendo, outbox_pendentes numerico, consumidor inscrito
             # em os.eventos com prefetch 1 no list_consumers), depois down -v
```

`make test` (ou `uv run pytest`) roda os testes unitários, os de contrato e os de integração contra um PostgreSQL e um RabbitMQ 4.3.6 efêmeros (testcontainers, Docker necessário), com gate de cobertura de 90% (`.coveragerc`). O teste de checksum dos contratos baixa o platform do GitHub e precisa de rede: offline, rode com `-m "not rede"` (a cobertura do gate continua valendo). O schema dos testes de integração é criado pela própria migração Alembic, e o broker de teste sobe com a topologia de `contratos/rabbitmq/`, só com o TTL das filas de retry reduzido a 100 ms para o ciclo inteiro de tentativas caber num teste.

No GitHub, o workflow `CI` (`.github/workflows/ci.yml`) roda os mesmos gates em todo PR, publica `coverage.xml`, `htmlcov/` e o JUnit como artefato com o resumo de cobertura por pacote no summary, passa o SonarQube com quality gate versionado (`.sonar/quality-gate.json`), builda a imagem e roda o `make smoke`. O workflow `Security` roda pip-audit (dependências de runtime com os extras da imagem), gitleaks e trivy (imagem), em todo PR e toda segunda-feira.

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.
