# Observabilidade

Este documento detalha a seção [Observabilidade](../README.md#observabilidade) do README.

Métricas, traces e logs do OS Service seguem o [ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md). O catálogo das métricas dos três serviços (tipo, rótulos, quem emite, quando conta e a unidade), os painéis do Grafana e as regras de alerta ficam no [README de observabilidade do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#catálogo-de-métricas), onde um teste reprova o painel ou o alerta que consulte uma métrica fora do catálogo ou um rótulo que ela não tem. Esta seção diz o que o OS emite e por quê.

Dashboards relacionados no Grafana: [Atendimento](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-negocio-atendimento.json), [Saga](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-saga.json), [Mensageria](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-mensageria.json), [Serviços](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-servicos.json), [Bancos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/dashboards/pytstop-bancos.json). Consulte a documentação dos [painéis de negócio](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/negocio.md) e dos [técnicos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md).

## Métricas

Cada processo serve o `/metrics`: a API na 8000, junto do resto da API, e o relay, o consumidor, o `prazos` e o notificador na `METRICS_PORT` (9100). O `/metrics` da API está sempre ligado e fica fora da borda: o Ingress do Kong responde 404 a `/os/metrics` sem chegar ao serviço (`k8s/base/borda.yaml`, conferido pelo smoke do platform), e o Prometheus raspa cada pod direto, pelas anotações `prometheus.io/*` dos Deployments.

| Processo | Série | Tipo e rótulos | Quando |
|---|---|---|---|
| API | `http_request_duration_seconds` | histograma, `method`, `rota` (o template, `nao_roteada` sem rota casada), `status` | fim de cada request, inclusive a que estoura (conta como 500) |
| API | `pytstop_os_criadas_total` | contador | OS gravada: conta depois do commit da transação raiz (rollback, conflito de versão ou commit que falha não contam) |
| API | `pytstop_os_por_status` | gauge por consulta, `status` (os nove, com zero onde não há OS) | raspagem |
| API | `pytstop_os_tempo_medio_status_segundos` | gauge por consulta, `status` | raspagem; só os status com permanência concluída na janela, ausente (nunca zero) nos demais |
| API | `pytstop_outbox_mais_antiga_segundos` | gauge por consulta | raspagem; idade da linha pendente mais antiga da outbox, 0 sem pendente. Vem da API: segue exportando com o relay parado, que é o que mantém o alerta "Outbox parada" de pé |
| API | `pytstop_saga_ativas{etapa}`, `pytstop_saga_etapa_mais_antiga_segundos{etapa}`, `pytstop_saga_prazo_vencido_segundos`, `pytstop_saga_coletor_disponivel`, `pytstop_saga_coletor_falhas_total` | gauges por consulta e contador | raspagem ([Observabilidade da saga](#observabilidade-da-saga)) |
| API, consumidor, `prazos` | `pytstop_saga_iniciadas_total`, `pytstop_saga_finalizadas_total{resultado}`, `pytstop_saga_compensacoes_total{motivo}`, `pytstop_saga_etapa_duracao_segundos{etapa}` | contadores e histograma | depois do commit, no processo que tira a saga da etapa |
| relay | `pytstop_mensagens_publicadas_total{tipo}`, `outbox_pendentes`, `outbox_dead` | contador e gauges | confirmação do broker; raspagem |
| consumidor | `pytstop_mensagens_consumidas_total{tipo,resultado}` | contador (`processada`, `duplicada`, `ignorada`, `retry`, `dlq`; `tipo` do catálogo ou `desconhecido`) | mensagem resolvida, depois do commit e do ack |
| relay, consumidor | `pytstop_reconexoes_ao_broker_total{componente}` | contador (`relay` ou `consumidor`; o rótulo é `componente`, e não `processo`, porque o Prometheus já põe `processo` em todo alvo e o da métrica viraria `exported_processo`) | cada espera antes de reconectar ao broker |
| `prazos` | `pytstop_saga_reenvios_total{comando}`, `pytstop_saga_prazos_esgotados_total{comando}`, `pytstop_prazos_ultimo_ciclo_timestamp_seconds` | contadores e gauge | depois do commit da saga; fim de cada ciclo que terminou (ocioso, com trabalho ou pausado: o que falha por banco fora não o atualiza) |

Toda série de rótulo fechado nasce com zero no boot do processo que a emite (a lista fechada vem do catálogo dos contratos, dos códigos da saga e do `StatusOrdem`), para o `increase()` dos painéis não perder o primeiro evento de cada série. Fora disso o contador conta quando o caso de uso comita; no consumidor, como as demais métricas dele, depois do ack, pelo menos uma vez.

**Os gauges por consulta** (saga, idade da outbox e OS por status) são do coletor da API (`coletor_da_api.py`), que só existe com a API de pé:

- **A raspagem não pendura.** O Prometheus do platform desiste em 10 s, e o alvo viraria `up=0` justo no incidente do banco. A raspagem abre uma conexão (`connect_timeout` de 1 s) e roda as consultas nela, cada uma com `statement_timeout` de 2 s e `lock_timeout` de 1 s. O trabalho roda numa thread e a raspagem espera no máximo 5 s, porque o prazo do servidor não corta um socket que emudece depois de aberto.
- **O que falta fica ausente.** Passado o prazo, a raspagem sai com o que já leu, e o que faltou fica ausente, nunca em zero. Com a leitura anterior ainda presa, a raspagem seguinte nem tenta (uma thread presa por vez) e sai só com `pytstop_saga_coletor_disponivel` em 0. Uma leitura presa num socket que o banco deixou aberto e mudo não tem prazo próprio: ela segura a vez, e os gauges seguem ausentes até o socket cair: o banco reinicia, a conexão é resetada ou o TCP desiste de um buraco de rede (em geral em minutos, porque a engine das métricas não fixa `keepalives` nem `tcp_user_timeout`). Aí a thread sai e a raspagem seguinte lê de novo (um teste de integração cobre a volta). O coletor não derruba esse socket por conta própria, e reiniciar o pod resolve na hora.
- **A OS por status tem cache.** A leitura das OS por status (a contagem e a permanência) vale 30 s no coletor: a raspagem de 15 s de cada réplica não repete a consulta.
- **O custo é pequeno.** As consultas de `sagas`, `outbox` e `historico_status_ordem` leem por índice (a migração 005 acrescenta `(status, criado_em)` na outbox e `(ocorrido_em)` no histórico, e um teste confere o plano das duas); a contagem de `ordens_de_servico` por status lê a tabela inteira, que no volume de uma oficina é pequena. Cada réplica da API repete os valores; os painéis agregam com `max`.

**Logs do coletor.** Quando uma leitura falha, o coletor registra um WARNING sem dado pessoal. `api gauges unavailable` leva `motivo=` (`prazo_esgotado`, `raspagem_anterior_presa` ou o tipo da exceção) e diz que a raspagem inteira saiu sem os gauges; `api gauge unavailable` leva `gauge=` (`saga`, `outbox` ou `ordens`) e `erro=` (o tipo da exceção) e diz que só aquela consulta falhou. No Loki, `{namespace="pytstop-os"} |= "api gauge"` pega os dois; o nome antigo, `saga gauges unavailable`, não existe mais.

**`GET /api/v1/ordens-de-servico/metricas`** (admin) devolve os mesmos números do `pytstop_os_por_status` e do `pytstop_os_tempo_medio_status_segundos`, na hora, sem o cache do coletor (`$TOKEN` é o JWT do admin, do login do [compose](como-rodar.md#como-rodar-local)):

```bash
curl -s localhost:8000/api/v1/ordens-de-servico/metricas -H "Authorization: Bearer $TOKEN" | jq
```

```json
{
  "total": 3,
  "por_status": {"recebida": 0, "em_diagnostico": 1, "aguardando_aprovacao": 1, "aguardando_pagamento": 0, "aguardando_execucao": 0, "em_execucao": 0, "finalizada": 0, "entregue": 0, "cancelada": 1},
  "tempo_medio_por_status_segundos": {"recebida": 150.0, "em_diagnostico": 41550.0, "aguardando_aprovacao": 600.0}
}
```

A permanência de um status é o tempo da linha do histórico em que a OS entrou nele até a linha seguinte da mesma OS, nas entradas dos últimos 30 dias; a OS que ainda está no status, os status finais e o status de outra versão não entram, e uma permanência negativa por desvio de relógio entre réplicas conta como zero. Os painéis "Volume diário de OS" (a partir do `increase()` de `pytstop_os_criadas_total`, em barras por dia UTC) e "Tempo médio por status" (RF-027 da fase 3, o pedido da banca) vêm destas séries; o painel de execução por serviço (RF-008) é da Execução.

## Spans

O relay, o consumidor e o `prazos` montam o provider (`criar_provedor`) sempre; a API só o monta com `OTEL_ENABLED=true`, e sem a flag ela não abre span (as métricas seguem, e o `trace_id` dos logs vem de um span aberto). O notificador não abre span. O recurso leva `service.name` (de `OTEL_SERVICE_NAME`), `service.version` e `pytstop.processo`, e `OTEL_ENABLED=true` liga a exportação OTLP para o Jaeger e a instrumentação do FastAPI e do SQLAlchemy.

| Processo | Spans |
|---|---|
| API | um SERVER por request, `GET /api/v1/ordens-de-servico/{ordem_id}`, com um CLIENT por comando SQL como filho (o SQLAlchemy cobre o psycopg2; o driver não é instrumentado à parte, para não duplicar o span); `saga cancelamento` e `saga retomada` (INTERNAL) |
| relay | `publish <tipo>` (PRODUCER, filho do contexto gravado na linha da outbox), com o comando SQL que marca a linha como filho |
| consumidor | `process <tipo>` (CONSUMER, filho do contexto do header), com os comandos SQL do tratamento da mensagem |
| `prazos` | `saga prazo` (INTERNAL), só quando uma saga é tratada, com os comandos SQL dela |
| notificador | nenhum (a linha de e-mail não guarda o contexto de trace) |

O pika não tem instrumentador: os spans de publicação e de consumo são abertos à mão (`mensageria/telemetria.py`), porque o contexto atravessa a outbox e a cópia de retry. O OS não chama outro serviço por HTTP, então não há span de cliente HTTP: o trace da saga atravessa os três serviços pelas mensagens, e o E2E do platform confere os três `service.name`. O contexto de entrada é só o W3C (`traceparent` e `tracestate`): o `baggage` do chamador não entra no processo nem sai dele, e o `tracestate` de mais de 512 caracteres é descartado também na entrada HTTP.

**Ocioso não gera trace.** O amostrador descarta o span de cliente sem pai: o poll do relay, a raspagem do `/metrics`, a limpeza horária, o ciclo do `prazos` sem saga vencida (ou pausado, sem consumidor ou com resposta na fila) e o laço do consumidor só fazem consultas sem span em volta. Um span de cliente dentro de uma request ou de uma mensagem segue a decisão do pai.

## O que não é exportado, e por quê

- **`/metrics` e as sondas** (`/api/v1/saude`, `/api/v1/saude/pronto`): o Prometheus e o kubelet as chamam a cada poucos segundos e não contam história nenhuma. A exclusão casa o caminho inteiro (com ou sem o prefixo do `--root-path`), e um `Host` com barra não tira outra rota do trace.
- **Os spans `receive` e `send` do ASGI**: duplicariam cada request.
- **O caminho cru, a query, o user-agent, o IP e a porta do par e o cabeçalho `Host`**: identificam o titular (o caminho traz o id da OS, do cliente e do veículo, e a query é de quem chama). O span leva o template da rota (`/api/v1/ordens-de-servico/{ordem_id}`) e `REDACTED` no lugar do resto; a porta do par vira o inteiro `0`, o tipo em que o instrumentador a grava.
- **O texto das exceções, o stack trace e a descrição do status de erro**: o erro do driver traz o `DETAIL` do PostgreSQL com o valor da linha (placa, texto livre), e o dos instrumentadores fica por fora da limpeza de PII dos logs. Um processador do provider, o primeiro e dono do exportador, tira esse texto de todo span; o motivo da falha vai no atributo `pytstop.falha`, só com o tipo da exceção ou um código fixo.
- **Os parâmetros dos comandos SQL**: o instrumentador do SQLAlchemy grava só o texto do comando, com os marcadores, e nunca os parâmetros. O `hide_parameters` do engine cuida da mensagem de erro e do log do engine.
- **Dinheiro, ids e rótulos abertos nas métricas**: os rótulos são enumerações fechadas (`status`, `etapa`, `motivo`, `comando`, `tipo`, `resultado`, `componente`) ou o template da rota; nunca `ordem_id`, placa ou documento.

O Jaeger do platform guarda até 250 traces em memória (`MEMORY_MAX_TRACES=250`), com retenção por contagem e sem prazo ([Retenção e prazos](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md#retenção-e-prazos)): o trace mais antigo sai quando entra o 251º, reiniciar o pod apaga tudo, e o que fica como evidência são os prints.

## Logs

Cada processo escreve um JSON por linha no stdout, que o Promtail do platform leva ao Loki.

- **Campos.** `event` (o nome do evento), `level`, `timestamp` (ISO 8601, em UTC), `logger`, `git_sha` e `git_date` (a versão da imagem), `trace_id` e `span_id` com um span aberto, e os campos de cada evento: o `correlation_id` (o id da OS) nos de saga, mensageria e prazos. Cada requisição da API deixa o evento `http_request`, com `method`, `path`, `status`, `duration_ms` e o `request_id`.
- **Idioma.** O `event` vem em inglês, menos os nomes herdados da fase 3: as auditorias LGPD (`dados_pessoais_exportados_via_admin`, `dados_pessoais_excluidos_via_admin`, `consentimento_registrado_via_admin` e `consentimento_revogado_via_admin`) e os do tratamento de erros (`erro_interno`, `dominio_excecao_tratada`, `validacao_schema_tratada_422` e `value_error_tratado_422`). As duas mensagens de `compartilhado/infraestrutura/encryption.py` (chave de criptografia não configurada e falha ao decifrar um token Fernet) usam o `logging` da stdlib e também saem em português.
- **Sem dado pessoal.** O mascaramento de `compartilhado/infraestrutura/logging.py` roda em todo evento, inclusive no log das bibliotecas e no traceback: CPF, CNPJ, e-mail e telefone viram máscara, e o valor de uma chave sensível (senha, token, contato, telefone) sai trocado inteiro. O texto livre, a placa e o envelope das mensagens não vão para o log, e do cliente AMQP só sai o nível ERROR.
- **Auditoria.** O pedido de cancelamento e a retomada da compensação deixam uma linha com o `ator` (o `sub` do JWT) e o resultado, e a leitura dos dados pessoais, com o `ator` e a rota (`via`).
- **Busca.** No Grafana do kind, a consulta `{namespace="pytstop-os"} | json | correlation_id="<id da OS>"` traz a OS nos cinco processos, e o `trace_id` de cada linha abre o trace no Jaeger.

## Como ver

- **Sem cluster.** `make compose-up` e `curl -s localhost:8000/metrics | grep '^pytstop_'` (a API); o relay, o consumidor, o `prazos` e o notificador servem o deles na 9100 dentro do container (`docker compose exec relay python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"`). O compose deixa `OTEL_ENABLED=false`: a API não abre span, e o relay, o consumidor e o `prazos` abrem os deles e levam o `trace_id` aos logs, mas os spans não saem do processo. Para ver um trace, acrescente um serviço `jaeger` ao compose (por exemplo num `docker-compose.override.yml`: `services: {jaeger: {image: "jaegertracing/all-in-one:1.76.0", ports: ["127.0.0.1:16686:16686"]}}`) e suba com `OTEL_ENABLED=true make compose-up`: o endpoint padrão dos processos é `http://jaeger:4317`, e a tela fica em `http://localhost:16686` (serviço `pytstop-os-service`; o atributo `pytstop.processo` do recurso diz de qual processo veio cada span).
- **No kind.** O ConfigMap do serviço liga `OTEL_ENABLED=true`, aponta para o Jaeger do platform e fixa `OTEL_SERVICE_NAME=os-service`: no Jaeger do kind o serviço aparece como `os-service` (no compose, sem a variável, é `pytstop-os-service`). Depois de `make -C ../postech-sw-arch-p4-platform kind-up deploy` e do `make kind-deploy` deste serviço (e dos outros), `make -C ../postech-sw-arch-p4-platform port-forward` abre o Jaeger em `http://localhost:16686` (busque pelo atributo `messaging.message.conversation_id`, o id da OS), o Grafana em `http://localhost:3000` e o Prometheus em `http://localhost:9090`. O `make -C ../postech-sw-arch-p4-platform e2e` roda os cenários da saga, o caminho feliz entre eles, e deixa os traces no Jaeger; ele pede o overlay `kind-ci` (`deploy OVERLAY=kind-ci`), que não tem o Grafana. Os painéis e os alertas estão descritos no [README de observabilidade do platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/observabilidade/README.md).

## Encerramento com o exportador fora

A API encerra o provider no fim do `lifespan` (`provedor.shutdown()` descarrega o lote de spans; o `atexit` do SDK não roda quando o uvicorn não é o PID 1) e o relay, o consumidor e o `prazos`, na saída do processo. Com o Jaeger fora do ar o SDK ainda tenta enviar a fila inteira (até 2.048 spans, em lotes de 512), e cada lote espera o prazo do exportador. Medido no container da imagem (uvicorn como PID 1), com um listener que aceita a conexão e não responde e a fila cheia (6.000 requisições em rajada, de dentro do container, a uma rota que não existe):

| `OTEL_EXPORTER_OTLP_TIMEOUT` | `OTEL_BSP_EXPORT_TIMEOUT` | `docker stop` da API |
|---|---|---|
| 10 s (o padrão do SDK) | 30 s (o padrão do SDK) | 30,5 s: mais que os 30 s do `terminationGracePeriodSeconds`, e o kubelet encerraria o processo à força |
| 2 s (compose e ConfigMap) | 2 s (compose e ConfigMap) | 8,1 s (quatro lotes de 2 s) |
| 2 s | 30 s (o padrão do SDK) | 8,0 s |

Com a fila vazia o encerramento leva 0,4 s nos dois casos. O compose e o ConfigMap fixam `OTEL_EXPORTER_OTLP_TIMEOUT=2` e `OTEL_BSP_EXPORT_TIMEOUT=2000`. O segundo não muda nada no SDK 1.45: o processador de lote ainda não aplica esse prazo, e a medida com o padrão dele (30 s) é a de 8,0 s. Ele fica fixado para valer quando o SDK passar a usá-lo.

## Observabilidade da saga

- **Trace.**
  - O span da requisição de abertura é a raiz do trace da saga: a outbox guarda o contexto, o relay publica como filho, e o `process <tipo>` do consumidor ganha `pytstop.saga.etapa`, `pytstop.saga.etapa_nova` e `pytstop.saga.desfecho` (`processada`, `ignorada`, `adiantada` ou `recusada`, com o motivo no status de erro), mais `pytstop.saga.falha` quando o evento leva a saga a `falha_na_compensacao`.
  - A saga guarda o `traceparent` da última transição. O cancelamento chega com trace próprio (a requisição do atendente): o caso de uso abre o span `saga cancelamento` como filho desse contexto, com span link para a requisição, e a primeira compensação e o novo `traceparent` da saga nascem nele, de modo que a compensação segue no trace da saga.
  - O `prazos` e a retomada fazem o mesmo: abrem `saga prazo` (um por saga tratada, o ciclo ocioso não abre span) e `saga retomada` como filhos do contexto da saga, e o reenvio sai no trace dela.
  - Os spans `saga cancelamento`, `saga prazo` e `saga retomada` ficam com status de erro e `pytstop.falha` (o tipo da exceção, sem a mensagem) só numa falha inesperada; as recusas de negócio (404, 409, 422) não os marcam.
- **Métricas** ([ADR-043](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/arquitetura/adr/fase4/043-observabilidade-distribuida.md)).
  - Contadores e histograma, contados só depois do commit da transação raiz (rollback, conflito de versão, commit que falha, savepoint liberado e savepoint desfeito não contam, e a retry não conta duas vezes): `pytstop_saga_iniciadas_total` (API), `pytstop_saga_finalizadas_total{resultado}` (`concluida` ou `compensada`), `pytstop_saga_compensacoes_total{motivo}` (quem inicia a compensação: o consumidor nas falhas de negócio e a API no cancelamento; os oito códigos) e `pytstop_saga_etapa_duracao_segundos{etapa}` (quem tira a saga da etapa). O `prazos` soma `pytstop_saga_reenvios_total{comando}` e `pytstop_saga_prazos_esgotados_total{comando}` (os nove comandos com prazo) e inicia a compensação `prazo_tecnico` na contagem de compensações.
  - Gauges `pytstop_saga_ativas{etapa}`, `pytstop_saga_etapa_mais_antiga_segundos{etapa}` e `pytstop_saga_prazo_vencido_segundos`: o coletor da API os calcula em duas consultas por raspagem, na conexão própria da raspagem, fora do pool das requisições, com 2 s de prazo para cada consulta e 1 s para esperar um lock ([Observabilidade](#observabilidade) tem o prazo total da raspagem e o que mais o coletor lê). O de prazo vencido é o maior `agora - (entregue_em + SAGA_PRAZO_RESPOSTA_SEGUNDOS)` da linha da outbox do envio mais recente de cada saga com prazo, só entre as entregues, e 0 sem atraso: comando `pendente` ou `dead` na outbox não conta, nem a saga parada em `falha_na_compensacao`. Como o coletor só lê o banco, ele acusa o prazo vencido também com o `prazos` fora do ar ou em pausa (o alerta "Saga parada" usa `max(pytstop_saga_prazo_vencido_segundos) > 60`).
  - Se a leitura das sagas falha, os três gauges ficam ausentes (nunca zero), `pytstop_saga_coletor_falhas_total` conta a falha e `pytstop_saga_coletor_disponivel` vai a 0, para o alerta "Saga parada" não ficar mudo com o coletor quebrado. As séries de rótulo fechado nascem em zero em cada processo.
- **Logs** JSON em inglês, com `correlation_id`, `etapa` e `tipo` (o `comando` nos do `prazos`).
  - `saga started`, `saga transition` (com o `motivo` da compensação; em aviso e com a `falha` quando a saga para na compensação), `saga event ignored` (com a classificação), `saga event ahead`, `late payment refunded` (o estorno automático) e `saga clock behind last record` (com a defasagem em segundos).
  - O pedido de cancelamento audita em `order cancellation requested` e `order cancellation refused`, com o `ator` e o `resultado`, sem texto livre, placa nem links.
  - O `prazos` loga `saga command resent` (com o número do `reenvio`), `saga deadline exhausted` (em aviso e com a `falha` quando a saga para), `saga deadline refused`, `saga deadlines paused` (com o `motivo`), `clock skew too large: saga deadlines skipped` (em erro, com o desvio) e `deadline cycle done` (só com trabalho). Nunca os `dados` do comando.
  - A retomada audita em `saga resumed` e `saga resume refused`, com o `ator` e o `resultado`.
