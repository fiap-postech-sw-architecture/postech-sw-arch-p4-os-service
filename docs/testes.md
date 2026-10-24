# Testes e qualidade

Este documento detalha a seção [Como testar](../README.md#como-testar) do README.

## Qualidade

- `make check`: `uv.lock` em dia, ruff (lint e formato), import-linter, mypy strict, bandit, pytest com o gate de 95% e o `diff-cover` (90% no código que a branch muda em relação à `origin/main`).
- `make manifests`: kubeconform e trivy config dos três overlays de `k8s/`, mais a conferência de que todo `PASSWORD` do `papeis.sql` vem de uma variável do psql.
- `make lint-workflows`: actionlint, com o shellcheck da imagem nos scripts dos `run:`, em todos os workflows (a imagem é a `rhysd/actionlint:1.7.12`, fixada por digest no Makefile, na mesma versão do `lint-scripts` do platform). Entra no `make check` e no job `lint`.
- `make audit`: pip-audit das dependências de runtime, com os extras da imagem.
- `make mutacao`: mutantes escritos à mão nos módulos críticos, aplicados um por vez numa cópia do código (seção [Mutação](#mutação)). Fica fora do `make check` e do CI.
- `make docs-api`: gera de novo o OpenAPI e a coleção Postman de [`docs/api/`](api/) a partir do app, sem servidor nem banco; o `make test` reprova os arquivos atrás do código.
- Erros no OpenAPI: o `make test` reprova o teste de API que recebe de uma rota de `src` um status que o OpenAPI não declara. A fixture automática `respostas_vistas` (`tests/respostas_da_api.py`) confere, no fim de cada teste, o que as rotas responderam contra o `responses` do app real; o status que nenhum teste provoca não é conferido. Os erros declarados usam o envelope de erro (`src/compartilhado/interfaces/respostas.py`). Fica de fora o 422 de domínio (`VALOR_INVALIDO`, no envelope): só o cancelamento o descreve, e nas demais rotas o 422 do OpenAPI é o padrão do FastAPI (`HTTPValidationError`), dívida aceita na [auditoria da fase 4](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/main/docs/entrega/fase4/auditoria-e-divida-tecnica.md). O 500 não se declara, e o 429 só vale nas rotas com `@limiter.limit` próprio, porque o limite padrão não alcança as rotas dos routers. `tests/integracao/test_erros_da_api.py` provoca, contra o app real, os erros de registrar, dos limites por IP e das rotas de cliente.
- `make cobertura-readme RUN=<id do run>`: reescreve a seção [Cobertura](../README.md#cobertura) com o `coverage.xml` de um run do CI (pede o `gh` autenticado).
- `make smoke`: a imagem pelo entrypoint real. Confere a readiness, o login do admin semeado validado pelo JWKS (e a assinatura adulterada recusada), o log de boot em JSON, o `http_request` estruturado e nenhum access log do uvicorn, a imagem de produção (usuário 1001, `ENVIRONMENT=production`, sem header `server`) e o relay, o consumidor, o `prazos` e o notificador prontos (`/metrics` respondendo, `outbox_pendentes` numérico, as séries de reenvio da saga em zero no `prazos`, consumidor inscrito em `os.eventos` com prefetch 1 no `list_consumers`), as séries de rótulo fechado da API, do relay, do consumidor e do `prazos` em zero logo depois do boot (e o último ciclo do `prazos` gravado), a OS aberta contada no `pytstop_os_criadas_total` e um e-mail entregue ao Mailpit pelo notificador (uma OS aberta pela API, um pedido gravado em `notificacoes` e a caixa lida pela API do Mailpit, sem endereço nem nome no log dele); depois, `down -v`.

```bash
make check
make lint-workflows
make manifests
make audit
make smoke
```

`make test` (ou `uv run pytest`) roda os testes unitários, os de contrato, o BDD de componente da saga e os de integração contra um PostgreSQL e um RabbitMQ 4.3.6 efêmeros (testcontainers, Docker necessário), com gate de cobertura de 95% (`.coveragerc`). Os testes dos manifestos leem o render do `kubectl kustomize`, então o `kubectl` precisa estar no PATH: sem ele a suíte encerra de uma vez, com essa mensagem, em vez de falhar teste a teste. O teste de checksum dos contratos baixa o platform do GitHub e o que confere o SHA das actions contra as tags roda `git ls-remote`: os dois precisam de rede, e offline se roda com `-m "not rede"` (a cobertura do gate continua valendo). O schema dos testes de integração é criado pela própria migração Alembic (as migrações só acrescentam, e o rollback de uma imagem não roda `downgrade`, que apaga dados: o da `003` apaga a tabela `sagas` e a coluna `ator` do histórico; a `003` desiste em 5 s de um lock que uma leitura longa segure), e o broker de teste sobe com a topologia de `contratos/rabbitmq/`, só com o TTL das filas de retry reduzido a 100 ms para o ciclo inteiro de tentativas caber num teste.

## Cobertura

As tabelas da seção [Cobertura](../README.md#cobertura) do README não são digitadas: `make cobertura-readme RUN=<id do run>` baixa o `coverage.xml` do artefato `relatorios-de-teste` de um run do CI e reescreve, com `scripts/cobertura_resumo.py`, o trecho entre os dois marcadores de comentário dela (`cobertura:inicio` e `cobertura:fim`), com o link do run. `tests/unitarios/test_documentacao.py` reprova o trecho editado à mão. O CI publica a mesma medida, por pacote, no summary do job `test` de cada run ([Actions → CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service/actions/workflows/ci.yml)), e `uv run python scripts/cobertura_resumo.py coverage.xml` a mostra depois de um `make test` local.

- **Gate.** 95% de linhas e ramos somados (`fail_under` no `.coveragerc`), e 90% no código que a branch muda (`make diff-cover`). A suíte tem testes unitários, de contrato, BDD de componente e de integração com PostgreSQL e RabbitMQ reais.
- **Oscilação.** Com o mesmo código, o número oscila entre 99,63% e 99,61%, no macOS e no CI: o ramo em que a segunda verificação do double-checked locking de `encryption.py` (linha 45) acha a instância já criada só roda quando as 8 threads do teste de concorrência se cruzam no lock, o que às vezes não acontece. Com 99,61%, são 918 ramos cobertos em vez de 919.
- **Fora da medição.** Os `__init__.py` e os módulos só com `Protocol` (portas dos repositórios, `ports.py` da aplicação e a unidade de trabalho), pelo `omit` do `.coveragerc`.
- **Linhas sem teste.** São 16: o `if __name__ == "__main__"` dos cinco processos, os três corpos `pass` do `Protocol` `Documento` e oito linhas de quatro trechos que o relatório do `make test` lista.

O `make diff-cover` lê o `coverage.xml` do `make test` e exige que ele exista e seja mais novo que o código de `src/`: sem o XML, ou com ele velho, recusa e manda rodar o `make test` antes. Com o XML velho, o diff-cover passaria sem medir nada. O `make check` já roda os dois na ordem certa.

## Mutação

Cobertura diz que a linha rodou; mutação diz se a suíte percebe quando ela está errada (ADR-041). Não é gate de CI e não tem pontuação mínima.

O `mutmut` não roda num pacote chamado `src`, que é o caso deste serviço (e do Billing, onde se mediu): a versão 3.8.0 espera o pacote em `src/<pacote>` e tira o prefixo `src.` do nome do módulo, e a primeira função mutada que os testes chamam reprova com

```text
AssertionError: Failed trampoline hit. Module name starts with `src.`, which is invalid
```

(`mutmut/stats.py`, `record_trampoline_hit`). Renomear o pacote ou remendar a ferramenta mudaria o que se mede, então a alternativa são mutantes escritos à mão: [`scripts/mutantes.toml`](../scripts/mutantes.toml) lista 60 defeitos plausíveis nos módulos críticos, e [`scripts/mutacao_manual.py`](../scripts/mutacao_manual.py) (o mesmo script do Billing) aplica um por vez numa cópia do código (`mutants/`, fora do git) e roda só os testes que deveriam pegá-lo.

**Resultado** em 18/10/2026: 60 mortos de 60 medidos (100%), sem equivalentes, em cerca de 6 min: de 5,5 a 6,3 min em seis medições, conforme a carga da máquina.

| Área | Mutantes | Mortos | Equivalentes |
|---|---:|---:|---:|
| Status da OS (máquina e agregado) | 11 | 11 | 0 |
| Classificação do evento na saga | 7 | 7 | 0 |
| Transições da saga | 5 | 5 | 0 |
| Prazos da saga | 4 | 4 | 0 |
| Compensação da saga | 4 | 4 | 0 |
| Orquestrador da saga | 3 | 3 | 0 |
| Consumidor | 5 | 5 | 0 |
| Idempotência | 2 | 2 | 0 |
| Relay | 6 | 6 | 0 |
| JWT e acesso | 9 | 9 | 0 |
| CPF | 4 | 4 | 0 |
| **total** | **60** | **60** | **0** |

- **Sobrevivente que virou teste:** o corpo no teto exato de 64 KiB tratado como grande demais (Q05). O único caso era um corpo de cerca de 70 KB, longe do limite; `test_corpo_ate_64_kib_passa_do_limite_de_tamanho` prende a constante e os dois lados dela, sem broker.
- **O que o percentual não é:** a lista é curada, então ele diz que a suíte pega esses 60 defeitos, e não é comparável ao de uma ferramenta que gera os mutantes sozinha.

Para refazer:

```bash
make mutacao
make mutacao MUTANTES="Q05 R03"
```

Como o alvo se comporta:

- Para de iniciar mutantes depois de `MUTACAO_PRAZO` segundos (900); os que sobram saem `nao rodado`.
- Antes do primeiro mutante de cada grupo de testes, roda o grupo na cópia sem mutante. Se o grupo já reprova ou estoura o tempo, os mutantes dele saem `erro`: um teste que já falha mataria qualquer um.
- Com `erro` ou `nao rodado`, o percentual vira `n/d`. A tabela vai para `reports/mutacao.md`.
- Os testes de integração sobem um PostgreSQL por testcontainers a cada grupo e a cada mutante, e os do consumidor e do relay também um RabbitMQ: o Docker precisa estar no ar.
- O script lê o arquivo original de `src/` a cada mutante: não edite `src/` com a bateria rodando.
- Um mutante novo entra com o trecho exato (`de`, uma ocorrência no arquivo), e `tests/unitarios/test_mutacao_manual.py` reprova se a lista envelhecer.

Códigos de saída:

- `0`: nenhum mutante saiu `erro`. Sobrevivente e `nao rodado` não reprovam, porque não há gate (ADR-041). É o código do script; o `make mutacao` sai com 0 ou com 2 (o `make` troca o 1 do script pelo 2 dele).
- `1`: algum mutante saiu `erro`, uma medida inválida, como o Docker fora ou um arquivo que os testes leem e que a cópia não levou.
- `2`: um id de `MUTANTES` não existe (a mensagem lista os válidos) ou o `.toml` não bate com o código.

## Testes da saga

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
