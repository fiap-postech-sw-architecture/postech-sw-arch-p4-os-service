# language: pt
Funcionalidade: Saga de atendimento orquestrada pelo OS Service
  O OS Service abre a ordem de serviço e conduz a saga pelos comandos e
  eventos da RFC-004 (seção 4.1). Billing e Execução respondem por um
  barramento em memória, com mensagens validadas pelos schemas de contratos/,
  e cada evento passa pelo mesmo caminho do consumidor: registro em
  mensagens_processadas, handler e commit; o evento adiantado volta para a
  fila. Os passos ficam em tests/integracao/test_saga_atendimento.py, ao lado
  do PostgreSQL de teste.

  Contexto:
    Dado um cliente com um veículo cadastrado
    Quando o atendente abre a ordem de serviço
    Então a Execução recebe o comando "SolicitarDiagnostico"
    E a saga está na etapa "aguardando_diagnostico" com a OS "recebida"

  Cenário: Caminho feliz da abertura à entrega
    Quando a Execução publica "DiagnosticoIniciado"
    Então a saga está na etapa "aguardando_diagnostico" com a OS "em_diagnostico"
    Quando a Execução publica "DiagnosticoConcluido"
    Então o Billing recebe o comando "GerarOrcamento"
    E a saga está na etapa "aguardando_orcamento" com a OS "em_diagnostico"
    Quando o Billing publica "OrcamentoGerado"
    Então a saga está na etapa "aguardando_decisao" com a OS "aguardando_aprovacao"
    Quando o Billing publica "OrcamentoAprovado"
    Então a Execução recebe o comando "ReservarPecas"
    E a saga está na etapa "aguardando_reserva" com a OS "aguardando_aprovacao"
    Quando a Execução publica "PecasReservadas"
    Então o Billing recebe o comando "SolicitarPagamento"
    E a saga está na etapa "aguardando_pagamento" com a OS "aguardando_pagamento"
    Quando o Billing publica "PagamentoSolicitado"
    E o Billing publica "PagamentoConfirmado"
    Então a Execução recebe o comando "AgendarExecucao"
    E a saga está na etapa "aguardando_agendamento" com a OS "aguardando_execucao"
    Quando a Execução publica "ExecucaoAgendada"
    Então a saga está na etapa "aguardando_inicio" com a OS "aguardando_execucao"
    Quando a Execução publica "ExecucaoIniciada"
    Então a saga está na etapa "em_execucao" com a OS "em_execucao"
    Quando a Execução publica "ExecucaoFinalizada"
    Então a saga está na etapa "concluida" com a OS "finalizada"
    Quando o atendente registra a entrega
    Então a OS fica "entregue" com a saga "concluida"
    E nenhuma mensagem ficou na fila do OS

  Cenário: Evento adiantado volta para a fila e o repetido é ignorado
    Quando a Execução publica "DiagnosticoConcluido"
    Então o evento "DiagnosticoConcluido" volta para a fila
    E a saga está na etapa "aguardando_diagnostico" com a OS "recebida"
    Quando a Execução publica "DiagnosticoIniciado"
    Então o Billing recebe o comando "GerarOrcamento"
    E a saga está na etapa "aguardando_orcamento" com a OS "em_diagnostico"
    Quando a Execução publica "DiagnosticoIniciado"
    Então o evento "DiagnosticoIniciado" é ignorado
    E a saga está na etapa "aguardando_orcamento" com a OS "em_diagnostico"
    E nenhuma mensagem ficou na fila do OS
