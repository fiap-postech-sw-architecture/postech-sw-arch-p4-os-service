# language: pt
Funcionalidade: Saga de atendimento orquestrada pelo OS Service
  O OS Service abre a ordem de serviço e conduz a saga pelos comandos e
  eventos da RFC-004 (seção 4.1). Billing e Execução respondem por um
  barramento em memória, com mensagens validadas pelos schemas de contratos/:
  cada evento publicado é entregue ao OS na hora, pelo mesmo caminho do
  consumidor (registro em mensagens_processadas, handler e commit), e os
  comandos que ele gera saem da outbox para o participante. Cada evento leva
  no causation_id o id do comando que responde, ou do que abriu o fluxo
  (RFC-004 seção 5.2). O evento adiantado volta para a fila e é entregue de
  novo depois do seguinte; o recusado vai para a DLQ com o motivo. Os passos
  ficam em tests/integracao/test_saga_atendimento.py, ao lado do PostgreSQL de
  teste.

  Contexto:
    Dado um cliente com um veículo cadastrado
    E uma ordem de serviço aberta pelo atendente

  Cenário: A abertura inicia a saga e pede o diagnóstico
    Então a Execução recebe o comando "SolicitarDiagnostico"
    E a saga está na etapa "aguardando_diagnostico" com a OS "recebida"

  Esquema do Cenário: Cada evento do caminho feliz leva a saga à etapa seguinte
    Dado os eventos do caminho feliz anteriores a "<evento>"
    Quando <participante> publica "<evento>"
    Então a saga está na etapa "<etapa>" com a OS "<status>"
    E o comando enviado em seguida é "<comando>"

    Exemplos:
      | evento               | participante | etapa                  | status               | comando            |
      | DiagnosticoIniciado  | a Execução   | aguardando_diagnostico | em_diagnostico       | nenhum             |
      | DiagnosticoConcluido | a Execução   | aguardando_orcamento   | em_diagnostico       | GerarOrcamento     |
      | OrcamentoGerado      | o Billing    | aguardando_decisao     | aguardando_aprovacao | nenhum             |
      | OrcamentoAprovado    | o Billing    | aguardando_reserva     | aguardando_aprovacao | ReservarPecas      |
      | PecasReservadas      | a Execução   | aguardando_pagamento   | aguardando_pagamento | SolicitarPagamento |
      | PagamentoSolicitado  | o Billing    | aguardando_pagamento   | aguardando_pagamento | nenhum             |
      | PagamentoConfirmado  | o Billing    | aguardando_agendamento | aguardando_execucao  | AgendarExecucao    |
      | ExecucaoAgendada     | a Execução   | aguardando_inicio      | aguardando_execucao  | nenhum             |
      | ExecucaoIniciada     | a Execução   | em_execucao            | em_execucao          | nenhum             |
      | ExecucaoFinalizada   | a Execução   | concluida              | finalizada           | nenhum             |

  Cenário: Caminho feliz até a entrega
    Dado os eventos do caminho feliz anteriores a "ExecucaoFinalizada"
    Quando a Execução publica "ExecucaoFinalizada"
    E o atendente registra a entrega
    Então a OS fica "entregue" com a saga "concluida"
    E os registros da saga são, em ordem:
      | gatilho              |
      | abertura             |
      | DiagnosticoIniciado  |
      | DiagnosticoConcluido |
      | OrcamentoGerado      |
      | OrcamentoAprovado    |
      | PecasReservadas      |
      | PagamentoSolicitado  |
      | PagamentoConfirmado  |
      | ExecucaoAgendada     |
      | ExecucaoIniciada     |
      | ExecucaoFinalizada   |
    E nenhuma mensagem ficou na fila do OS

  Cenário: A OS mostra o orçamento e o checkout publicados pelo Billing
    Dado os eventos do caminho feliz anteriores a "PagamentoSolicitado"
    Quando o Billing publica "PagamentoSolicitado"
    Então a OS mostra o link de decisão e o checkout publicados pelo Billing

  Cenário: Evento adiantado volta para a fila até a saga alcançá-lo
    Quando a Execução publica "DiagnosticoConcluido"
    Então o evento "DiagnosticoConcluido" volta para a fila
    E a saga está na etapa "aguardando_diagnostico" com a OS "recebida"
    Quando a Execução publica "DiagnosticoIniciado"
    Então o Billing recebe o comando "GerarOrcamento"
    E a saga está na etapa "aguardando_orcamento" com a OS "em_diagnostico"
    E nenhuma mensagem ficou na fila do OS

  Cenário: Evento repetido é ignorado
    Dado os eventos do caminho feliz anteriores a "OrcamentoGerado"
    Quando a Execução publica "DiagnosticoIniciado"
    Então o evento "DiagnosticoIniciado" é ignorado
    E a saga está na etapa "aguardando_orcamento" com a OS "em_diagnostico"

  Esquema do Cenário: Falha de negócio desfaz os passos feitos e cancela a OS
    Dado os eventos do caminho feliz anteriores a "<proximo>"
    Quando <participante> publica "<falha>"
    Então a saga está na etapa "compensando" com a OS "<status>"
    Quando os participantes respondem a cada compensação
    Então as compensações enviadas, em ordem, são "<compensacoes>"
    E a OS fica cancelada pela saga com o motivo "<motivo>"
    E nenhuma mensagem ficou na fila do OS

    Exemplos:
      | proximo             | participante | falha                    | status               | compensacoes                                            | motivo                                    |
      | OrcamentoGerado     | o Billing    | GeracaoDeOrcamentoFalhou | em_diagnostico       | DescartarDiagnostico                                    | Falha na geração do orçamento             |
      | OrcamentoAprovado   | o Billing    | OrcamentoRecusado        | aguardando_aprovacao | DescartarDiagnostico                                    | Orçamento recusado pelo cliente           |
      | OrcamentoAprovado   | o Billing    | OrcamentoExpirado        | aguardando_aprovacao | DescartarDiagnostico                                    | Orçamento expirado sem decisão do cliente |
      | PecasReservadas     | a Execução   | ReservaDePecasFalhou     | aguardando_aprovacao | CancelarOrcamento, DescartarDiagnostico                 | Peças indisponíveis no estoque            |
      | PagamentoConfirmado | o Billing    | PagamentoRecusado        | aguardando_pagamento | LiberarReserva, CancelarOrcamento, DescartarDiagnostico | Pagamento recusado                        |
      | PagamentoConfirmado | o Billing    | PagamentoExpirado        | aguardando_pagamento | LiberarReserva, CancelarOrcamento, DescartarDiagnostico | Pagamento expirado                        |

  Cenário: Resposta repetida de uma compensação já respondida é ignorada
    Dado os eventos do caminho feliz anteriores a "PecasReservadas"
    E a Execução publica "ReservaDePecasFalhou"
    Quando o Billing publica "OrcamentoCancelado"
    E o Billing publica "OrcamentoCancelado"
    Então o evento "OrcamentoCancelado" é ignorado
    E as compensações enviadas, em ordem, são "CancelarOrcamento, DescartarDiagnostico"
