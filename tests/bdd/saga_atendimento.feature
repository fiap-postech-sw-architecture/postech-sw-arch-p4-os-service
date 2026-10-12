# language: pt
Funcionalidade: Saga de atendimento orquestrada pelo OS Service
  O OS Service abre a ordem de serviço e conduz a saga pelos comandos e
  eventos da RFC-004 (seção 4.1). Billing e Execução respondem por um
  barramento em memória, com mensagens validadas pelos schemas de contratos/:
  cada evento publicado é entregue ao OS na hora, pelo mesmo caminho do
  consumidor (registro em mensagens_processadas, handler e commit), e os
  comandos que ele gera saem da outbox para o participante. O evento adiantado
  volta para a fila e é entregue de novo depois do seguinte; o recusado vai
  para a DLQ com o motivo. Os passos ficam em
  tests/integracao/test_saga_atendimento.py, ao lado do PostgreSQL de teste.

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

  Cenário: Falha de negócio sem tratador nesta versão vai para a DLQ
    Dado os eventos do caminho feliz anteriores a "OrcamentoAprovado"
    Quando o Billing publica "OrcamentoRecusado"
    Então o evento "OrcamentoRecusado" vai para a DLQ com o motivo "sem_tratador_nesta_versao"
    E a saga está na etapa "aguardando_decisao" com a OS "aguardando_aprovacao"
