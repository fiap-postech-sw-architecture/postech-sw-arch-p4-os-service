# Origem do código e escopo do serviço

Este documento detalha a seção [Decisões e rastreabilidade](../README.md#decisões-e-rastreabilidade) do README.

## Origem do código

O código parte de um recorte da `main` do PytStop da fase 3 (`postech-sw-arch-p3`): o commit [`08dcffe`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/08dcffe6365ece594f438cdbc4c5eef1d88ebfb1) mais o [`fc06263`](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/commit/fc06263d1f99747145e5a354b817905c59637ebd), a normalização ASCII de CPF e CNPJ (p3 #32). Entraram só os contextos deste serviço: `compartilhado`, `cliente_veiculo`, `autenticacao` e `ordem_servico`.

As referências `p3 #N` no código apontam para issues e PRs daquele repositório. Os identificadores `RF-018`, `ADR-020` e `TD-017` seguem a numeração contínua das fases anteriores ([requisitos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/requisitos), [ADRs](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/arquitetura/adr) e [dívida técnica](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p3/tree/main/docs/tech-debt) do p3). A fase 4 começa em RF-028 e ADR-034.

A RFC-004 e os ADRs 034 a 043 ficam em [`docs/arquitetura` do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/tree/main/docs/arquitetura). O código os cita nos comentários como `RFC-004 secao N` e `ADR-0NN`. Os entregáveis RNF-050 a RNF-052 vêm da [gap analysis da fase](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/requisitos/fase4/gap-analysis-fase-4.md).

## Escopo do serviço

- Ciclo de vida da OS: `RECEBIDA → EM_DIAGNOSTICO → AGUARDANDO_APROVACAO → AGUARDANDO_PAGAMENTO → AGUARDANDO_EXECUCAO → EM_EXECUCAO → FINALIZADA → ENTREGUE`, com `CANCELADA` antes do início da execução. A OS guarda o histórico de mudanças de status, o resumo do orçamento e do pagamento (que vivem no Billing) e uma versão para lock otimista (escrita concorrente responde 409).
- Saga de atendimento orquestrada pelo OS Service. A abertura da OS inicia a saga, e o consumidor conduz o caminho feliz até `FINALIZADA`. As falhas de negócio e o cancelamento disparam as compensações, uma por vez, até a OS `CANCELADA` ou até a falha da compensação. A etapa fica visível na OS e na rota de operação.
- Prazos da saga: o processo `prazos` reenvia o comando sem resposta e compensa o passo que esgota os reenvios. Quando a compensação esgota os reenvios sem resposta, ele leva a saga ao estado `falha_na_compensacao`, de onde o admin a retoma ([Saga](saga.md#saga)).
- API (o OpenAPI completo está em [`docs/api/`](api/)):
  - OS: `POST` e `GET /api/v1/ordens-de-servico`, `GET /{id}` (com a etapa da saga), `GET /{id}/historico` (com os passos da saga), `POST /{id}/cancelamento` (202 com a etapa, ou 409) e `POST /{id}/entrega`.
  - Saga (admin): `GET /api/v1/sagas/{ordem_id}` e `POST /api/v1/sagas/{ordem_id}/compensacao`.
  - Clientes e veículos, com as rotas da LGPD.
  - Autenticação: `/api/v1/autenticacao/*` e o JWKS em `GET /.well-known/jwks.json`.
  - Acompanhamento público: `POST /api/v1/publico/acompanhamento`, com placa e documento no corpo.
  - Operação: `GET /api/v1/saude` (liveness), `GET /api/v1/saude/pronto` (readiness: 503 se o banco não responder em 2 s), `GET /api/v1/ordens-de-servico/metricas` (admin: total, OS por status e permanência média em cada status) e `GET /metrics` (sempre ligado).
  - Swagger em `/docs`.
- Mensageria com RabbitMQ: outbox transacional no envelope do contrato, relay com confirmação do broker e consumidor idempotente da fila `os.eventos`, com retry por atraso e DLQ ([Mensageria](mensageria.md#mensageria)).
- Manifestos Kubernetes em `k8s/`, com o PostgreSQL do serviço, o Job de migração e a borda no Kong ([Implantação](como-rodar.md#implantação)).
- E-mail ao cliente a cada mudança de situação da OS, pelo processo `notificador` e o Mailpit. A eliminação de dados da LGPD também anonimiza a placa na Execução e nos comandos da outbox ([Notificações por e-mail e LGPD](mensageria.md#notificações-por-e-mail-e-lgpd)).
- A compensação vai para o cluster junto com o processo `prazos`. Sem ele, um participante que nunca responde deixa a saga em `compensando` sem reenvio nem alerta de prazo. Os manifestos têm os dois ([Implantação e DLQ](como-rodar.md#implantação-e-dlq)).
- Observabilidade: o `/metrics` de cada processo, spans do OpenTelemetry (exportados para o Jaeger com `OTEL_ENABLED=true`) e `trace_id` nos logs. Os gauges por consulta (saga, outbox e OS por status) saem do coletor da API. `GET /api/v1/ordens-de-servico/metricas` (admin) devolve os mesmos números das OS ([Observabilidade](observabilidade.md#observabilidade)).
