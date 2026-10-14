# PytStop fase 4: OS Service

Serviço de ordens de serviço: abertura, status e histórico da OS, cadastro de clientes e veículos (com LGPD), usuários internos (emissão de JWT) e orquestração da saga de atendimento. Banco próprio: PostgreSQL 16.

Parte da fase 4 do Tech Challenge (FIAP Pós Tech, Software Architecture, 15SOAT): o PytStop, sistema de gestão de oficina mecânica das fases anteriores, refatorado em microsserviços com Saga Pattern, mensageria assíncrona, CI/CD por serviço e deploy automatizado em Kubernetes.

**Status:** em construção. Este README será substituído pela documentação completa do serviço (arquitetura, fluxos, exemplos de API, testes e cobertura, pipelines).

**Proveniência:** recorte da `main` do PytStop fase 3 (`postech-sw-arch-p3` @ [`08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) + [`fc06263`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/fc06263d1f99747145e5a354b817905c59637ebd), a normalização ASCII de CPF/CNPJ do p3 #32), só com os contextos deste serviço (`compartilhado`, `cliente_veiculo`, `autenticacao`, `ordem_servico`). Referências `p3 #N` no código apontam para issues e PRs daquele repositório; IDs como `RF-018`, `ADR-020` e `TD-017` são da numeração contínua das fases anteriores ([requisitos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/requisitos), [ADRs](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/arquitetura/adr) e [dívida técnica](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/tech-debt) do p3); a fase 4 começa em RF-028 e ADR-034.

**Arquitetura da fase 4:** a RFC-004 e os ADR-034 a ADR-043, citados nos comentários do código como `RFC-004 secao N` e `ADR-0NN`, ficam em [`docs/arquitetura` do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura).

## O que já existe

- OS da fase 4: `RECEBIDA → EM_DIAGNOSTICO → AGUARDANDO_APROVACAO → AGUARDANDO_PAGAMENTO → AGUARDANDO_EXECUCAO → EM_EXECUCAO → FINALIZADA → ENTREGUE`, com `CANCELADA` antes do início da execução. A OS guarda o histórico de mudanças de status, o resumo do orçamento e do pagamento (que vivem no Billing) e uma versão para lock otimista (escrita concorrente responde 409).
- Saga de atendimento orquestrada pelo OS Service: a abertura da OS inicia a saga, o consumidor conduz o caminho feliz até `FINALIZADA`, e as falhas de negócio e o cancelamento levam as compensações, uma por vez, até a OS `CANCELADA` ou até a falha da compensação, com a etapa visível na OS e na rota de operação. O processo `prazos` reenvia o comando sem resposta, compensa o passo que esgota os reenvios e para a compensação sem resposta em `falha_na_compensacao`, de onde o admin a retoma ([seção abaixo](#saga)).
- API: `POST/GET /api/v1/ordens-de-servico`, `GET /{id}` (com a etapa da saga), `GET /{id}/historico` (com os passos da saga), `POST /{id}/cancelamento` (202 com a etapa, ou 409), `POST /{id}/entrega`, `GET /api/v1/sagas/{ordem_id}` e `POST /api/v1/sagas/{ordem_id}/compensacao` (admin), clientes e veículos com rotas LGPD, autenticação (`/api/v1/autenticacao/*` e o JWKS em `GET /.well-known/jwks.json`), acompanhamento público (`POST /api/v1/publico/acompanhamento`, placa e documento no corpo), `GET /api/v1/saude` (liveness), `GET /api/v1/saude/pronto` (readiness: 503 se o banco não responder em 2 s) e `GET /metrics` (com `API_METRICS_ENABLED=true`, ligado no compose). Swagger em `/docs`.
- Mensageria com RabbitMQ: outbox transacional no envelope do contrato, relay com confirmação do broker, consumidor idempotente da fila `os.eventos` com retry por atraso e DLQ ([seção abaixo](#mensageria)).
- Manifestos Kubernetes em `k8s/`, com o PostgreSQL do serviço, o Job de migração e a borda no Kong ([Implantação](#implantação)).
- E-mail ao cliente a cada mudança de situação da OS, pelo processo `notificador` e o Mailpit, e a eliminação de dados da LGPD que também anonimiza a placa na Execução e nos comandos da outbox ([seção abaixo](#notificações-por-e-mail-e-lgpd)).
- A compensação só vai para o cluster junto com o processo `prazos`, que já está nos manifestos ([Implantação e DLQ](#implantação-e-dlq)).

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

A chave privada não entra no repositório nem na imagem (`*.pem` e `*.key` são ignorados pelo git e pelo Docker); gere as chaves num diretório temporário, fora do repositório, e leve-as direto para o Secret (apague o diretório depois de atualizar o Secret). O primeiro `openssl pkey` mostra a pública da nova; o segundo, a da atual, que sai da chave que está no Secret, gravada no mesmo diretório como `atual.pem`:

```bash
chaves="$(mktemp -d)"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$chaves/nova.pem"
openssl pkey -in "$chaves/nova.pem" -pubout
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
4. Efeito: grava o `id` em `mensagens_processadas` e chama o handler do `tipo` no despachante (`montar_despachante`, em `src/consumidor.py`: os 23 eventos vão ao orquestrador da saga) com a transação da mensagem; o consumidor comita e só então dá ack. Id repetido recebe ack sem efeito.

| Situação | Resultado |
|---|---|
| Handler concluiu | ack (`processada`, ou `ignorada` quando a mensagem não corresponde ao estado atual) |
| Mesmo `id` de novo | ack sem efeito (`duplicada`) |
| Erro transitório (banco fora, `FalhaTransitoriaError`, conflito de versão) | cópia em `pytstop.retry` com `x-tentativa` + 1 e a routing key da fila do nível (`os.eventos.retry.1s`, `.5s`, `.15s`, `.60s` e `.300s`), com confirmação e `mandatory`, e só então o ack (`retry`); a cópia leva as propriedades da original e o contexto de trace do consumo |
| Sexta falha transitória, tipo, versão, contrato ou origem inválidos, cópia de retry devolvida ou recusada, `FalhaPermanenteError` do handler (com o motivo dela em código no log) ou outra exceção do handler | `reject` sem requeue, e a fila manda para `os.eventos.dlq` (`dlq`); o `id` não entra em `mensagens_processadas`, e o redrive trata a mensagem de novo |

Cada nível de atraso tem a sua fila, com o TTL (tempo de vida da mensagem) como argumento dela: uma fila só, com `expiration` por mensagem, seguraria a cópia de 1 s atrás da de 300 s, porque a mensagem só expira na cabeça da fila. Uma mensagem que o cliente AMQP não consegue nem decodificar (um header de timestamp fora do intervalo, por exemplo) derruba a conexão a cada entrega: com prefetch 1 só ela cai, e o `delivery-limit` de 5 da fila (policy do platform) a manda para a DLQ sem levar as seguintes. Uma vez por hora o consumidor apaga, em lotes, as linhas de `mensagens_processadas` com mais de 30 dias.

### Observabilidade, saúde e encerramento

- Spans: `publish <tipo>` (PRODUCER, filho do span que gravou a outbox) e `process <tipo>` (CONSUMER, filho da publicação), com `correlation_id` como atributo e o status de erro só com o tipo da exceção; os laços ociosos não abrem span. O SDK do OpenTelemetry fica sempre ligado no relay e no consumidor, e `OTEL_ENABLED=true` liga a exportação OTLP (o protocolo do OpenTelemetry) para o Jaeger. O recurso leva `service.name` (de `OTEL_SERVICE_NAME`) e `pytstop.processo` (`api`, `relay`, `consumidor` ou `prazos`). O `notificador` não abre span: a linha de e-mail não guarda o contexto de trace.
- Logs JSON com `correlation_id`, `trace_id` e `span_id`, sem dados pessoais nem texto livre: o envelope não vai para o log, e do cliente AMQP só sai o nível ERROR (em WARNING ele imprime o corpo da mensagem devolvida).
- Métricas no `/metrics` de cada processo (porta `METRICS_PORT`, 9100): `pytstop_mensagens_publicadas_total{tipo}`, `pytstop_mensagens_consumidas_total{tipo,resultado}`, `pytstop_reconexoes_ao_broker_total{processo}` (para o alerta de processo preso reconectando), `outbox_pendentes` e `outbox_dead`.
- Saúde: cada processo toca `/tmp/<processo>-heartbeat` a cada volta do laço, a cada linha do lote do relay e entre os lotes da limpeza (liveness), e mantém `/tmp/<processo>-pronto` enquanto está conectado ao broker (readiness; o `notificador` não fala com o broker, e o seu pronto é o banco respondendo). Broker fora, inclusive com o nome dele sem resolução no DNS (o Service headless sem pod pronto) e o broker que aceita a conexão TCP e não responde o AMQP no prazo do cliente (15 s), tira o processo de pronto sem reiniciá-lo, e ele reconecta com backoff; a tentativa que falha toca o heartbeat de novo, porque a resolução do nome não tem prazo no cliente AMQP. Falta de descritores, certificado TLS recusado e erro de disco no arquivo de saúde não são broker fora: derrubam o processo, para o Kubernetes reiniciá-lo e o defeito aparecer. A sonda de liveness tolera 90 s sem heartbeat (120 s no `prazos`): com o broker mudo um publish fica até cerca de 70 s preso antes de o heartbeat AMQP (30 s) derrubar a conexão, e com o broker em alarme até 30 s. No SIGTERM o processo termina a mensagem em curso e fecha as conexões; no consumidor, as demais seguem na fila.
- Banco: relay, consumidor, `prazos` e notificador usam um pool de 2 + 2 conexões e sessões com `statement_timeout` de 15 s e `lock_timeout` de 10 s, abaixo da janela do heartbeat do broker (o handler roda na thread da conexão AMQP).

### Configuração

| Variável | Padrão | Uso |
|---|---|---|
| `RABBITMQ_URL` | obrigatória | URL AMQP com o usuário `os`; fora de `development` e `test`, a senha de demonstração é recusada no boot (a do Postgres também) |
| `METRICS_PORT` | 9100 | porta do `/metrics` do relay, do consumidor, do `prazos` e do notificador |
| `OUTBOX_POLL_SEGUNDOS` | 5 | poll de segurança do relay, de 0,1 a 15 s (o laço ocioso atende o heartbeat AMQP) |
| `OUTBOX_LOTE` | 10 | linhas por lote do relay (1 ou mais) |
| `OUTBOX_LEASE_SEGUNDOS` | 60 | lease de cada linha reivindicada (45 ou mais: cobre o publish bloqueado por alarme do broker, de até 30 s, e a marcação da linha) |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` | `false`, `http://jaeger:4317` | exportação OTLP dos spans |
| `OTEL_SERVICE_NAME` | `pytstop-os-service` | `service.name` dos spans |
| `SAGA_PRAZO_RESPOSTA_SEGUNDOS` | 120 | prazo técnico gravado pela saga no envio de cada comando com resposta automática (1 ou mais; [seção Saga](#saga)). Tem de ser o mesmo valor na API (a primeira compensação do cancelamento e a retomada), no consumidor e no processo `prazos`; no compose é um ponto só (`x-saga`) |
| `SAGA_MAX_REENVIOS` | 5 | reenvios do mesmo comando (0 ou mais) antes de compensar ou de parar a compensação; só o `prazos` lê ([Prazos, falha na compensação e retomada](#prazos-falha-na-compensação-e-retomada)) |
| `PRAZOS_INTERVALO_SEGUNDOS` | 30 | intervalo entre os ciclos do `prazos`, de 0,1 a 300 s |
| `ROOT_PATH` | vazio | prefixo da API atrás do gateway (`/os` no cluster); o `entrypoint.sh` o passa ao uvicorn como `--root-path` e recusa, no boot, valor sem a barra inicial ou com a barra final |

Valor fora da faixa aborta o boot com a variável e o valor na mensagem.

## Saga

O OS Service orquestra a saga de atendimento ([ADR-035](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/035-saga-orquestrada.md), [RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) seção 4). Há uma instância por OS, com `saga_id = ordem_id`, que também é o `correlation_id` de todas as mensagens.

- O orquestrador é um *process manager*: o objeto que guarda o estado da saga e, a cada evento, decide o próximo comando. Ele vive na camada de aplicação (`src/ordem_servico/aplicacao/saga/`), com estado e regras sem I/O, e é persistido como agregado próprio na tabela `sagas`, no mesmo PostgreSQL da outbox.
- A etapa da saga, o status da OS, o registro da transição na linha do tempo da saga e o comando seguinte entram no mesmo commit: na abertura, o da requisição; nos eventos, o do consumidor, junto com `mensagens_processadas`.

### Por que orquestração

- **O tamanho da saga.** São nove passos, T1 a T9 (as transações locais de cada serviço, da abertura à finalização), em três serviços, mais a entrega local (T10), com esperas humanas (mecânico e cliente), um provedor externo (Mercado Pago) e seis compensações. Um dos passos é o *pivot*, o ponto sem retorno (RN-029): o início da execução física, depois do qual não há compensação.
- **Por que não coreografia.** A Aula 02 de SAGA Pattern, citando Richardson, recomenda a coreografia para sagas simples, e esta não é. Com coreografia, os passos ficariam espalhados nos três serviços, sem um lugar que diga em que etapa a OS está, e prazos, ordem das compensações e o pivot exigiriam que cada serviço assinasse eventos dos outros dois, com risco de ciclo.
- **Por que no OS, e não num quarto serviço.** Como nos exemplos do material, o orquestrador fica no serviço que inicia a saga: o fluxo inteiro fica num lugar só, testável sem broker, e etapa e status mudam na mesma transação local.
- **Os dois contras da aula, contidos.** O orquestrador só conhece a ordem dos passos, as compensações e os prazos: preço, validade e pagamento são dos participantes, que respondem a comandos sem conhecer a saga. E ele não é ponto único de falha: o estado fica no banco e o consumidor roda em réplicas. As alternativas descartadas (coreografia, orquestrador como quarto serviço, motor de workflow) estão no ADR-035.

### Caminho feliz

O diagrama é o da RFC-004, seção 4.8a; setas abertas são mensagens assíncronas (outbox, relay, exchange, fila, consumidor).

```mermaid
sequenceDiagram
    autonumber
    actor AT as Atendente
    actor ME as Mecânico
    actor CL as Cliente
    participant OS as os-service
    participant EX as execution-service
    participant BI as billing-service
    participant MP as Mercado Pago

    rect rgba(70, 130, 180, 0.10)
        Note over AT,EX: T1 e T2: abertura e diagnóstico
        AT->>OS: POST /api/v1/ordens-de-servico
        OS->>OS: transação: OS RECEBIDA, saga AGUARDANDO_DIAGNOSTICO, comando na outbox
        OS-->>AT: 201 Created
        OS-)EX: SolicitarDiagnostico
        ME->>EX: POST /api/v1/diagnosticos/{ordem_id}/inicio
        EX-)OS: DiagnosticoIniciado
        Note over OS: OS EM_DIAGNOSTICO
        ME->>EX: POST /api/v1/diagnosticos/{ordem_id}/conclusao
        EX->>EX: valida o estado local: diagnóstico em andamento, mecânico dono, itens e SKUs no estoque
        EX->>BI: POST /api/v1/precos/validacao (REST síncrono)
        BI-->>EX: 200, invalidos vazio
        EX-)OS: DiagnosticoConcluido
    end

    rect rgba(60, 179, 113, 0.10)
        Note over OS,BI: T3 e T4: orçamento e decisão do cliente
        OS-)BI: GerarOrcamento
        BI-)OS: OrcamentoGerado
        Note over OS,EX: OS AGUARDANDO_APROVACAO, e-mail com link_decisao pela outbox
        CL->>BI: POST /api/v1/publico/orcamentos/{token}/decisao (aprova)
        BI-)OS: OrcamentoAprovado
    end

    rect rgba(218, 165, 32, 0.12)
        Note over OS,MP: T5 e T6: reserva de peças e pagamento
        OS-)EX: ReservarPecas
        EX-)OS: PecasReservadas
        OS-)BI: SolicitarPagamento
        Note over OS: OS AGUARDANDO_PAGAMENTO
        BI->>MP: POST /checkout/preferences
        MP-->>BI: init_point
        BI-)OS: PagamentoSolicitado
        Note over OS,EX: e-mail com checkout_url pela outbox
        CL->>MP: paga no Checkout Pro
        MP->>BI: POST /api/v1/webhooks/mercadopago (x-signature)
        BI->>MP: GET /v1/payments/{id}
        MP-->>BI: status approved
        Note over BI,MP: sem webhook, a conciliação do prazos acha o pagamento em até 30 s
        BI-)OS: PagamentoConfirmado
        Note over OS: OS AGUARDANDO_EXECUCAO
    end

    rect rgba(147, 112, 219, 0.10)
        Note over OS,EX: T7 a T9: fila, execução e finalização
        OS-)EX: AgendarExecucao
        EX-)OS: ExecucaoAgendada
        ME->>EX: POST /api/v1/execucoes/{ordem_id}/inicio
        EX-)OS: ExecucaoIniciada
        Note over OS,EX: pivot: OS EM_EXECUCAO, cancelamento passa a responder 409
        ME->>EX: POST /api/v1/execucoes/{ordem_id}/finalizacao
        EX->>EX: baixa das peças reservadas
        EX-)OS: ExecucaoFinalizada
        Note over OS,EX: OS FINALIZADA, saga CONCLUIDA
    end

    AT->>OS: POST /api/v1/ordens-de-servico/{id}/entrega
    Note over AT,OS: T10: OS ENTREGUE (fora da saga)
```

O e-mail ao cliente do diagrama sai do processo `notificador`, e não da outbox de comandos ([Notificações por e-mail e LGPD](#notificações-por-e-mail-e-lgpd)).

### Etapas e status

Etapa (o estado do orquestrador) e status (o que cliente e atendente veem) são campos diferentes, com dois nomes em comum; a API, o banco e o label `etapa` das métricas usam os nomes em minúsculas.

| Etapa | Status da OS | Evento que tira a saga da etapa | Comando enviado |
|---|---|---|---|
| `aguardando_diagnostico` | `recebida`; `em_diagnostico` com `DiagnosticoIniciado` | `DiagnosticoConcluido` (com a OS em diagnóstico) | `GerarOrcamento`, com os itens do diagnóstico |
| `aguardando_orcamento` | `em_diagnostico` | `OrcamentoGerado` (a OS guarda o resumo) | nenhum |
| `aguardando_decisao` | `aguardando_aprovacao` | `OrcamentoAprovado` | `ReservarPecas`, com as peças dos itens (lista vazia vale) |
| `aguardando_reserva` | `aguardando_aprovacao` | `PecasReservadas` | `SolicitarPagamento` |
| `aguardando_pagamento` | `aguardando_pagamento` | `PagamentoConfirmado` (depois do `PagamentoSolicitado`, que grava o resumo) | `AgendarExecucao`, prioridade `normal` |
| `aguardando_agendamento` | `aguardando_execucao` | `ExecucaoAgendada` (a posição na fila fica só no passo) | nenhum |
| `aguardando_inicio` | `aguardando_execucao` | `ExecucaoIniciada` (pivot) | nenhum |
| `em_execucao` | `em_execucao` | `ExecucaoFinalizada` | nenhum |
| `concluida` | `finalizada`; `entregue` com a entrega (T10, fora da saga) | nenhum | nenhum |

A falha de negócio e o cancelamento levam a saga às etapas `compensando`, `compensada` e `falha_na_compensacao` (seção [Compensações e cancelamento](#compensações-e-cancelamento)); nelas o status da OS não muda, até a OS ficar `cancelada`.

- **O agregado `Saga` impõe a tabela** (`tabela_da_saga.py`, `transicoes.py`). Antes de mudar, ele confere que o evento é da OS e se classifica para ser processado (com os marcos da OS de antes do fato), que o instante não volta antes do último registro e que o gatilho do registro é do vocabulário fechado.
- **Confere o comando.** O comando enviado tem de ser o da linha, com o `ordem_id` da OS, os itens ou as peças que a saga guarda, o orçamento da OS no `SolicitarPagamento`, a prioridade `normal` no `AgendarExecucao`, os ids dos recursos nas compensações e o prazo técnico. O orquestrador só traduz cada evento em chamadas: o fato na OS, o comando na outbox e o registro na saga.
- **Um só caminho de envio.** A abertura, o orquestrador e o cancelamento gravam o comando pela mesma função (`enviar`, em `contexto_de_envio.py`), e o `ContextoDeEnvio` leva o prazo técnico, o relógio e o span da ação. O instante de cada passo nunca fica antes do último registro da saga, que pode ter vindo de outra réplica ou da API com o relógio um pouco adiantado; é a mesma política no consumidor e na API: com mais de 1 s de defasagem avisa no log `saga clock behind last record`, e com mais de 60 s recusa o passo (no consumidor, DLQ com o motivo `relogio_defasado`; no cancelamento, 500).
- **Causa.** Cada comando sai com `causation_id` = id do evento que o causou; o `SolicitarDiagnostico` da abertura, causado pela requisição, sai sem ele.
- **Comando em voo.** O comando com resposta automática (`GerarOrcamento`, `ReservarPecas`, `SolicitarPagamento` até o `PagamentoSolicitado` e `AgendarExecucao`) fica registrado na saga como o comando em voo, o que espera a resposta, com o prazo técnico (`prazo_resposta_em`, `SAGA_PRAZO_RESPOSTA_SEGUNDOS` depois do envio) e o id de cada envio (`mensagem_ids`), com que a resposta casa pelo `causation_id`. O `SolicitarDiagnostico` e as esperas humanas não têm prazo.
- **Só códigos.** A saga guarda etapa, gatilho, comando e ator; texto livre e placa ficam na OS e na mensagem.

### Evento fora de ordem

O consumidor entrega cada evento ao orquestrador, que o classifica pela etapa antes de tocar no domínio (RFC-004 seção 4.5). Os eventos que chegam durante uma compensação têm a tabela própria, em [Compensações e cancelamento](#compensações-e-cancelamento).

| Situação | Exemplo | Desfecho |
|---|---|---|
| Etapa do evento é a atual | `OrcamentoGerado` em `aguardando_orcamento` | processado: status, registro e comando seguinte |
| Etapa já passada, inclusive a resposta republicada | `OrcamentoGerado` em `aguardando_decisao` | ignorado com log (`saga event ignored`) |
| Mesma etapa, fato já aplicado | `DiagnosticoIniciado` com a OS já em diagnóstico; `PagamentoSolicitado` com o resumo gravado | ignorado com log |
| Etapa à frente da atual | `ExecucaoIniciada` antes da `ExecucaoAgendada` | adiantado (`FalhaTransitoriaError`): volta pela fila de retry até a saga alcançá-lo |
| Mesma etapa, antes do fato que o habilita | `DiagnosticoConcluido` com a OS ainda `recebida`; `PagamentoConfirmado` antes do `PagamentoSolicitado` | adiantado |
| Saga concluída | `ExecucaoFinalizada` repetida em `concluida` | ignorado com log |
| OS sem saga, ou `ordem_id` dos dados diferente do `correlation_id` | `DiagnosticoIniciado` de uma OS que não tem saga | erro permanente: DLQ com o motivo `saga_inexistente` ou `ordem_id_divergente` |
| OS cancelada ou entregue com a saga viva (estado que o cancelamento recusa) | `PagamentoConfirmado` de uma OS que um `UPDATE` por fora deixou `cancelada` | DLQ com o motivo `ordem_encerrada`: nenhum comando para OS encerrada |
| Fato que a OS ou a saga recusam | `PagamentoSolicitado` com a OS fora de `aguardando_pagamento` | DLQ com o motivo `transicao_invalida` |
| Relógio desta réplica mais de 60 s atrás do último registro da saga (de outra réplica ou da API) | réplica com o relógio 90 s atrás do `em` do último registro | DLQ com o motivo `relogio_defasado`: não é jitter, e gravaria `em` e prazos no futuro; até 60 s o registro fica no instante do anterior |

O descompasso de estado (etapa já passada, fato repetido, saga encerrada) é ignorado com log e nunca vai para a DLQ. O adiantado volta pela fila de retry nas cinco cópias (1, 5, 15, 60 e 300 s, 381 s ao todo), bem mais que o atraso entre eventos do mesmo passo; se a saga ainda não o tiver alcançado, a sexta falha vai para a DLQ, com alerta (RFC-004 seção 4.5, ADR-035). O motivo de cada recusa sai em código no log `message rejected to dlq` e no status de erro do span do consumo.

A mensagem que o consumidor recusa não entra em `mensagens_processadas` (ADR-036): o redrive da DLQ a trata de novo. A `os.eventos.dlq` guarda cada mensagem por 7 dias (`message-ttl` da política `dlq` em `contratos/rabbitmq/definitions.json`); o procedimento está em [Implantação e DLQ](#implantação-e-dlq).

### Compensações e cancelamento

Termos desta seção:

- **Plano:** as compensações que a saga ainda tem de enviar, na ordem fixa `CancelarExecucao`, `EstornarPagamento`, `LiberarReserva`, `CancelarOrcamento`, `DescartarDiagnostico`. É uma função pura (`aplicacao/saga/compensacao.py`), calculada quando a saga entra em `compensando` e gravada em `plano_compensacao`.
- **Pendente:** a primeira compensação do plano, a que espera resposta (é o comando em voo).
- **Resposta casada:** a resposta cujo `causation_id` é o id de um envio do comando em voo (um dos `comando_em_voo.mensagem_ids`).
- **Estorno automático:** o `PagamentoEstornado` que o Billing publica sozinho, com o `motivo` `pagamento_apos_encerramento`, quando o cliente paga depois de a cobrança ser cancelada, expirada ou recusada (ADR-040). A causa dele é o `SolicitarPagamento`, e não o `EstornarPagamento`.
- **Corrida do pivot:** o `ExecucaoIniciada` que chega com a compensação em curso ou parada, porque a Execução iniciou antes de receber o `CancelarExecucao`.

Entra no plano a compensação de cada passo concluído ou em voo, menos o que a falha de negócio já encerrou no participante; o `DescartarDiagnostico` entra sempre ([RFC-004 seção 4.4](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md)):

| Etapa | Gatilho | Compensações, em ordem |
|---|---|---|
| `aguardando_diagnostico` | cancelamento | `DescartarDiagnostico` |
| `aguardando_orcamento` | `GeracaoDeOrcamentoFalhou` | `DescartarDiagnostico` |
| `aguardando_orcamento` | cancelamento (ou prazo técnico), com o `GerarOrcamento` em voo | `CancelarOrcamento`, `DescartarDiagnostico` |
| `aguardando_decisao` | `OrcamentoRecusado` ou `OrcamentoExpirado` | `DescartarDiagnostico` |
| `aguardando_decisao` | cancelamento | `CancelarOrcamento`, `DescartarDiagnostico` |
| `aguardando_reserva` | `ReservaDePecasFalhou` | `CancelarOrcamento`, `DescartarDiagnostico` |
| `aguardando_reserva` | cancelamento (ou prazo técnico), com o `ReservarPecas` em voo | `LiberarReserva`, `CancelarOrcamento`, `DescartarDiagnostico` |
| `aguardando_pagamento` | `PagamentoRecusado` ou `PagamentoExpirado` | `LiberarReserva`, `CancelarOrcamento`, `DescartarDiagnostico` |
| `aguardando_pagamento` | cancelamento (antes do `PagamentoSolicitado` ou com o checkout aberto) | `EstornarPagamento`, `LiberarReserva`, `CancelarOrcamento`, `DescartarDiagnostico` |
| `aguardando_agendamento` | cancelamento (ou prazo técnico), com o `AgendarExecucao` em voo | as cinco |
| `aguardando_inicio` | cancelamento | as cinco |

Os estados da compensação e o que move a saga entre eles:

```mermaid
stateDiagram-v2
    [*] --> compensando: falha de negócio ou cancelamento (antes do pivot)
    compensando --> compensando: resposta casada da pendente, a seguinte sai
    compensando --> compensada: resposta da última, OS cancelada
    compensando --> falha_na_compensacao: EstornoDePagamentoFalhou
    compensando --> em_execucao: ExecucaoIniciada, só o CancelarExecucao pendente
    falha_na_compensacao --> em_execucao: ExecucaoIniciada, só o CancelarExecucao pendente
    compensada --> [*]
```

O cancelamento depois do pagamento, com o estorno (RFC-004 seção 4.8d, sem a retomada do admin):

```mermaid
sequenceDiagram
    autonumber
    actor AT as Atendente
    participant OS as os-service
    participant EX as execution-service
    participant BI as billing-service

    Note over OS,BI: saga AGUARDANDO_INICIO, OS AGUARDANDO_EXECUCAO (paga e na fila)
    AT->>OS: POST /api/v1/ordens-de-servico/{id}/cancelamento com o motivo
    OS->>OS: OS guarda o texto, saga COMPENSANDO (plano das cinco)
    OS-->>AT: 202 Accepted, etapa compensando
    OS-)EX: CancelarExecucao
    EX-)OS: ExecucaoCancelada
    OS-)BI: EstornarPagamento
    alt estorno aceito
        BI-)OS: PagamentoEstornado
        OS-)EX: LiberarReserva
        EX-)OS: ReservaLiberada
        OS-)BI: CancelarOrcamento
        BI-)OS: OrcamentoCancelado
        OS-)EX: DescartarDiagnostico
        EX-)OS: DiagnosticoDescartado
        OS->>OS: OS CANCELADA com o texto do atendente, saga COMPENSADA
    else estorno recusado pelo provedor
        BI-)OS: EstornoDePagamentoFalhou
        OS->>OS: saga FALHA_NA_COMPENSACAO, sequência suspensa, comando em voo sem prazo
    end
    Note over AT,BI: depois de ExecucaoIniciada (pivot), o mesmo POST responde 409
```

Os eventos que chegam durante a compensação:

| Situação | Exemplo | Desfecho |
|---|---|---|
| Falha de negócio na etapa do passo que falha | `OrcamentoRecusado` em `aguardando_decisao` | processado: a compensação começa pela primeira do plano |
| Resposta da compensação pendente | `ExecucaoCancelada` com o `CancelarExecucao` pendente; as três do `EstornarPagamento` só se forem casadas | processado: a seguinte sai, ou a saga termina |
| Resposta de compensação que não é a da pendente, ou fora de `compensando` | `ReservaLiberada` com o `EstornarPagamento` pendente; qualquer resposta em `falha_na_compensacao` | ignorado com log |
| Evento do fluxo normal com a saga em compensação ou compensada | `OrcamentoAprovado` ou `PagamentoConfirmado` atrasados | ignorado com log: a compensação já cancela o orçamento e devolve o pagamento |
| Estorno automático, em qualquer etapa | `PagamentoEstornado` com `pagamento_apos_encerramento` e o `causation_id` do `SolicitarPagamento` | só o resumo do pagamento passa a `estornado`, se a OS o tiver: sem etapa nova, sem comando e sem registro, com o log `late payment refunded` (sem resumo, ou já estornado, ignorado com log) |
| Corrida do pivot | `ExecucaoIniciada` com só o `CancelarExecucao` pendente | a saga volta a `em_execucao`, e a OS descarta o texto do pedido |
| `ExecucaoIniciada` com outra compensação pendente | `ExecucaoIniciada` com o `EstornarPagamento` pendente | ignorado com log de erro: o `CancelarExecucao` já foi respondido |
| `ExecucaoFinalizada` com o `CancelarExecucao` pendente | | adiantado: volta pela retry até o `ExecucaoIniciada` chegar |

Como a saga se comporta:

- **Uma por vez.** Cada compensação espera a resposta da anterior, com o prazo técnico gravado pela saga no envio. A resposta da última leva a saga a `compensada` e a OS a `cancelada`.
- **Dados do comando.** `{ordem_id, motivo}`, em que `motivo` é o código da compensação e nunca o texto do atendente, mais o `orcamento_id` no `CancelarOrcamento` e o `pagamento_id` no `EstornarPagamento` quando a OS já os tem (sem eles o participante acha o recurso pelo `ordem_id`).
- **A OS cancelada.** Tem origem `saga` e o motivo que o atendente escreveu ou, nas falhas de negócio, o rótulo do código (`ROTULO_DO_MOTIVO`: "Orçamento recusado pelo cliente", "Falta de peça para a reserva", "Pagamento não realizado no prazo"...). O `ator` da mudança é o `sub` do atendente que pediu o cancelamento e, nas falhas de negócio, o processo `consumidor` (ADR-039).
- **Resposta casada.** Com o `EstornarPagamento` pendente, concluem a compensação o `PagamentoCancelado` e o `PagamentoEstornado` (de qualquer `motivo`) casados, e o `EstornoDePagamentoFalhou` casado leva a saga a `falha_na_compensacao`. O `motivo` não serve de chave: o Billing responde ao comando com `pagamento_apos_encerramento` quando a aprovação tardia já tinha sido estornada.
- **Resumo do pagamento.** A OS passa o resumo a `cancelado` ou `estornado` com a resposta, quando o tem (`ResumoPagamento.cancelado()` e `.estornado()`). O cancelamento só atinge a cobrança em aberto e o estorno aceita qualquer origem; nenhuma das duas recusa a resposta do Billing. Se o estorno automático chegou antes e o resumo já está `estornado`, a resposta casada deixa o resumo como está e a compensação avança, em qualquer ordem de chegada entre as réplicas do consumidor.
- **Falha na compensação.** O `EstornoDePagamentoFalhou` para a sequência: o compensado fica compensado, o resto como estava e a OS mantém o status. A saga guarda `falha=estorno_recusado`, o plano com a pendente em primeiro lugar e o comando em voo **sem prazo**, com os ids de todos os envios (a resposta tardia ao original ainda casa depois da retomada). A resposta que chega em `falha_na_compensacao` é ignorada, salvo o estorno automático (só o resumo) e o `ExecucaoIniciada` com o `CancelarExecucao` parado. A retomada pelo admin e o reenvio por prazo ainda não existem.
- **Corrida do pivot.** Se o `ExecucaoIniciada` vence o cancelamento, a saga e a OS voltam a `em_execucao`: o texto do pedido é descartado, motivo, plano e falha se limpam, e o registro tem o gatilho `cancelamento_recusado` com o `mensagem_id` do evento. Se o cancelamento venceu, o `ExecucaoIniciada` que chega depois é ignorado com log de erro.
- **Registros.** O gatilho de cada registro é o tipo da mensagem ou uma palavra da lista fechada `abertura`, `cancelamento_solicitado`, `cancelamento_recusado`, `reenvio`, `prazo_esgotado` e `retomada`; a saga recusa outro, e o `GET /api/v1/sagas/{ordem_id}` documenta a lista.

`POST /api/v1/ordens-de-servico/{id}/cancelamento` (atendente, corpo `{"motivo": "texto livre"}`) responde:

| Etapa | Resposta |
|---|---|
| `aguardando_*` (antes do pivot) | **202** com a OS e `etapa: "compensando"`: a OS guarda o texto em `motivo_cancelamento` sem mudar o status, a saga entra em `compensando` com `motivo=cancelamento` e a primeira compensação sai no mesmo commit, com o registro `cancelamento_solicitado` |
| `compensando` ou `falha_na_compensacao` | **202** com a etapa atual, sem efeito: o pedido repetido não grava texto, registro nem comando |
| `em_execucao` ou `concluida` | **409** `TRANSICAO_STATUS_INVALIDA`, "Cancelamento recusado: execução já iniciada" (RN-029) |
| `compensada` | **409** `TRANSICAO_STATUS_INVALIDA`, "Ordem já cancelada" |
| Conflito de versão com o consumidor que persiste nas três leituras | **409** `CONFLITO_DE_CONCORRENCIA` |
| OS inexistente, ou sem saga (toda OS tem saga desde a migração `003`) | **404** `ENTIDADE_NAO_ENCONTRADA`; não há cancelamento direto |
| Motivo vazio, em branco, com mais de 500 caracteres ou com caractere de controle ou de formatação (ver "Texto livre", abaixo) | **422** `VALOR_INVALIDO`, no envelope do domínio, antes de qualquer efeito |
| Corpo fora do schema (sem `motivo`, campo a mais, tipo errado) | **422** no formato da validação de schema dos três serviços, `{detail, id_requisicao}` |

```bash
curl -s -X POST localhost:8000/api/v1/ordens-de-servico/$ORDEM_ID/cancelamento \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"motivo": "Cliente vendeu o veículo"}' | jq '{status, etapa, motivo_cancelamento}'
```

Com a OS recém-aberta (sem resposta dos participantes ainda), a resposta é `{"status": "recebida", "etapa": "compensando", "motivo_cancelamento": "Cliente vendeu o veículo"}`; repetido o mesmo `POST`, vem outro 202, sem efeito, e depois do início da execução vem o 409 da tabela.

- **Conflito de versão.** Quando o consumidor grava a mesma OS ou saga no meio, a requisição relê as duas e decide de novo, até três leituras (*reread value*, RFC-004 seção 4.5).
- **O texto do atendente.** O `motivo_cancelamento` é o texto do pedido, guardado já no 202, enquanto a OS segue com o status anterior. Quem lê a OS vê o texto com a etapa `compensando`, e ele volta a `null` se o início da execução vencer o pedido. As OS canceladas diretamente, antes de o cancelamento passar pela saga, guardam o texto no mesmo campo.
- **Onde o texto fica.** Em `ordens_de_servico.motivo_cancelamento` e, depois de a OS ser cancelada, em `historico_status_ordem.motivo` (devolvido por `GET /{id}/historico`). O apagamento LGPD do cliente anonimiza as duas colunas, e a saga guarda só códigos.
- **Texto livre.** A descrição do problema (`POST /api/v1/ordens-de-servico`, até 1000 caracteres) e o motivo do cancelamento (até 500) passam pela mesma validação, no domínio: o texto entra aparado (CRLF vira LF), e a violação volta como 422 `VALOR_INVALIDO`.
  - **Recusa:** o texto vazio, o que passa do limite e os caracteres das categorias Unicode `Cc` (controle, como o NUL e o CR solto), `Cf` (formatação: espaço de largura zero, override bidirecional, marca de direção), `Zl` e `Zp` (separador de linha e de parágrafo). A quebra de linha e a tabulação passam.
  - **Passa:** acentos, o espaço sem quebra e o emoji simples, com tom de pele, bandeira ou variação de apresentação.
  - **Falso positivo conhecido:** o emoji composto por junção de largura zero (`👨‍🔧`, `👩‍💻`, as famílias), o hífen condicional e a marca esquerda-direita também são `Cf` e voltam 422 com `<campo> tem caractere de controle ou de formatacao`, sem dizer qual. Texto colado do WhatsApp ou do Word pode trazê-los.
  - **Decisão:** manter a categoria `Cf` inteira recusada. O atendente lê o texto na tela e a LGPD o alcança, e o override bidirecional e o espaço de largura zero escondem ou reordenam o que se lê. Liberar só a junção entre dois pictogramas pediria a propriedade Emoji do Unicode, que o `unicodedata` do Python não tem, e uma lista de pictogramas que cresce a cada versão do Unicode. O custo é o atendente tirar o caractere ou trocar o emoji composto pelo simples; `test_texto_livre.py` fixa os dois lados.
- **Auditoria (ADR-039).** Cada pedido deixa uma linha de log com `correlation_id` (a OS), `ator` (o `sub` de quem pediu) e `resultado`: `order cancellation requested` com `aceito` ou `repetido` (e a `etapa`), ou `order cancellation refused` com o código do erro. Nunca o texto do motivo. O `sub` fica também no registro `cancelamento_solicitado` da saga e no `ator` da mudança `cancelada` do histórico. Só o que chega ao caso de uso é auditado: o corpo fora do schema (422 de schema) e as falhas inesperadas (500, como o relógio defasado ou a invariante da saga violada) saem apenas pelo log genérico da requisição ou do erro, sem `ator` nem `correlation_id`.
- **Cliente com OS em andamento.** Enquanto a OS tem compensação em andamento ela segue ativa, e a desativação e o apagamento do cliente respondem 409 até a saga terminar (`existe_os_ativa_para_cliente` só libera a OS entregue ou cancelada).

### Prazos, falha na compensação e retomada

Termos desta seção:

- **Prazo técnico:** quanto o OS espera a resposta automática de um comando (`GerarOrcamento`, `ReservarPecas`, `SolicitarPagamento` até o `PagamentoSolicitado`, `AgendarExecucao` e as cinco compensações) antes de reenviá-lo. `SolicitarDiagnostico` e as esperas humanas (o mecânico, a decisão do orçamento, o checkout aberto, o início da execução) não têm prazo técnico: quem expira o orçamento e o pagamento é o Billing.
- **Envio mais recente:** o último id de `comando_em_voo.mensagem_ids`, o do original, de um reenvio ou da retomada. O prazo conta dele.
- **Reenvio:** o mesmo `tipo` e os mesmos `dados` (a mesma chave de negócio, então o participante não repete o efeito e republica o desfecho que já registrou), com `id` novo e `causation_id` nulo. O id novo entra em `mensagem_ids`: a resposta ao original, a qualquer reenvio e à retomada continua casando pelo `causation_id`.

O processo `prazos` (`python -m src.prazos`, a mesma imagem) roda a cada `PRAZOS_INTERVALO_SEGUNDOS`, uma transação por saga ([RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) seções 4.6 e 4.7):

1. Confere o relógio do processo contra o do banco (`clock_timestamp()`, o que grava o `entregue_em` da outbox), uma vez por ciclo. Com desvio acima de 5 s, adiantado ou atrasado, o ciclo não age e registra `clock skew too large` em erro, com o desvio: o relógio adiantado reenviaria e esgotaria antes da hora, sem volta, e o atrasado não acharia as vencidas. O ciclo seguinte confere de novo.
2. Confere o consumidor: uma declaração passiva de `os.eventos` (o usuário `os` já lê a fila) devolve o número de consumidores. Com zero, ou com o broker fora, o ciclo não reenvia nem esgota prazo, só avisa em log (`saga deadlines paused`): a resposta pode estar esperando na fila, e quando o consumidor volta a saga segue de onde estava. O `/metrics` e o alerta "Saga parada" seguem vendo o prazo vencido.
3. Reivindica a saga de prazo vencido mais antiga com `FOR UPDATE SKIP LOCKED` (`SagaRepository.reivindicar_vencida`). O prazo só corre depois que o comando sai da outbox: a saga só vence se a linha do envio mais recente está `entregue` e `entregue_em + SAGA_PRAZO_RESPOSTA_SEGUNDOS` já passou. Linha `pendente` (relay ou broker fora) ou `dead` não vence e não gasta reenvio; quem acusa é o alerta de outbox parada. O `prazo_resposta_em` que a saga grava no envio é só o limite inferior que o índice parcial `ix_sagas_prazo` usa para achar as candidatas.
4. Lê a OS e a saga, decide e comita a saga e o comando na outbox juntos. Duas réplicas nunca tratam a mesma saga; o consumidor que leu a saga antes perde na conferência da `versao`, volta pela fila de retry, relê e segue: a resposta ao comando original continua válida, e a repetida que o reenvio provoca chega depois e é ignorada.

| Situação | O que o `prazos` faz |
|---|---|
| Prazo vencido, `reenvios < SAGA_MAX_REENVIOS` | reenvia o comando em voo, `reenvios + 1`, prazo novo, registro `reenvio` (campo `reenvio` com o número, 1 a 5) |
| Esgotado em etapa linear (`aguardando_orcamento`, `_reserva`, `_pagamento` ou `_agendamento`) | inicia a compensação com `motivo=prazo_tecnico` e o passo em voo no plano (as linhas "prazo técnico esgotado" da tabela da RFC 4.4), envia a primeira e zera o contador; registro `prazo_esgotado` |
| Esgotado em `compensando` | a saga vai a `falha_na_compensacao` com `falha=reenvios_esgotados`; nenhum comando sai, o comando em voo fica sem prazo e o plano segue com a pendente primeiro; registro `prazo_esgotado` |
| Saga parada em `falha_na_compensacao`, esperas humanas, sagas encerradas | nada: não têm `prazo_resposta_em` |
| Dado que o domínio recusa (comando guardado fora do contrato, estado incoerente) | a saga fica de fora do ciclo, `saga deadline refused` em erro com o `correlation_id` e o tipo da exceção, e o prazo vencido continua acusando no gauge |

O ator dos registros é `prazos`. Com os padrões (120 s, 5 reenvios, ciclo de 30 s), o original e os cinco reenvios dão ao participante 12 minutos (6 x 120 s) e o limite real chega a cerca de 15 (6 x (120 + 30 + a latência do relay)).

| Variável | Padrão | Uso |
|---|---|---|
| `SAGA_PRAZO_RESPOSTA_SEGUNDOS` | 120 | espera pela resposta, contada da saída da outbox; o mesmo valor na API, no consumidor e no `prazos` |
| `SAGA_MAX_REENVIOS` | 5 | reenvios (0 ou mais) antes de compensar, ou de parar a compensação |
| `PRAZOS_INTERVALO_SEGUNDOS` | 30 | intervalo entre os ciclos, de 0,1 a 300 s; o alerta "Saga parada" usa 60 s, duas vezes o padrão |

A janela de reenvio (720 s) tem de passar da soma dos atrasos de retry do consumidor (1 + 5 + 15 + 60 + 300 = 381 s): senão a saga desistiria de um passo cuja resposta ainda está na fila de retry. `test_configuracao_dos_prazos.py` confere a relação a partir do TTL das filas de retry do `definitions.json`, dos padrões do código, do compose e dos três overlays. Os reenvios saem depois e podem ainda estar no retry quando a saga desiste; o efeito deles é desfeito pela compensação do passo em voo ou descartado pela lápide do participante.

**Falha na compensação.** Entra em `falha_na_compensacao` o `EstornoDePagamentoFalhou` (`falha=estorno_recusado`) ou a compensação sem resposta depois dos reenvios (`falha=reenvios_esgotados`). A sequência para: o compensado fica compensado, o resto fica como estava, a OS mantém o status, o alerta "Saga parada" dispara, e a resposta que chega atrasada é ignorada (salvo o `ExecucaoIniciada` com o `CancelarExecucao` parado, que devolve a saga a `em_execucao`). O operador segue o [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md), resolve a causa e retoma.

`POST /api/v1/sagas/{ordem_id}/compensacao` (admin) reenvia a compensação pendente, o comando guardado em `comando_em_voo`, com `id` novo, que se soma a `mensagem_ids` (os ids do original e dos reenvios ficam: a resposta tardia ao original ainda conclui a compensação), `reenvios = 0` e prazo novo; limpa a `falha`, volta a saga a `compensando`, grava o registro `retomada` com o `sub` do admin e segue o plano até `compensada`:

| Etapa | Resposta |
|---|---|
| `falha_na_compensacao` | **202** com a saga (`SagaResponse`, a mesma do `GET`), `etapa: "compensando"`, `falha: null`, `reenvios: 0` e o prazo novo |
| Qualquer outra | **409** `TRANSICAO_STATUS_INVALIDA`, "Retomada recusada: a saga não está em falha na compensação" |
| Conflito de versão com o consumidor (a corrida do pivot gravou a saga no meio) | **409** `CONFLITO_DE_CONCORRENCIA`; o operador repete o pedido |
| OS inexistente ou sem saga | **404** `ENTIDADE_NAO_ENCONTRADA` |
| Outro papel, sem token | **403** `ACESSO_NEGADO`, **401** `NAO_AUTENTICADO` |

```bash
curl -s -X POST localhost:8000/api/v1/sagas/$ORDEM_ID/compensacao \
  -H "Authorization: Bearer $TOKEN" | jq '{etapa, falha, reenvios, plano_compensacao}'
```

Cada retomada deixa uma linha de log com `correlation_id`, `ator` e `resultado` (`saga resumed`, ou `saga resume refused` com o código do erro), sem os `dados` do comando, e o span `saga retomada` é filho do contexto que a saga guardou, com link para a requisição.

### Implantação e DLQ

- **Ordem.** A migração primeiro (a `004` cria a tabela `notificacoes`, de que o `notificador` depende); depois a API, o relay, o consumidor e o `prazos`, juntos (no cluster, o mesmo `kubectl apply -k` aplica tudo, e os pods esperam a migração no `initContainer`) ou o consumidor antes: a compensação que a API inicia é respondida na `os.eventos`, e uma réplica do consumidor de versão anterior, sem tratador para a falha de negócio e para a resposta de compensação, a recusa com o motivo `sem_tratador_nesta_versao`. Durante o rollout, as respostas que uma réplica antiga consumir vão para a DLQ. O `notificador` sobe por último, depois que a API e o consumidor já gravam o pedido.
- **Janela do rolling update e o e-mail.** Durante o rollout o Deployment do consumidor tem a réplica antiga e a nova consumindo `os.eventos` juntas, e só a nova grava o pedido de e-mail na transação. A transição que a réplica antiga tratar nunca gera e-mail, e nada a reconcilia depois: é aceito (o e-mail é complementar e a OS segue o fluxo). O mesmo vale para o `kubectl rollout undo` do consumidor. O pedido que a API já nova gravar com modelo, status ou motivo que um `notificador` antigo não conhece fica `pendente` até a versão nova subir, sem gastar tentativa.
- **Redrive depois da versão nova, em todas as réplicas.** Antes disso a versão antiga recusa as mesmas mensagens de novo e o prazo de 7 dias recomeça. No clone do platform, `make redrive FILA=os.eventos`; se a implantação conviveu com réplicas antigas, repita-o no fim. O teste `test_redrive_so_trata_o_evento_depois_de_implantar_a_versao_nova` mostra a ordem com o shovel do platform.
- **Inspecione a DLQ antes.** O redrive devolve a fila inteira, sem filtrar por motivo: anote o `type`, o `message_id`, o `user_id` e o `correlation_id` de cada mensagem, como o [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md) manda.
- **Rollback.** Não volte a imagem com saga em `compensando` ou em `falha_na_compensacao`: a réplica anterior que tentar mudar essa saga viola a invariante dela, que exige prazo junto do comando em voo (`ValueError`, e a mensagem vai para a DLQ com `erro_no_handler`), e manda a resposta da compensação para a DLQ com `sem_tratador_nesta_versao`. Se voltar, faça o redrive depois de implantar de novo.
- **O processo `prazos` junto com a compensação.** O reenvio por prazo e a retomada (`POST /api/v1/sagas/{ordem_id}/compensacao`) vêm com o `prazos`: sem ele, um participante que nunca responde deixa a saga em `compensando` sem reenvio nem alerta de prazo, e a saga em `falha_na_compensacao` dispara o alerta "Saga parada" cujo procedimento termina na retomada. Por isso a versão que compensa e o `prazos` vão juntos para o cluster (os manifestos têm os dois), e o `make smoke` confere que o `prazos` fica pronto.

### Consulta e operação

- `GET /api/v1/ordens-de-servico/{id}`: a etapa da saga ao lado do status.
- `GET /api/v1/ordens-de-servico/{id}/historico`: as mudanças de status (`mudancas`), com o ator de cada uma (`sub` do JWT ou o processo `consumidor`), e os registros da saga (`passos`: gatilho, etapa antes e depois, comando enviado e ator), em duas listas, cada uma na sua ordem.
- `GET /api/v1/sagas/{ordem_id}` (admin): etapa, motivo, falha, plano de compensação restante, comando em voo (tipo e hora do envio), reenvios, prazo e registros (o `reenvio` traz o número, 1 a `SAGA_MAX_REENVIOS`, só no registro de reenvio); é a primeira consulta do [runbook da saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/operacao/runbook-saga.md).
- `POST /api/v1/sagas/{ordem_id}/compensacao` (admin): a retomada da compensação parada ([Prazos, falha na compensação e retomada](#prazos-falha-na-compensação-e-retomada)).

```bash
curl -s localhost:8000/api/v1/sagas/$ORDEM_ID -H "Authorization: Bearer $TOKEN" | jq
```

Logo depois do `DiagnosticoConcluido`, a resposta fica assim (registros abreviados: cada um traz também `em`, `mensagem_id`, `comando_id` e `motivo`):

```json
{
  "ordem_id": "3f6e1c2a-8d4b-4f7e-9a11-5c2d0b7e9f40",
  "etapa": "aguardando_orcamento",
  "motivo": null,
  "falha": null,
  "plano_compensacao": [],
  "comando_em_voo": {"tipo": "GerarOrcamento", "enviado_em": "2026-10-07T13:02:11Z"},
  "reenvios": 0,
  "prazo_resposta_em": "2026-10-07T13:04:11Z",
  "passos": [
    {"seq": 1, "gatilho": "abertura", "de": null, "para": "aguardando_diagnostico", "comando": "SolicitarDiagnostico", "ator": "<sub do atendente>"},
    {"seq": 2, "gatilho": "DiagnosticoIniciado", "de": "aguardando_diagnostico", "para": "aguardando_diagnostico", "comando": null, "ator": "consumidor"},
    {"seq": 3, "gatilho": "DiagnosticoConcluido", "de": "aguardando_diagnostico", "para": "aguardando_orcamento", "comando": "GerarOrcamento", "ator": "consumidor"}
  ]
}
```

Depois do cancelamento com o pagamento já feito e da `ExecucaoCancelada`, a saga está em `compensando`: `plano_compensacao` traz o que falta (a primeira é a pendente), `comando_em_voo` é essa pendente e `prazo_resposta_em` é o prazo técnico dela (só os três últimos registros, abreviados como acima):

```json
{
  "ordem_id": "3f6e1c2a-8d4b-4f7e-9a11-5c2d0b7e9f40",
  "etapa": "compensando",
  "motivo": "cancelamento",
  "falha": null,
  "plano_compensacao": ["EstornarPagamento", "LiberarReserva", "CancelarOrcamento", "DescartarDiagnostico"],
  "comando_em_voo": {"tipo": "EstornarPagamento", "enviado_em": "2026-10-07T15:21:14Z"},
  "reenvios": 0,
  "prazo_resposta_em": "2026-10-07T15:23:14Z",
  "passos": [
    {"seq": 9, "gatilho": "ExecucaoAgendada", "de": "aguardando_agendamento", "para": "aguardando_inicio", "comando": null, "ator": "consumidor"},
    {"seq": 10, "gatilho": "cancelamento_solicitado", "de": "aguardando_inicio", "para": "compensando", "comando": "CancelarExecucao", "ator": "<sub do atendente>"},
    {"seq": 11, "gatilho": "ExecucaoCancelada", "de": "compensando", "para": "compensando", "comando": "EstornarPagamento", "ator": "consumidor"}
  ]
}
```

Se o provedor recusa o estorno, o Billing publica o `EstornoDePagamentoFalhou` e a saga para em `falha_na_compensacao`: `falha` traz a causa, o plano segue com a pendente em primeiro lugar, o `comando_em_voo` continua sendo o `EstornarPagamento` e o `prazo_resposta_em` é `null`, porque o comando parado não tem prazo (registros abreviados também):

```json
{
  "ordem_id": "3f6e1c2a-8d4b-4f7e-9a11-5c2d0b7e9f40",
  "etapa": "falha_na_compensacao",
  "motivo": "cancelamento",
  "falha": "estorno_recusado",
  "plano_compensacao": ["EstornarPagamento", "LiberarReserva", "CancelarOrcamento", "DescartarDiagnostico"],
  "comando_em_voo": {"tipo": "EstornarPagamento", "enviado_em": "2026-10-07T15:21:14Z"},
  "reenvios": 0,
  "prazo_resposta_em": null,
  "passos": [
    {"seq": 10, "gatilho": "cancelamento_solicitado", "de": "aguardando_inicio", "para": "compensando", "comando": "CancelarExecucao", "ator": "<sub do atendente>"},
    {"seq": 11, "gatilho": "ExecucaoCancelada", "de": "compensando", "para": "compensando", "comando": "EstornarPagamento", "ator": "consumidor"},
    {"seq": 12, "gatilho": "EstornoDePagamentoFalhou", "de": "compensando", "para": "falha_na_compensacao", "comando": null, "ator": "consumidor"}
  ]
}
```

O trace da saga é o do `POST` de abertura: no Jaeger, busque pelo `correlation_id` (o id da OS, atributo dos spans de mensagem); no Loki, pelo campo `correlation_id` dos logs.

### Observabilidade da saga

- **Trace.**
  - O span da requisição de abertura é a raiz do trace da saga: a outbox guarda o contexto, o relay publica como filho, e o `process <tipo>` do consumidor ganha `pytstop.saga.etapa`, `pytstop.saga.etapa_nova` e `pytstop.saga.desfecho` (`processada`, `ignorada`, `adiantada` ou `recusada`, com o motivo no status de erro), mais `pytstop.saga.falha` quando o evento leva a saga a `falha_na_compensacao`.
  - A saga guarda o `traceparent` da última transição. O cancelamento chega com trace próprio (a requisição do atendente): o caso de uso abre o span `saga cancelamento` como filho desse contexto, com span link para a requisição, e a primeira compensação e o novo `traceparent` da saga nascem nele, de modo que a compensação segue no trace da saga.
  - O `prazos` e a retomada fazem o mesmo: abrem `saga prazo` (um por saga tratada, o ciclo ocioso não abre span) e `saga retomada` como filhos do contexto da saga, e o reenvio sai no trace dela.
  - Os spans `saga cancelamento`, `saga prazo` e `saga retomada` ficam com status de erro (o tipo da exceção, sem a mensagem) só numa falha inesperada; as recusas de negócio (404, 409, 422) não os marcam.
- **Métricas** ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md)).
  - Contadores e histograma, contados só depois do commit da transação raiz (rollback, conflito de versão, commit que falha, savepoint liberado e savepoint desfeito não contam, e a retry não conta duas vezes): `pytstop_saga_iniciadas_total` (API), `pytstop_saga_finalizadas_total{resultado}` (`concluida` ou `compensada`), `pytstop_saga_compensacoes_total{motivo}` (quem inicia a compensação: o consumidor nas falhas de negócio e a API no cancelamento; os oito códigos) e `pytstop_saga_etapa_duracao_segundos{etapa}` (quem tira a saga da etapa). O `prazos` soma `pytstop_saga_reenvios_total{comando}` e `pytstop_saga_prazos_esgotados_total{comando}` (os nove comandos com prazo) e inicia a compensação `prazo_tecnico` na contagem de compensações.
  - Gauges `pytstop_saga_ativas{etapa}`, `pytstop_saga_etapa_mais_antiga_segundos{etapa}` e `pytstop_saga_prazo_vencido_segundos`: o coletor da API os calcula em duas consultas por raspagem (só com `API_METRICS_ENABLED=true`, que liga o `/metrics` da API), numa conexão própria, fora do pool das requisições, com 2 s de prazo para cada consulta e 1 s para esperar um lock. O de prazo vencido é o maior `agora - (entregue_em + SAGA_PRAZO_RESPOSTA_SEGUNDOS)` da linha da outbox do envio mais recente de cada saga com prazo, só entre as entregues, e 0 sem atraso: comando `pendente` ou `dead` na outbox não conta, nem a saga parada em `falha_na_compensacao`. Como o coletor só lê o banco, ele acusa o prazo vencido também com o `prazos` fora do ar ou em pausa (o alerta "Saga parada" usa `max(pytstop_saga_prazo_vencido_segundos) > 60`).
  - Se a consulta falha, os três gauges ficam ausentes (nunca zero), `pytstop_saga_coletor_falhas_total` conta a falha e `pytstop_saga_coletor_disponivel` vai a 0, para o alerta "Saga parada" não ficar mudo com o coletor quebrado. As séries de rótulo fechado nascem em zero em cada processo.
- **Logs** JSON em inglês, com `correlation_id`, `etapa` e `tipo` (o `comando` nos do `prazos`).
  - `saga started`, `saga transition` (com o `motivo` da compensação; em aviso e com a `falha` quando a saga para na compensação), `saga event ignored` (com a classificação), `saga event ahead`, `late payment refunded` (o estorno automático) e `saga clock behind last record` (com a defasagem em segundos).
  - O pedido de cancelamento audita em `order cancellation requested` e `order cancellation refused`, com o `ator` e o `resultado`, sem texto livre, placa nem links.
  - O `prazos` loga `saga command resent` (com o número do `reenvio`), `saga deadline exhausted` (em aviso e com a `falha` quando a saga para), `saga deadline refused`, `saga deadlines paused: the os.eventos consumer is away`, `os.eventos has no consumer` e `deadline cycle done` (só com trabalho). Nunca os `dados` do comando.
  - A retomada audita em `saga resumed` e `saga resume refused`, com o `ator` e o `resultado`.

### Testes da saga

- **Unitários.**
  - Cada linha da tabela de etapas no agregado e no handler (etapa, status, comando e `dados`, prazo, resumos da OS e ator) e as recusas do agregado: `tests/unitarios/ordem_servico/test_saga.py` e `test_orquestrador.py`.
  - A matriz da classificação, gerada de um oráculo escrito a partir da RFC: 12 etapas x 10 perfis da OS x 23 tipos no agregado (`test_classificacao.py`) e, no handler, com a OS de cada etapa e com a OS encerrada (`test_orquestrador.py`).
  - As conferências do agregado, as invariantes e o vocabulário do gatilho (`test_transicoes.py`); o envio e a política do relógio (`test_contexto_de_envio.py`).
  - As compensações: as 11 linhas da tabela da RFC-004 seção 4.4 (`test_compensacao.py`), cada compensação pendente por cada resposta, casada ou não pelo `causation_id` (`test_orquestrador_compensacoes.py`), as 12 células das transições do resumo do pagamento (`test_resumos.py`) e o cancelamento em cada etapa, com o rótulo dos oito códigos (202, repetido, 409 e 422: `test_use_cases_os.py`).
- **Limites.** `test_limites_do_dominio.py` mede o que o desenho manda: no máximo 300 linhas por módulo de `aplicacao/saga/` e da `OrdemDeServico`, e 10 métodos públicos que mudam estado em cada um dos dois agregados (a conta inclui os mixins de leitura; as propriedades e as duas consultas da `LeituraDaSaga` não contam).
- **Propriedade** (`test_propriedades_da_saga.py`).
  - Em mil sementes, participantes simulados respondem a cada comando com eventos embaralhados, repetidos com id novo e atrasados, com retry nos atrasos das filas, e o atendente tenta cancelar e entregar a OS em pontos aleatórios.
  - Cada roteiro escolhe uma falha de negócio (ou nenhuma), se o provedor recusa o estorno, se o cliente paga depois de a cobrança ser encerrada e se o estorno automático chega antes da resposta ao `EstornarPagamento` (a ordem trocada entre duas réplicas do consumidor).
  - Só a falha transitória escapa, o par etapa e status fica na tabela, o repetido não muda nada, a OS fica cancelada se e só se a saga está compensada, a compensação seguinte só sai depois da resposta da anterior (o estorno nunca antes da execução cancelada), o gatilho de todo registro é do vocabulário, o plano é o da tabela da RFC e nada vai para a DLQ.
  - Uma guarda confere que o gerador produz de fato obsoletos, repetidos, adiantados, estornos automáticos, a ordem trocada e eventos fora do fluxo.
- **Prazos** (a matriz abaixo roda no `make test`).
  - Unitários: o reenvio de 1 a 5 em cada etapa técnica, o esgotamento em cada uma e em `compensando`, a retomada e tudo o que cada uma recusa sem mudar nada (`test_prazos_da_saga.py`); o ciclo do `VencerPrazos` com relógio manual, sem consumidor e com a saga que o domínio recusa (`test_vencer_prazos.py`); a retomada pelo caso de uso e pela rota (`test_retomar_compensacao.py`, `test_router_os.py`); a configuração: a soma dos atrasos de retry (381 s) contra a janela de reenvio (720 s), no código, no compose e nos três overlays (`test_configuracao_dos_prazos.py`); os manifestos do `prazos` (`test_contrato_dos_manifestos.py`, `test_manifestos.py`).
  - Propriedade (`test_propriedades_dos_prazos.py`): em mil sementes, participantes mudos, lentos, que perdem o envio ou que recusam o estorno, com o consumidor do OS fora por uma janela, cancelamento e retomada em pontos aleatórios; o par etapa e status fica na tabela, o prazo existe se e só se há comando com prazo em voo, os reenvios nunca passam do máximo, o ciclo pausado não muda nada, cada reenvio é do mesmo comando e só sai depois do prazo, o esgotamento só vem com os reenvios gastos e com participante mudo a saga nunca conclui.
  - PostgreSQL real (`tests/integracao/ordem_servico/test_prazos_no_banco.py`): a consulta com `SKIP LOCKED` (linha pendente, `dead`, entregue há pouco, último envio, ordem, saga travada por outra transação), a transação única com a falha forçada no commit, duas réplicas em threads sobre doze sagas, o consumidor que perde na versão, o gauge de prazo vencido nos dois sentidos e os 12 minutos com os padrões (relógio controlado, ciclo de 30 s, compensação em 720 s, falha em 1440 s e retomada).
  - RabbitMQ real (`tests/integracao/mensageria/test_prazos_com_broker.py`): participante mudo (cinco reenvios com id novo e os mesmos dados, depois a compensação), duas réplicas do `prazos`, estorno recusado, falha e retomada (resposta ao reenvio e ao original), `os.eventos` sem consumidor e relay parado sem gastar reenvio, dois consumidores com a resposta tardia ao original e a presença do consumidor.
- **BDD de componente.** `tests/bdd/saga_atendimento.feature`, em português, com o OS sobre o PostgreSQL de teste e um barramento em memória no lugar do RabbitMQ; os passos ficam em `tests/integracao/test_saga_atendimento.py`, ao lado das fixtures do banco. Cobre o caminho feliz, o adiantado, o repetido e o obsoleto, cada falha de negócio, o cancelamento em cada etapa (plano da RFC, passo em voo e ordem das compensações), repetido, recusado depois do início e com a OS já cancelada, a corrida do pivot nos dois sentidos, o estorno recusado, a resposta atrasada e o estorno automático. O prazo anda no banco (o prazo gravado e a entrega recuam 121 s a cada "o prazo de resposta vence") e o `prazos` roda um ciclo: cobre o reenvio até o máximo em cada etapa técnica e a compensação por prazo técnico, a resposta ao original depois do reenvio, o consumidor fora, o relay parado, a falha na compensação, a retomada (inclusive depois do estorno recusado e com a resposta ao envio original) e a retomada recusada fora da falha.
- **Integração** (`tests/integracao/mensageria/test_saga_com_broker.py`): relay, consumidor e RabbitMQ reais com participantes falsos.
  - O caminho feliz até `ENTREGUE` num trace só (com a árvore de spans conferida), o adiantado passando pela retry, a reentrega do mesmo evento sem repetir o comando, as recusas na DLQ com o motivo e a placa e a descrição só no comando da Execução.
  - A transação única: com o `PagamentoConfirmado`, o commit recusado depois de gravadas a OS, o histórico, a saga, o comando e o pedido de e-mail desfaz tudo, inclusive o registro da mensagem; a entrega pela API com o commit recusado deixa a OS `finalizada` e sem pedido de e-mail.
  - O cancelamento depois do pagamento, com as cinco compensações uma por vez e o span do cancelamento no trace da saga; a falta de peça (orçamento e diagnóstico compensados, cada um causado pela resposta anterior); o estorno recusado (a sequência para e o comando em voo fica sem prazo).
  - O redrive da DLQ com o shovel do platform: antes de implantar a versão nova, a versão anterior recusa a mensagem de novo; depois, o consumidor novo trata o evento que ela recusou.

## Notificações por e-mail e LGPD

### E-mail ao cliente

O cliente recebe um e-mail a cada mudança de situação da OS ([RFC-004](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/rfc/fase4/rfc-004-microsservicos-saga.md) seção 3 e [ADR-036](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/036-mensageria-rabbitmq.md), "E-mail ao cliente"). O pedido nasce na transação da transição e o envio é de outro processo, então SMTP lento ou fora não atrasa comando, resposta nem a saga.

```mermaid
flowchart LR
    t["transição da OS<br/>(consumidor ou API)"] -->|"mesmo commit"| n[("notificacoes")]
    n -->|"NOTIFY + poll"| nt["notificador<br/>python -m src.notificador"]
    nt -->|"OS e contato do cliente"| db[("PostgreSQL")]
    nt -->|"SMTP, prazo de 5 s"| mp["Mailpit"]
```

- **Quando.** Cada mudança de status da OS pede um e-mail do modelo do novo status, menos a abertura (`recebida`) e `aguardando_pagamento`, cujo e-mail é o do checkout, que o `PagamentoSolicitado` pede (ele não muda o status). O consumidor grava o pedido junto com o fato da saga e `mensagens_processadas`; a API, na entrega. O pedido de cancelamento (202) não muda a OS e não manda e-mail: o da `cancelada` sai quando a última compensação responde.

  | Modelo | Quando | Texto |
  |---|---|---|
  | `situacao` | `em_diagnostico`, `aguardando_execucao`, `em_execucao`, `finalizada`, `entregue` | o rótulo da situação |
  | `orcamento` | `aguardando_aprovacao` | total, validade e o `link_decisao` do Billing |
  | `pagamento` | `PagamentoSolicitado` | valor, validade e o `checkout_url` do Billing |
  | `cancelamento` | `cancelada` | o rótulo do código da compensação (nunca o texto do atendente) |

- **O pedido.** A tabela `notificacoes` (migração `004`, só expansão) guarda `ordem_id`, `modelo`, `status_da_ordem` e o `motivo` com o código, mais a situação da linha (`pendente`, `entregue` ou `dead`), as tentativas e o lease. Não tem endereço, nome, link nem texto livre: o nome e o endereço (extraído do campo livre `contato`) saem do cadastro, por uma projeção de duas colunas, sem carregar o agregado nem decifrar o documento, e por isso o `notificador` não recebe a `ENCRYPTION_KEY`; os links saem do resumo do orçamento e do pagamento que a OS guarda. A `outbox` não muda e continua só com comandos AMQP: um relay de uma versão anterior nunca vê um e-mail.
- **Entrega** (`python -m src.notificador`, a mesma imagem). Uma linha por vez, com o claim da outbox: `FOR UPDATE SKIP LOCKED` e lease com fencing, na ordem de cada OS (um e-mail em espera segura só os seguintes da mesma OS). A OS e o contato são lidos numa transação de leitura que fecha antes do envio. O envio é SMTP puro (`smtplib`, prazo de 5 s por operação); falha conta tentativa e volta depois de 1, 4, 16 e 64 s, e a quinta falha da linha a deixa `dead`, visível na tabela e no log. Não há métrica nem alerta próprios: o e-mail é complementar e não segura a saga. Cliente anonimizado (o contato virou marcador), sem e-mail no contato ou sem o resumo que o modelo pede: a linha é dada como entregue, sem envio, com um aviso no log. Uma vez por hora apaga as linhas entregues há mais de 7 dias e as `dead` há mais de 30.
- **Sem dado pessoal no log.** O log leva só `correlation_id` (a OS), `modelo` e o resultado: nunca o endereço, o nome, o `link_decisao` ou o `checkout_url`, e o `ultimo_erro` da linha é o nome da exceção. O processo não abre span nem tem métrica própria; o `/metrics` na 9100 traz só as métricas padrão do Python.
- **Saúde.** Heartbeat a cada volta do laço e arquivo de pronto enquanto o banco responde; SMTP fora não o tira de pronto. Encerramento gracioso no SIGTERM.

| Variável | Padrão | Uso |
|---|---|---|
| `SMTP_HOST` | `localhost` só em development/test | servidor SMTP (`mailpit` no compose; `mailpit.pytstop-plataforma.svc.cluster.local` no cluster). Fora de development/test é obrigatório: sem ele o notificador não sobe, em vez de mandar todo e-mail ao `localhost` do pod e deixá-lo `dead` |
| `SMTP_PORT` | 1025 | porta SMTP (1 ou mais); valor inválido aborta o boot |
| `SMTP_FROM` | `oficina@pytstop.local` | remetente |

No compose, o Mailpit mostra a caixa de entrada em `http://localhost:8026` (`MAILPIT_UI_PORT`) e recebe o SMTP em `localhost:1026` (`MAILPIT_SMTP_PORT`), fora das portas do compose do platform (8025 e 1025), para os dois conviverem. Sem credencial nem TLS: o Mailpit da demonstração não pede. **SMTP externo** exigiria STARTTLS e credencial por Secret, que o adapter ainda não implementa (a política de saída `notificador-saida` também teria de mudar); o link de decisão e o de pagamento vão no corpo do e-mail, então só um servidor de confiança deve recebê-lo.

**Rede no Kubernetes.** O notificador é o único processo do serviço com política de saída (`notificador-saida`: DNS, PostgreSQL do namespace e Mailpit na 1025), porque é o único com destino de rede de terceiro configurável (`SMTP_HOST`) e com escrita em todo o banco; a API, o relay, o consumidor e o `prazos` ficam sem política de saída, porque os destinos deles são fixos (o PostgreSQL do namespace e o RabbitMQ e o Jaeger da plataforma; o `prazos` só usa o PostgreSQL e o RabbitMQ). Um `SMTP_HOST` fora da regra não é recusado, apenas expira (os 5 s de cada tentativa) e os e-mails vão para `dead`: trocar o servidor exige ajustar a política. A política foi exercitada num kind (Mailpit:1025 e banco:5432 abrem; Mailpit:8025, broker:5672, a API do namespace e a internet fecham; o DNS resolve), mas não tem prova negativa no smoke do platform, e a regra de entrada no Mailpit também é do platform.

### Operação do e-mail

- **Linha `dead`.** Sem métrica nem alerta próprios, a linha que esgotou as cinco tentativas fica na tabela e no log (`notification send failed; row dead`, com o `correlation_id` da OS). Para listá-las:

  ```sql
  SELECT id, ordem_id, modelo, status_da_ordem, tentativas, ultimo_erro
  FROM notificacoes WHERE situacao = 'dead' ORDER BY id;
  ```

- **Reenvio.** Depois de corrigir a causa (o SMTP voltou, o endereço do cliente foi ajustado), devolva a linha à fila; o notificador a pega no próximo ciclo (o `UPDATE` não emite `NOTIFY`, então vale o poll de 5 s). Só vale até a retenção de 30 dias, quando a limpeza horária apaga a linha:

  ```sql
  UPDATE notificacoes
  SET situacao = 'pendente', tentativas = 0, ultimo_erro = NULL, proxima_tentativa_em = now()
  WHERE id = 42 AND situacao = 'dead';  -- o id da consulta acima
  ```

  A `dead` não segura a fila da OS, então o e-mail reenviado chega depois dos seguintes da mesma OS que já saíram.
- **Ordem de implantação e rolling update:** ver [Implantação e DLQ](#implantação-e-dlq).

### Eliminação de dados (LGPD)

`DELETE /api/v1/clientes/{id}/dados-pessoais` (admin) só vale sem OS ativa e, numa transação só, anonimiza:

- o cliente, os veículos e o texto livre das OS (descrição do problema e motivos de cancelamento);
- a placa e a descrição do `SolicitarDiagnostico` que ficaram na outbox, em qualquer situação da linha (pendente, entregue ou `dead`), achadas pelo `correlation_id` das OS do cliente: a placa vira `AAA0A00`, que respeita o padrão do schema (a linha pendente continua publicável e o relay segue validando o envelope), e a descrição vira `[removido]`; marca e modelo não identificam o titular e ficam;
- a placa guardada na Execução, por um comando `AnonimizarVeiculo` por veículo, fora da saga (o `correlation_id` é o `veiculo_id`, sem resposta nem prazo; a falha definitiva vai para a `execucao.comandos.dlq`). O `DELETE` repetido responde 204 de novo e não reenvia o comando: só os veículos que ainda guardam a placa original geram um.

Se a transação desfaz, nenhum comando sai e nada é anonimizado. A cópia do comando na `execucao.comandos.dlq` (7 dias) mantém a placa original: depois de um apagamento, **não faça redrive de DLQ de comando**, que recriaria a placa na Execução.

**Reenviar só um `AnonimizarVeiculo`.** Como o `DELETE` repetido não reemite o comando, o que cair na `execucao.comandos.dlq` (a Execução não anonimizou a placa) precisa de reenvio próprio. O comando leva só o `veiculo_id` (o `correlation_id` da mensagem na DLQ), sem placa. Publicá-lo à mão no exchange de comandos não funciona: o broker só aceita um `user_id` igual ao usuário da conexão, a Execução só aceita `os` como produtor desse tipo, e o usuário `os` não entra na API de gerenciamento (publicar como `admin` com `user_id` `os` volta 406, e sem `user_id` a Execução recusa o comando). O reenvio grava o comando de novo na outbox, na transação e com o contrato do erasure, e o relay o publica como `os`. Troque o `VEICULO_ID` pelo da mensagem na DLQ (a senha do banco já está no ambiente do pod) e rode, com prazo:

```bash
VEICULO_ID=3277db7e-8283-4dd9-89e7-df3eeacb8710
timeout 60 kubectl --context kind-pytstop-p4 -n pytstop-os exec deploy/os-service-consumidor -- python scripts/reenviar_anonimizacao.py "$VEICULO_ID"
```

No macOS sem o `timeout` do coreutils, troque-o por `perl -e 'alarm shift; exec @ARGV' 60`. O script sai com 1, sem enviar nada, para um veículo que não foi anonimizado (o comando de um veículo vivo apagaria a placa dele na Execução). A cópia na DLQ expira sozinha em 7 dias, e o redrive da fila inteira continua proibido: a mesma DLQ pode guardar um `SolicitarDiagnostico` com a placa original.

### Testes

- **Unitários.** O modelo de cada status e a exaustividade da tabela (`test_notificacoes.py`), o texto de cada e-mail, o que fica de fora dele e o aviso sem dado pessoal (`test_emails.py`), o adapter SMTP (`test_email_adapter.py`), o pedido gravado por cada evento do fluxo, de cada compensação e da corrida do pivot (`test_orquestrador_notificacoes.py`), um comando por veículo e a transação desfeita (`test_lgpd_use_cases.py`) e os manifestos e o compose do `notificador`.
- **Propriedade** (`test_propriedades_da_saga.py`): em mil sementes, com eventos embaralhados, repetidos e atrasados, um pedido de e-mail por transição de status e nunca dois, e o código do motivo só no cancelamento.
- **BDD** (`tests/bdd/notificacoes_do_cliente.feature`): os pedidos do caminho feliz, o cancelamento, a falha de negócio, o texto de cada modelo e o cliente apagado.
- **Integração.** A tabela, o claim (que só pega o modelo, o status e o motivo que esta versão conhece), o fencing de cada desfecho, o lease vencido que não devolve linha `entregue` nem `dead`, a ordem por OS, os atrasos até `dead` e a retenção (`test_fila_de_notificacoes.py`); o `notificador` com PostgreSQL e Mailpit reais (`test_notificador.py`); a saga com relay, consumidor, RabbitMQ, notificador e Mailpit, com um e-mail por gatilho, SMTP fora sem atrasar a saga e nenhum dado pessoal no log, nos spans e nas métricas (`test_notificacoes_da_saga.py`); o erasure com um comando por veículo, a outbox anonimizada e a linha anonimizada publicada pelo relay (`test_erasure_outbox.py`).

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

Os manifestos Kubernetes ficam em `k8s/`: `base/` e três overlays, `kind` (local), `kind-ci` (o CD, com uma réplica só da API) e `k3s` (a VM na Azure). Tudo vai para o namespace `pytstop-os`, com Pod Security `restricted`. Nenhum Secret está nos manifestos: os do serviço nascem no `make deploy` do platform ([segredos gerados](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#segredos-gerados)) e entram por `secretKeyRef` ([ADR-042](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/042-cicd-e-deploy-kubernetes.md)).

| Objeto | O que é |
|---|---|
| Deployment e HPA `os-service-api` | A API na porta 8000, com o `/metrics` na mesma porta; o HPA, por CPU (70%), vai de 1 a 2 réplicas no `kind`, a 1 no `kind-ci` e a 3 no `k3s` |
| Deployments `os-service-relay`, `os-service-consumidor`, `os-service-prazos` e `os-service-notificador` | Relay, consumidor, prazos da saga e notificador dos e-mails, uma réplica cada, com o `/metrics` na 9100. O notificador só recebe a senha do banco e o `SMTP_*` do ConfigMap (o Mailpit da plataforma, na 1025): sem `RABBITMQ_URL` e sem `ENCRYPTION_KEY` |
| StatefulSet e Service headless `os-postgres` | PostgreSQL 16.15 com volume de 1Gi (5Gi no `k3s`) e o `postgres_exporter` como sidecar, na 9187 |
| Job `os-migracao` | `alembic upgrade head` e a semente do admin, antes dos Deployments |
| Service `os-service` | O endereço interno da API, o do `JWKS_URL` de Billing e Execução |
| `borda.yaml` | Os Services e Ingress do Kong, os mesmos objetos do [exemplo do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/k8s/exemplos/borda-os-service.yaml) com o cabeçalho deste repositório: `/os/api/v1`, `/os/docs`, `/os/openapi.json` e o JWKS pela borda, e `/os/api/v1/admin` e `/os/metrics` respondendo 404 sem chegar ao serviço |
| NetworkPolicy | Nega toda entrada no namespace e libera só a API para o Kong, o Prometheus e as APIs de Billing e Execução (que leem o JWKS), relay, consumidor, prazos e notificador para o Prometheus, o PostgreSQL para os pods do namespace e o exporter para o Prometheus. A saída do notificador também é restrita (`notificador-saida`): só o DNS, o PostgreSQL do namespace e o Mailpit da plataforma na 1025. É a única política de saída do serviço, por critério: o notificador é o único processo com destino de rede de terceiro configurável (`SMTP_HOST`) e escreve em todo o banco; os demais, o `prazos` inclusive, têm destinos fixos (o PostgreSQL do namespace e o RabbitMQ e o Jaeger da plataforma; o `prazos` só usa o PostgreSQL e o RabbitMQ). Trocar o `SMTP_HOST` exige ajustar essa política. A regra de entrada no Mailpit é do platform |

Todo pod roda sem root (uid 1001; 999 no banco e 65534 no exporter), sem escalar privilégio, sem capability, com seccomp `RuntimeDefault`, raiz somente leitura (`/tmp` em `emptyDir`) e sem token de ServiceAccount. O `make manifests` valida os três overlays com o kubeconform, contra os schemas do Kubernetes 1.35 e com `Secret` reprovado, e com o `trivy config`, sem achado HIGH nem CRITICAL; o job `build` do CI o roda em todo PR.

### Implantar no kind

Pré-requisitos: Docker, kind, kubectl, jq e curl (o `implantar-servicos.sh` e o smoke usam os cinco; o `make manifests` e o `make test` precisam só do kubectl e do Docker) e o platform clonado ao lado deste repositório. O primeiro comando sobe o cluster, a plataforma e os Secrets do serviço; o segundo implanta a imagem deste commit, com o Job de migração e os rollouts; o terceiro roda o smoke:

```bash
make -C ../postech-sw-arch-p4-platform kind-up deploy
make kind-deploy
../postech-sw-arch-p4-platform/scripts/ci/smoke-servicos.sh os-service
```

O `make kind-deploy` chama o `scripts/ci/implantar-servicos.sh` do platform, o mesmo do CD (o contrato de implantação dos serviços está no [README do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#contrato-com-os-serviços); `PLATFORM=<caminho>` aponta outro clone): ele constrói a imagem `pytstop-os-service` com o commit como tag, a carrega no kind, aplica o overlay `kind` com essa imagem e espera o banco, o Job e cada Deployment. O smoke confere pela borda a saúde em 200 e o `/metrics` em 404, o `up` de cada pod no Prometheus e a NetworkPolicy barrando o banco a quem vem de outro namespace. O Swagger fica em `https://localhost/os/docs` (com o certificado padrão do Kong).

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
- **Processo `prazos`:** `k8s/base/prazos.yaml`, com o mesmo molde do relay e do consumidor (mesma imagem, `python -m src.prazos`, o initContainer `aguarda-migracao`, o Secret do banco e o do RabbitMQ, e sem a `ENCRYPTION_KEY`, que só a API recebe). Uma réplica: duas não se atropelam (`SKIP LOCKED`), mas uma basta para o intervalo de 30 s. `SAGA_PRAZO_RESPOSTA_SEGUNDOS`, `SAGA_MAX_REENVIOS` e `PRAZOS_INTERVALO_SEGUNDOS` vêm do ConfigMap `os-service`. O encerramento dá 30 s à saga em curso. A compensação só vai para o cluster junto com ele ([Implantação e DLQ](#implantação-e-dlq)).
- **Sondas:** a API tem liveness em `/api/v1/saude` e readiness em `/api/v1/saude/pronto`; relay, consumidor, `prazos` e notificador, sem curl na imagem, têm liveness pela idade do heartbeat (até 90 s; 120 s no `prazos`, onde a startup é a mesma) e readiness pelo arquivo de pronto (o do notificador é o banco respondendo; o do `prazos`, o broker de pé), os de `/tmp` (seção [Observabilidade, saúde e encerramento](#observabilidade-saúde-e-encerramento)). A sonda de startup dá 1 min ao boot de cada processo e 2 min ao primeiro init do banco.
- **Imagens de terceiros:** o job `trivy` do workflow Security reprova a imagem do serviço com HIGH ou CRITICAL corrigíveis. As duas que o serviço põe no cluster, `postgres:16.15` e `postgres-exporter:v0.20.1`, só têm relatório no summary do job, sem reprovar, porque a correção depende de um build novo do fornecedor (`scripts/ci/relatorio-trivy-terceiros.sh`; a falha da varredura em si, como um pull recusado, vira aviso). O risco aceito, medido com o trivy 0.72.0: a `postgres:16.15` tem 1 CRITICAL e 24 HIGH, todos no binário Go `gosu`, que o pod, já como uid 999, não executa, e a chave de teste `ssl-cert-snakeoil` do Debian, que o PostgreSQL daqui não usa (sem SSL); o exporter tem 16 HIGH na biblioteca padrão do Go do próprio binário, e sua porta 9187 só chega ao Prometheus. A política é subir a versão quando o fornecedor publicar um build corrigido, junto da tabela de versões do platform.

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

A chave RSA do JWT gira pelas etapas da [rotação da chave](#rotação-da-chave), e cada etapa repete os mesmos três passos, com `$chaves` o diretório da rotação:

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

## Qualidade

- `make check`: `uv.lock` em dia, ruff (lint e formato), import-linter, mypy strict, bandit e pytest.
- `make manifests`: kubeconform e trivy config dos três overlays de `k8s/`, mais a conferência de que todo `PASSWORD` do `papeis.sql` vem de uma variável do psql.
- `make audit`: pip-audit das dependências de runtime, com os extras da imagem.
- `make smoke`: a imagem pelo entrypoint real. Confere a readiness, o login do admin semeado validado pelo JWKS (e a assinatura adulterada recusada), o log de boot em JSON, o `http_request` estruturado e nenhum access log do uvicorn, a imagem de produção (usuário 1001, `ENVIRONMENT=production`, sem header `server`) e o relay, o consumidor, o `prazos` e o notificador prontos (`/metrics` respondendo, `outbox_pendentes` numérico, as séries de reenvio da saga em zero no `prazos`, consumidor inscrito em `os.eventos` com prefetch 1 no `list_consumers`) e um e-mail entregue ao Mailpit pelo notificador (uma OS aberta pela API, um pedido gravado em `notificacoes` e a caixa lida pela API do Mailpit, sem endereço nem nome no log dele); depois, `down -v`.

```bash
make check
make manifests
make audit
make smoke
```

`make test` (ou `uv run pytest`) roda os testes unitários, os de contrato, o BDD de componente da saga e os de integração contra um PostgreSQL e um RabbitMQ 4.3.6 efêmeros (testcontainers, Docker necessário), com gate de cobertura de 90% (`.coveragerc`). Os testes dos manifestos leem o render do `kubectl kustomize`, então o `kubectl` precisa estar no PATH: sem ele a suíte encerra de uma vez, com essa mensagem, em vez de falhar teste a teste. O teste de checksum dos contratos baixa o platform do GitHub e precisa de rede: offline, rode com `-m "not rede"` (a cobertura do gate continua valendo). O schema dos testes de integração é criado pela própria migração Alembic (as migrações só acrescentam, e o rollback de uma imagem não roda `downgrade`, que apaga dados: o da `003` apaga a tabela `sagas` e a coluna `ator` do histórico; a `003` desiste em 5 s de um lock que uma leitura longa segure), e o broker de teste sobe com a topologia de `contratos/rabbitmq/`, só com o TTL das filas de retry reduzido a 100 ms para o ciclo inteiro de tentativas caber num teste.

No GitHub, o workflow `CI` (`.github/workflows/ci.yml`) roda os mesmos gates em todo PR, publica `coverage.xml`, `htmlcov/` e o JUnit como artefato com o resumo de cobertura por pacote no summary, passa o SonarQube com quality gate versionado (`.sonar/quality-gate.json`), valida os manifestos (`make manifests`), builda a imagem e roda o `make smoke`. O workflow `Security` roda pip-audit (dependências de runtime com os extras da imagem), gitleaks (o histórico do commit testado, inclusive o que um commit seguinte apagou) e trivy (imagem), em todo PR e toda segunda-feira.

## Repositórios da fase 4

| Repositório | Papel |
|---|---|
| [postech-sw-arch-p4-os-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service) | Ordens de serviço, clientes e veículos, usuários internos e orquestrador da saga |
| [postech-sw-arch-p4-billing-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service) | Orçamentos, pagamentos via Mercado Pago e tabela de preços |
| [postech-sw-arch-p4-execution-service](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-execution-service) | Fila de diagnóstico e execução e estoque de peças |
| [postech-sw-arch-p4-platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform) | Infraestrutura compartilhada, testes E2E, arquitetura global e entrega |

A `main` é protegida desde o primeiro commit: toda mudança entra por pull request com squash.
