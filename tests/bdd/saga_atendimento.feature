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
      | PecasReservadas     | a Execução   | ReservaDePecasFalhou     | aguardando_aprovacao | CancelarOrcamento, DescartarDiagnostico                 | Falta de peça para a reserva              |
      | PagamentoConfirmado | o Billing    | PagamentoRecusado        | aguardando_pagamento | LiberarReserva, CancelarOrcamento, DescartarDiagnostico | Pagamento recusado                        |
      | PagamentoConfirmado | o Billing    | PagamentoExpirado        | aguardando_pagamento | LiberarReserva, CancelarOrcamento, DescartarDiagnostico | Pagamento não realizado no prazo          |

  Cenário: Resposta repetida de uma compensação já respondida é ignorada
    Dado os eventos do caminho feliz anteriores a "PecasReservadas"
    E a Execução publica "ReservaDePecasFalhou"
    Quando o Billing publica "OrcamentoCancelado"
    E o Billing publica "OrcamentoCancelado"
    Então o evento "OrcamentoCancelado" é ignorado
    E as compensações enviadas, em ordem, são "CancelarOrcamento, DescartarDiagnostico"

  Cenário: Evento de etapa já passada é ignorado
    Dado os eventos do caminho feliz anteriores a "PecasReservadas"
    Quando o Billing publica "OrcamentoGerado"
    Então o evento "OrcamentoGerado" é ignorado
    E a saga está na etapa "aguardando_reserva" com a OS "aguardando_aprovacao"

  Esquema do Cenário: Cancelamento em cada etapa desfaz os passos feitos e o que estava em voo
    Dado os eventos do caminho feliz anteriores a "<proximo>"
    Quando o atendente pede o cancelamento com o motivo "Cliente vendeu o carro"
    Então o pedido é aceito com a saga na etapa "compensando"
    E a OS segue "<status>" e guarda o motivo "Cliente vendeu o carro"
    E o comando enviado em seguida é "<primeira>"
    E o comando em voo da saga é "<primeira>"
    Quando os participantes respondem a cada compensação
    Então as compensações enviadas, em ordem, são "<compensacoes>"
    E a OS fica cancelada pela saga com o motivo "Cliente vendeu o carro"
    E nenhuma mensagem ficou na fila do OS

    Exemplos:
      | proximo              | status               | primeira             | compensacoes                                                                                    |
      | DiagnosticoIniciado  | recebida             | DescartarDiagnostico | DescartarDiagnostico                                                                            |
      | OrcamentoGerado      | em_diagnostico       | CancelarOrcamento    | CancelarOrcamento, DescartarDiagnostico                                                         |
      | OrcamentoAprovado    | aguardando_aprovacao | CancelarOrcamento    | CancelarOrcamento, DescartarDiagnostico                                                         |
      | PecasReservadas      | aguardando_aprovacao | LiberarReserva       | LiberarReserva, CancelarOrcamento, DescartarDiagnostico                                         |
      | PagamentoSolicitado  | aguardando_pagamento | EstornarPagamento    | EstornarPagamento, LiberarReserva, CancelarOrcamento, DescartarDiagnostico                      |
      | PagamentoConfirmado  | aguardando_pagamento | EstornarPagamento    | EstornarPagamento, LiberarReserva, CancelarOrcamento, DescartarDiagnostico                      |
      | ExecucaoAgendada     | aguardando_execucao  | CancelarExecucao     | CancelarExecucao, EstornarPagamento, LiberarReserva, CancelarOrcamento, DescartarDiagnostico    |
      | ExecucaoIniciada     | aguardando_execucao  | CancelarExecucao     | CancelarExecucao, EstornarPagamento, LiberarReserva, CancelarOrcamento, DescartarDiagnostico    |

  Cenário: A cobrança em aberto é cancelada e a compensação segue
    Dado os eventos do caminho feliz anteriores a "PagamentoConfirmado"
    Quando o atendente pede o cancelamento com o motivo "Cliente desistiu"
    E o Billing responde "PagamentoCancelado" ao "EstornarPagamento"
    Então as compensações enviadas, em ordem, são "EstornarPagamento, LiberarReserva"
    E a OS mostra o pagamento "cancelado"

  Cenário: Cancelamento repetido durante a compensação não muda nada
    Dado os eventos do caminho feliz anteriores a "OrcamentoAprovado"
    E o atendente pede o cancelamento com o motivo "Cliente vendeu o carro"
    Quando o atendente pede o cancelamento com o motivo "Outro motivo"
    Então o pedido é aceito com a saga na etapa "compensando"
    E nenhum comando saiu em seguida
    E a OS segue "aguardando_aprovacao" e guarda o motivo "Cliente vendeu o carro"
    E os registros da saga são, em ordem:
      | gatilho                 |
      | abertura                |
      | DiagnosticoIniciado     |
      | DiagnosticoConcluido    |
      | OrcamentoGerado         |
      | cancelamento_solicitado |

  Cenário: Cancelamento depois do início da execução é recusado
    Dado os eventos do caminho feliz anteriores a "ExecucaoFinalizada"
    Quando o atendente pede o cancelamento com o motivo "Tarde demais"
    Então o pedido é recusado com 409 e a mensagem "Cancelamento recusado: execução já iniciada"
    E a saga está na etapa "em_execucao" com a OS "em_execucao"
    E nenhum comando saiu em seguida

  Cenário: Cancelamento de uma OS já cancelada é recusado
    Dado os eventos do caminho feliz anteriores a "OrcamentoGerado"
    E o atendente pede o cancelamento com o motivo "Cliente vendeu o carro"
    E os participantes respondem a cada compensação
    Quando o atendente pede o cancelamento com o motivo "De novo"
    Então o pedido é recusado com 409 e a mensagem "Ordem já cancelada"

  Cenário: O início da execução vence o cancelamento
    Dado os eventos do caminho feliz anteriores a "ExecucaoIniciada"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    Quando a Execução publica "ExecucaoIniciada"
    Então a saga está na etapa "em_execucao" com a OS "em_execucao"
    E a OS não guarda mais o motivo do pedido
    E os registros da saga terminam em:
      | gatilho                 |
      | cancelamento_solicitado |
      | cancelamento_recusado   |
    E as compensações enviadas, em ordem, são "CancelarExecucao"
    Quando a Execução publica "ExecucaoFinalizada"
    Então a saga está na etapa "concluida" com a OS "finalizada"
    E nenhuma mensagem ficou na fila do OS

  Cenário: O cancelamento vence o início da execução
    Dado os eventos do caminho feliz anteriores a "ExecucaoIniciada"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    Quando a Execução responde "ExecucaoCancelada" ao "CancelarExecucao"
    E a Execução publica "ExecucaoIniciada"
    Então o evento "ExecucaoIniciada" é ignorado
    E a saga está na etapa "compensando" com a OS "aguardando_execucao"

  Cenário: Estorno recusado para a sequência e a saga espera o operador
    Dado os eventos do caminho feliz anteriores a "ExecucaoIniciada"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    E a Execução responde "ExecucaoCancelada" ao "CancelarExecucao"
    Quando o Billing responde "EstornoDePagamentoFalhou" ao "EstornarPagamento"
    Então a saga está na etapa "falha_na_compensacao" com a OS "aguardando_execucao"
    E a falha da saga é "estorno_recusado" com a compensação pendente "EstornarPagamento"
    E as compensações enviadas, em ordem, são "CancelarExecucao, EstornarPagamento"
    E nenhuma mensagem ficou na fila do OS

  Cenário: Resposta atrasada em falha na compensação só atualiza o resumo do pagamento
    Dado os eventos do caminho feliz anteriores a "ExecucaoIniciada"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    E a Execução responde "ExecucaoCancelada" ao "CancelarExecucao"
    E o Billing responde "EstornoDePagamentoFalhou" ao "EstornarPagamento"
    Quando o Billing responde "PagamentoEstornado" ao "EstornarPagamento"
    Então a saga está na etapa "falha_na_compensacao" com a OS "aguardando_execucao"
    E a OS mostra o pagamento "estornado"

  Cenário: Aprovação tardia estornada pelo Billing só atualiza o resumo do pagamento
    Dado os eventos do caminho feliz anteriores a "PagamentoConfirmado"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    Quando o Billing publica o estorno automático da aprovação tardia
    Então a saga está na etapa "compensando" com a OS "aguardando_pagamento"
    E a OS mostra o pagamento "estornado"
    E o comando enviado em seguida é "nenhum"
    E os registros da saga terminam em:
      | gatilho                 |
      | cancelamento_solicitado |

  Cenário: Pagamento confirmado durante a compensação é ignorado
    Dado os eventos do caminho feliz anteriores a "PagamentoConfirmado"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    Quando o Billing publica "PagamentoConfirmado"
    Então o evento "PagamentoConfirmado" é ignorado
    E a saga está na etapa "compensando" com a OS "aguardando_pagamento"
    E nenhum comando saiu em seguida

  Esquema do Cenário: Comando sem resposta é reenviado até o máximo e depois a saga compensa
    Dado os eventos do caminho feliz anteriores a "<proximo>"
    Quando o prazo de resposta vence 5 vezes
    Então o comando "<comando>" foi enviado 6 vezes, com id novo e os mesmos dados
    E a saga tem 5 reenvios no comando em voo
    E a saga está na etapa "<etapa>" com a OS "<status>"
    Quando o prazo de resposta vence
    Então a saga está na etapa "compensando" com a OS "<status>"
    E os registros da saga terminam em:
      | gatilho        |
      | reenvio        |
      | prazo_esgotado |
    E a saga tem 0 reenvios no comando em voo
    Quando os participantes respondem a cada compensação
    Então as compensações enviadas, em ordem, são "<compensacoes>"
    E a OS fica cancelada pela saga com o motivo "Serviço sem resposta no prazo técnico"
    E nenhuma mensagem ficou na fila do OS

    Exemplos:
      | proximo             | comando            | etapa                  | status               | compensacoes                                                                                 |
      | OrcamentoGerado     | GerarOrcamento     | aguardando_orcamento   | em_diagnostico       | CancelarOrcamento, DescartarDiagnostico                                                      |
      | PecasReservadas     | ReservarPecas      | aguardando_reserva     | aguardando_aprovacao | LiberarReserva, CancelarOrcamento, DescartarDiagnostico                                      |
      | PagamentoSolicitado | SolicitarPagamento | aguardando_pagamento   | aguardando_pagamento | EstornarPagamento, LiberarReserva, CancelarOrcamento, DescartarDiagnostico                   |
      | ExecucaoAgendada    | AgendarExecucao    | aguardando_agendamento | aguardando_execucao  | CancelarExecucao, EstornarPagamento, LiberarReserva, CancelarOrcamento, DescartarDiagnostico |

  Cenário: A resposta dentro do prazo não gasta reenvio
    Dado os eventos do caminho feliz anteriores a "OrcamentoGerado"
    Quando o Billing publica "OrcamentoGerado"
    E o prazo de resposta vence
    Então nenhum comando foi reenviado
    E a saga está na etapa "aguardando_decisao" com a OS "aguardando_aprovacao"

  Cenário: A resposta ao envio original vale depois do reenvio e a repetida ao reenvio é ignorada
    Dado os eventos do caminho feliz anteriores a "OrcamentoGerado"
    E o prazo de resposta vence
    Quando o Billing responde "OrcamentoGerado" ao envio original do "GerarOrcamento"
    E o Billing publica "OrcamentoGerado"
    Então o evento "OrcamentoGerado" é ignorado
    E a saga está na etapa "aguardando_decisao" com a OS "aguardando_aprovacao"
    E o comando "GerarOrcamento" foi enviado 2 vezes, com id novo e os mesmos dados
    E nenhuma mensagem ficou na fila do OS

  Cenário: Sem consumidor do OS o prazo vencido não reenvia nem esgota
    Dado os eventos do caminho feliz anteriores a "OrcamentoGerado"
    E o consumidor do OS está fora
    Quando o prazo de resposta vence 8 vezes
    Então nenhum comando foi reenviado
    E a saga está na etapa "aguardando_orcamento" com a OS "em_diagnostico"
    Quando o consumidor do OS volta
    E o prazo de resposta vence
    Então o comando "GerarOrcamento" foi enviado 2 vezes, com id novo e os mesmos dados
    E a saga tem 1 reenvios no comando em voo

  Cenário: Comando que ainda espera na outbox não vence
    Dado o relay está parado
    E os eventos do caminho feliz anteriores a "OrcamentoGerado"
    Quando o prazo de resposta vence 8 vezes
    Então nenhum comando foi reenviado
    E a saga está na etapa "aguardando_orcamento" com a OS "em_diagnostico"
    Quando o relay volta
    E o prazo de resposta vence
    Então a saga tem 1 reenvios no comando em voo

  Cenário: Compensação sem resposta para a saga em falha na compensação e a retomada a conclui
    Dado os eventos do caminho feliz anteriores a "ExecucaoIniciada"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    Quando o prazo de resposta vence 6 vezes
    Então a saga está na etapa "falha_na_compensacao" com a OS "aguardando_execucao"
    E a falha da saga é "reenvios_esgotados" com a compensação pendente "CancelarExecucao"
    E o comando "CancelarExecucao" foi enviado 6 vezes, com id novo e os mesmos dados
    Quando o prazo de resposta vence 3 vezes
    Então nenhum comando saiu em seguida
    Quando o operador retoma a compensação
    Então a retomada é aceita com a saga na etapa "compensando"
    E o comando "CancelarExecucao" foi enviado 7 vezes, com id novo e os mesmos dados
    E os registros da saga terminam em:
      | gatilho        |
      | prazo_esgotado |
      | retomada       |
    Quando os participantes respondem a cada compensação
    Então a OS fica cancelada pela saga com o motivo "Cliente desistiu"
    E nenhuma mensagem ficou na fila do OS

  Cenário: Retomada depois do estorno recusado segue o plano até a OS cancelada
    Dado os eventos do caminho feliz anteriores a "ExecucaoIniciada"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    E a Execução responde "ExecucaoCancelada" ao "CancelarExecucao"
    E o Billing responde "EstornoDePagamentoFalhou" ao "EstornarPagamento"
    Quando o operador retoma a compensação
    Então a retomada é aceita com a saga na etapa "compensando"
    E o comando enviado em seguida é "EstornarPagamento"
    Quando o Billing responde "PagamentoEstornado" ao "EstornarPagamento"
    E os participantes respondem a cada compensação
    Então a OS fica cancelada pela saga com o motivo "Cliente desistiu"
    E nenhuma mensagem ficou na fila do OS

  Cenário: A resposta tardia ao envio original conclui a compensação depois da retomada
    Dado os eventos do caminho feliz anteriores a "ExecucaoIniciada"
    E o atendente pede o cancelamento com o motivo "Cliente desistiu"
    E a Execução responde "ExecucaoCancelada" ao "CancelarExecucao"
    E o Billing responde "EstornoDePagamentoFalhou" ao "EstornarPagamento"
    E o operador retoma a compensação
    Quando o Billing responde "PagamentoEstornado" ao envio original do "EstornarPagamento"
    Então as compensações enviadas, em ordem, são "CancelarExecucao, EstornarPagamento, EstornarPagamento, LiberarReserva"

  Esquema do Cenário: Retomada só vale em falha na compensação
    Dado os eventos do caminho feliz anteriores a "<proximo>"
    Quando o operador retoma a compensação
    Então a retomada é recusada com 409 e a mensagem "Retomada recusada: a saga não está em falha na compensação"
    E nenhum comando saiu em seguida
    E a saga está na etapa "<etapa>" com a OS "<status>"

    Exemplos:
      | proximo             | etapa                  | status               |
      | DiagnosticoIniciado | aguardando_diagnostico | recebida             |
      | OrcamentoAprovado   | aguardando_decisao     | aguardando_aprovacao |
      | ExecucaoFinalizada  | em_execucao            | em_execucao          |
