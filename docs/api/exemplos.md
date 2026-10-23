# Exemplos da API

Este documento detalha a seção [API](../../README.md#api) do README.

Os `curl` das rotas do OpenAPI, um por rota e agrupados por fluxo, contra a API do compose (`make compose-up`, porta 8000). Pela borda do kind, troque `localhost:8000` por `https://localhost/os` e acrescente `-k` (o certificado do Kong é autoassinado). O OpenAPI e a coleção Postman, com um exemplo de corpo e de resposta por rota, estão em [`docs/api/`](./) (na coleção, o login e o refresh guardam o token, e o cadastro do cliente, o do veículo e a abertura da OS guardam os ids nas variáveis), e os fluxos por trás de cada rota, em [`docs/arquitetura.md`](../arquitetura.md).

- **Variáveis.** Cada bloco usa as que os anteriores preenchem: `TOKEN` e `REFRESH` saem do login, e `CLIENTE_ID`, `VEICULO_ID` e `ORDEM_ID`, das respostas do cadastro e da abertura.
- **O que o compose não tem.** O compose não sobe o Billing nem a Execução, então a OS aberta aqui fica em `recebida`, com a saga esperando o diagnóstico. As rotas que pedem outro estado respondem o erro da regra, que o texto de cada bloco diz.
- **Teste.** `tests/unitarios/test_documentacao.py` reprova este documento quando uma rota do OpenAPI fica sem `curl`, quando um corpo sai do schema da rota (inclusive o formato, como o de um uuid) e quando falta o `Authorization` de uma rota protegida ou o `Content-Type` de um corpo.

## Saúde

Liveness e readiness (503 com o banco fora), sem token:

```bash
curl -s localhost:8000/api/v1/saude
curl -s localhost:8000/api/v1/saude/pronto
```

## Autenticação

Login do admin de demonstração do compose. A resposta traz o access token, de 15 min, e o refresh, de 7 dias:

```bash
RESPOSTA=$(curl -s -X POST localhost:8000/api/v1/autenticacao/login \
  -H 'Content-Type: application/json' \
  -d '{"email": "admin@pytstop.dev", "senha": "admin-demo-os-2026"}')
TOKEN=$(echo "$RESPOSTA" | jq -r .access_token)
REFRESH=$(echo "$RESPOSTA" | jq -r .refresh_token)
```

A troca do refresh por um par novo; o refresh usado deixa de valer:

```bash
RESPOSTA=$(curl -s -X POST localhost:8000/api/v1/autenticacao/refresh \
  -H 'Content-Type: application/json' \
  -d '{"refresh_token": "'"$REFRESH"'"}')
TOKEN=$(echo "$RESPOSTA" | jq -r .access_token)
REFRESH=$(echo "$RESPOSTA" | jq -r .refresh_token)
```

As chaves públicas com que Billing e Execução validam o token, sem token e com cache de 10 min:

```bash
curl -s localhost:8000/.well-known/jwks.json | jq '.keys[].kid'
```

O cadastro de um usuário interno (admin). O `papel` é `admin`, `atendente` ou `mecanico`, e a senha tem de 12 a 128 caracteres:

```bash
curl -s -X POST localhost:8000/api/v1/autenticacao/registrar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"email": "atendente@pytstop.dev", "senha": "atendente-demo-2026", "papel": "atendente"}' | jq
```

O logout fica no fim desta seção, porque revoga o `TOKEN`.

## Cadastro do cliente com consentimento (LGPD)

O cliente com CPF (o dígito verificador é conferido, e o documento repetido responde 409), o consentimento de tratamento dos dados e um veículo:

```bash
CLIENTE_ID=$(curl -s -X POST localhost:8000/api/v1/clientes \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"nome": "Maria Souza", "documento": "529.982.247-25", "tipo_documento": "cpf", "contato": "maria.souza@example.com"}' | jq -r .id)
curl -s -X POST localhost:8000/api/v1/clientes/$CLIENTE_ID/consentimento \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tipo": "tratamento_dados"}' | jq
VEICULO_ID=$(curl -s -X POST localhost:8000/api/v1/clientes/$CLIENTE_ID/veiculos \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"placa": "BRA2E19", "marca": "Fiat", "modelo": "Argo", "ano": 2022}' | jq -r .id)
```

A listagem paginada, a consulta, a atualização do nome e do contato e os veículos do cliente:

```bash
curl -s "localhost:8000/api/v1/clientes?offset=0&limit=20" -H "Authorization: Bearer $TOKEN" | jq
curl -s localhost:8000/api/v1/clientes/$CLIENTE_ID -H "Authorization: Bearer $TOKEN" | jq
curl -s -X PUT localhost:8000/api/v1/clientes/$CLIENTE_ID \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"nome": "Maria Souza Lima", "contato": "maria.lima@example.com"}' | jq
curl -s localhost:8000/api/v1/clientes/$CLIENTE_ID/veiculos -H "Authorization: Bearer $TOKEN" | jq
```

Os dados pessoais do titular, decifrados (acesso e portabilidade). Cada leitura deixa uma linha de auditoria no log, sem o dado:

```bash
curl -s localhost:8000/api/v1/clientes/$CLIENTE_ID/dados-pessoais -H "Authorization: Bearer $TOKEN" | jq
curl -s localhost:8000/api/v1/clientes/$CLIENTE_ID/dados-pessoais/exportar -H "Authorization: Bearer $TOKEN" | jq
```

## Ordem de serviço

A abertura da OS, que inicia a saga e manda o `SolicitarDiagnostico` à Execução, a consulta com a etapa da saga, o histórico e a fila por prioridade de status:

```bash
ORDEM_ID=$(curl -s -X POST localhost:8000/api/v1/ordens-de-servico \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"cliente_id": "'"$CLIENTE_ID"'", "veiculo_id": "'"$VEICULO_ID"'", "descricao_problema": "Barulho no freio dianteiro ao frear em descida"}' | jq -r .id)
curl -s localhost:8000/api/v1/ordens-de-servico/$ORDEM_ID -H "Authorization: Bearer $TOKEN" | jq '{status, situacao, etapa}'
curl -s localhost:8000/api/v1/ordens-de-servico/$ORDEM_ID/historico -H "Authorization: Bearer $TOKEN" | jq
curl -s "localhost:8000/api/v1/ordens-de-servico?offset=0&limit=20&incluir_encerradas=false" -H "Authorization: Bearer $TOKEN" | jq
```

O total, as OS por status e a permanência média em cada status (admin):

```bash
curl -s localhost:8000/api/v1/ordens-de-servico/metricas -H "Authorization: Bearer $TOKEN" | jq
```

A entrega ao cliente (T10) só sai de `finalizada`; a OS do compose, sem a Execução, responde 409 `TRANSICAO_STATUS_INVALIDA`:

```bash
curl -s -X POST localhost:8000/api/v1/ordens-de-servico/$ORDEM_ID/entrega -H "Authorization: Bearer $TOKEN" | jq
```

O pedido de cancelamento responde 202 com a etapa `compensando` antes do pivot (as respostas de cada etapa estão em [Compensações e cancelamento](../saga.md#compensações-e-cancelamento)):

```bash
curl -s -X POST localhost:8000/api/v1/ordens-de-servico/$ORDEM_ID/cancelamento \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"motivo": "Cliente vendeu o veículo"}' | jq '{status, etapa, motivo_cancelamento}'
```

## Saga (operação, admin)

O estado da saga e a retomada da compensação parada; fora de `falha_na_compensacao`, a retomada responde 409:

```bash
curl -s localhost:8000/api/v1/sagas/$ORDEM_ID -H "Authorization: Bearer $TOKEN" | jq
curl -s -X POST localhost:8000/api/v1/sagas/$ORDEM_ID/compensacao -H "Authorization: Bearer $TOKEN" | jq
```

## Acompanhamento público

Sem token, com a placa e o documento no corpo. Formato ou dígito verificador inválido e OS não encontrada dão o mesmo 404; placa fora de 7 a 8 caracteres ou documento fora de 11 a 18 dão o 422 da validação de schema:

```bash
curl -s -X POST localhost:8000/api/v1/publico/acompanhamento \
  -H 'Content-Type: application/json' \
  -d '{"placa": "BRA2E19", "documento": "529.982.247-25"}' | jq
```

## Revogação e eliminação (LGPD)

A revogação do consentimento, a remoção do veículo, a desativação do cliente e a eliminação dos dados pessoais (admin) respondem 204, sem corpo. A remoção do veículo responde 409 se ele tem OS, e a desativação e a eliminação, enquanto o cliente tem OS ativa: no roteiro acima, só a revogação passa, porque a OS segue `recebida` com a compensação esperando a Execução.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "localhost:8000/api/v1/clientes/$CLIENTE_ID/consentimento?tipo=tratamento_dados" -H "Authorization: Bearer $TOKEN"
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE localhost:8000/api/v1/clientes/$CLIENTE_ID/veiculos/$VEICULO_ID -H "Authorization: Bearer $TOKEN"
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE localhost:8000/api/v1/clientes/$CLIENTE_ID -H "Authorization: Bearer $TOKEN"
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE localhost:8000/api/v1/clientes/$CLIENTE_ID/dados-pessoais -H "Authorization: Bearer $TOKEN"
```

## Logout

Revoga o access do cabeçalho e o refresh do corpo:

```bash
curl -s -X POST localhost:8000/api/v1/autenticacao/logout \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"refresh_token": "'"$REFRESH"'"}' | jq
```
