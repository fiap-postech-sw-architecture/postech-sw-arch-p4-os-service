# language: pt
Funcionalidade: E-mail ao cliente a cada mudança de situação da OS
  A transição da OS grava o pedido de e-mail na mesma transação (RFC-004 seção
  7.2), e o processo notificador o entrega depois, com o texto do modelo. O pedido
  leva só a OS, o modelo, o status e o código do motivo: o destinatário, o nome e
  os links saem da OS e do cadastro do cliente na hora da entrega. A abertura
  (recebida) e o aguardando pagamento não têm e-mail: o do checkout vem no
  PagamentoSolicitado. Os participantes respondem por um barramento em memória,
  como na saga (saga_atendimento.feature); os passos ficam em
  tests/integracao/test_notificacoes_bdd.py.

  Contexto:
    Dado um cliente com e-mail e um veículo cadastrados
    E uma ordem de serviço aberta pelo atendente

  Cenário: Cada situação do caminho feliz pede o e-mail do seu modelo
    Dado os eventos do caminho feliz anteriores a "ExecucaoFinalizada"
    Quando a Execução publica "ExecucaoFinalizada"
    E o atendente registra a entrega
    Então os pedidos de e-mail, em ordem, são:
      | modelo    | status               | motivo |
      | situacao  | em_diagnostico       |        |
      | orcamento | aguardando_aprovacao |        |
      | pagamento | aguardando_pagamento |        |
      | situacao  | aguardando_execucao  |        |
      | situacao  | em_execucao          |        |
      | situacao  | finalizada           |        |
      | situacao  | entregue             |        |

  Cenário: Evento repetido e ignorado não pede e-mail
    Dado os eventos do caminho feliz anteriores a "OrcamentoGerado"
    Quando a Execução publica "DiagnosticoIniciado"
    Então nenhum pedido de e-mail novo foi gravado

  Cenário: O pedido de cancelamento só pede e-mail quando a OS é cancelada
    Dado os eventos do caminho feliz anteriores a "OrcamentoAprovado"
    Quando o atendente pede o cancelamento com o motivo "Texto digitado pelo atendente"
    Então nenhum pedido de e-mail novo foi gravado
    Quando os participantes respondem a cada compensação
    Então o último pedido de e-mail é "cancelamento" com o motivo "cancelamento"
    E nenhum pedido de e-mail guarda o texto do atendente

  Esquema do Cenário: Falha de negócio pede o e-mail de cancelamento com o código dela
    Dado os eventos do caminho feliz anteriores a "<proximo>"
    Quando <participante> publica "<falha>"
    E os participantes respondem a cada compensação
    Então o último pedido de e-mail é "cancelamento" com o motivo "<codigo>"

    Exemplos:
      | proximo           | participante | falha                    | codigo              |
      | OrcamentoGerado   | o Billing    | GeracaoDeOrcamentoFalhou | geracao_falhou      |
      | OrcamentoAprovado | o Billing    | OrcamentoRecusado        | orcamento_recusado  |
      | OrcamentoAprovado | o Billing    | OrcamentoExpirado        | orcamento_expirado  |
      | PecasReservadas   | a Execução   | ReservaDePecasFalhou     | reserva_falhou      |

  Esquema do Cenário: O texto de cada e-mail leva o que o cliente precisa para agir
    Dado os eventos do caminho feliz anteriores a "<evento>"
    Quando <participante> publica "<evento>"
    Então o e-mail do último pedido tem o assunto "<assunto>" e o texto "<texto>"

    Exemplos:
      | evento              | participante | assunto                       | texto                                                           |
      | DiagnosticoIniciado | a Execução   | Em diagnóstico                | foi atualizada para: Em diagnóstico.                            |
      | OrcamentoGerado     | o Billing    | orçamento para a sua decisão  | http://localhost/billing/api/v1/publico/orcamentos/              |
      | PagamentoSolicitado | o Billing    | pagamento                     | http://localhost/billing/simulador/checkout/                     |
      | ExecucaoFinalizada  | a Execução   | Finalizada                    | foi atualizada para: Finalizada.                                 |

  Cenário: Cliente apagado pela LGPD não recebe e-mail
    Dado os eventos do caminho feliz anteriores a "ExecucaoFinalizada"
    E a Execução publica "ExecucaoFinalizada"
    E o atendente registra a entrega
    E o cliente pediu a eliminação dos dados pessoais
    Quando o e-mail do último pedido é montado
    Então não há e-mail a enviar
