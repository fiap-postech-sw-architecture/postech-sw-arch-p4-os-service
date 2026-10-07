"""Saga em memoria para os testes do orquestrador: abertura real e eventos do contrato.

Tambem guarda o oraculo da classificacao (RFC-004 secoes 4.1 e 4.5), escrito
aqui a partir da tabela da RFC e nao importado do modulo testado.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.orquestrador import OrquestradorDaSaga
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import Classificacao
from src.ordem_servico.aplicacao.use_cases import AbrirOrdem
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE, ATOR_PROCESSO, ordem_em
from tests.unitarios.fakes import (
    ClientePortFake,
    FakeUnitOfWork,
    RepoEmMemoria,
    SagasEmMemoria,
)

if TYPE_CHECKING:
    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.orquestrador import Tratamento
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

C = Classificacao
S = StatusOrdem
PRAZO: Final = timedelta(seconds=120)

# Os eventos do caminho feliz, na ordem da tabela da RFC-004 secao 4.1.
FLUXO_FELIZ: Final = (
    "DiagnosticoIniciado",
    "DiagnosticoConcluido",
    "OrcamentoGerado",
    "OrcamentoAprovado",
    "PecasReservadas",
    "PagamentoSolicitado",
    "PagamentoConfirmado",
    "ExecucaoAgendada",
    "ExecucaoIniciada",
    "ExecucaoFinalizada",
)
# Ordem linear das etapas e etapa esperada de cada tipo (RFC-004 secao 4.1).
LINEAR: Final = (
    "aguardando_diagnostico",
    "aguardando_orcamento",
    "aguardando_decisao",
    "aguardando_reserva",
    "aguardando_pagamento",
    "aguardando_agendamento",
    "aguardando_inicio",
    "em_execucao",
    "concluida",
)
ESPERADA: Final = {
    "DiagnosticoIniciado": "aguardando_diagnostico",
    "DiagnosticoConcluido": "aguardando_diagnostico",
    "OrcamentoGerado": "aguardando_orcamento",
    "GeracaoDeOrcamentoFalhou": "aguardando_orcamento",
    "OrcamentoAprovado": "aguardando_decisao",
    "OrcamentoRecusado": "aguardando_decisao",
    "OrcamentoExpirado": "aguardando_decisao",
    "PecasReservadas": "aguardando_reserva",
    "ReservaDePecasFalhou": "aguardando_reserva",
    "PagamentoSolicitado": "aguardando_pagamento",
    "PagamentoConfirmado": "aguardando_pagamento",
    "PagamentoRecusado": "aguardando_pagamento",
    "PagamentoExpirado": "aguardando_pagamento",
    "ExecucaoAgendada": "aguardando_agendamento",
    "ExecucaoIniciada": "aguardando_inicio",
    "ExecucaoFinalizada": "em_execucao",
    "DiagnosticoDescartado": "compensando",
    "OrcamentoCancelado": "compensando",
    "ReservaLiberada": "compensando",
    "PagamentoCancelado": "compensando",
    "PagamentoEstornado": "compensando",
    "EstornoDePagamentoFalhou": "compensando",
    "ExecucaoCancelada": "compensando",
}
# Status com que a OS entra em cada etapa (o checkout ainda fechado em
# aguardando_pagamento); nas etapas da compensacao, um status anterior ao pivot.
STATUS_DE_ENTRADA: Final = {
    "aguardando_diagnostico": S.RECEBIDA,
    "aguardando_orcamento": S.EM_DIAGNOSTICO,
    "aguardando_decisao": S.AGUARDANDO_APROVACAO,
    "aguardando_reserva": S.AGUARDANDO_APROVACAO,
    "aguardando_pagamento": S.AGUARDANDO_PAGAMENTO,
    "aguardando_agendamento": S.AGUARDANDO_EXECUCAO,
    "aguardando_inicio": S.AGUARDANDO_EXECUCAO,
    "em_execucao": S.EM_EXECUCAO,
    "concluida": S.FINALIZADA,
    "compensando": S.AGUARDANDO_APROVACAO,
    "compensada": S.CANCELADA,
    "falha_na_compensacao": S.AGUARDANDO_EXECUCAO,
}


# Perfis da OS na matriz: os 9 status (AGUARDANDO_PAGAMENTO com o checkout
# aberto) e o AGUARDANDO_PAGAMENTO antes do PagamentoSolicitado.
SEM_CHECKOUT: Final = "aguardando_pagamento_sem_checkout"
PERFIS: Final = (*(s.value for s in StatusOrdem), SEM_CHECKOUT)
_COM_CHECKOUT: Final = {
    "aguardando_pagamento",
    "aguardando_execucao",
    "em_execucao",
    "finalizada",
}


def perfil_de_entrada(etapa: str) -> str:
    """Perfil da OS ao entrar na etapa (o checkout ainda fechado no pagamento)."""
    if etapa == "aguardando_pagamento":
        return SEM_CHECKOUT
    return STATUS_DE_ENTRADA[etapa].value


def ordem_no_perfil(perfil: str) -> OrdemDeServico:
    """OS no perfil pedido, so pelos fatos de dominio (``CANCELADA`` na abertura)."""
    if perfil == SEM_CHECKOUT:
        ordem = ordem_em(S.AGUARDANDO_APROVACAO)
        ordem.registrar_pecas_reservadas(ator=ATOR_PROCESSO)
        return ordem
    return ordem_em(S(perfil))


def esperado(etapa: str, tipo: str, perfil: str | None = None) -> Classificacao:
    """A regra da RFC-004 secao 4.5, com a OS no ``perfil`` (o de entrada, sem ele).

    OS encerrada (cancelada ou entregue) com a saga viva e o estado que o
    cancelamento recusa: o evento e recusado, nunca aplicado.
    """
    perfil = perfil or perfil_de_entrada(etapa)
    alvo = ESPERADA[tipo]
    if etapa in {"concluida", "compensada"}:
        return C.FORA_DA_COMPENSACAO if alvo == "compensando" else C.FORA_DO_FLUXO
    if perfil in {"entregue", "cancelada"}:
        return C.ORDEM_ENCERRADA
    if alvo == "compensando":
        return C.PROCESSAR if etapa == "compensando" else C.FORA_DA_COMPENSACAO
    if etapa not in LINEAR:
        return C.FORA_DO_FLUXO
    if LINEAR.index(alvo) < LINEAR.index(etapa):
        return C.OBSOLETO
    if LINEAR.index(alvo) > LINEAR.index(etapa):
        return C.ADIANTADO
    # Mesma etapa: o diagnostico (so a OS recebida nao o iniciou) e o checkout
    # desempatam o repetido e o adiantado.
    diagnostico, checkout = perfil != "recebida", perfil in _COM_CHECKOUT
    repetido = {
        "DiagnosticoIniciado": diagnostico,
        "PagamentoSolicitado": checkout,
    }
    if repetido.get(tipo):
        return C.REPETIDO
    adiantado = {
        "DiagnosticoConcluido": not diagnostico,
        "PagamentoConfirmado": not checkout,
        "PagamentoRecusado": not checkout,
        "PagamentoExpirado": not checkout,
    }
    if adiantado.get(tipo):
        return C.ADIANTADO
    return C.PROCESSAR


class Relogio:
    """Relogio que anda 1 s a cada leitura: instantes distintos e em ordem.

    Comeca no instante da abertura da OS (que usa o relogio real), para os
    passos seguintes nunca virem antes dela.
    """

    def __init__(self, inicio: datetime) -> None:
        self.agora = inicio

    def __call__(self) -> datetime:
        self.agora += timedelta(seconds=1)
        return self.agora


class CenarioDaSaga:
    """OS aberta pelo caso de uso real, fakes em memoria e o orquestrador real."""

    def __init__(self) -> None:
        self.ordens = RepoEmMemoria()
        self.sagas = SagasEmMemoria()
        self.publicador = FakeUnitOfWork()
        aberta = AbrirOrdem(
            self.ordens, self.publicador, ClientePortFake(), self.sagas
        ).executar(
            AbrirOrdemDTO(
                cliente_id=UUID(int=1),
                veiculo_id=UUID(int=2),
                descricao_problema="Barulho na suspensao",
                ator=ATOR_ATENDENTE,
            )
        )
        self.ordem_id: UUID = aberta.id
        # Dados dos eventos do Billing ja tratados, por tipo.
        self.billing: dict[str, Any] = {}
        self.relogio = Relogio(aberta.criado_em)
        self.orquestrador = OrquestradorDaSaga(
            ordens=self.ordens,
            sagas=self.sagas,
            publicador=self.publicador,
            prazo_resposta=PRAZO,
            relogio=self.relogio,
        )

    @classmethod
    def em(cls, etapa: str) -> CenarioDaSaga:
        """Saga na ``etapa``, com a OS no status de entrada dela.

        As etapas do fluxo normal vem pelo caminho feliz; as da compensacao,
        que ele nao alcanca, por uma saga reidratada.
        """
        cenario = cls()
        if etapa in LINEAR:
            cenario.levar_ate(etapa)
            return cenario
        cenario._reidratar(etapa, ordem_em(STATUS_DE_ENTRADA[etapa]))
        return cenario

    @classmethod
    def com_os(cls, etapa: str, perfil: str) -> CenarioDaSaga:
        """Saga reidratada na ``etapa`` com a OS no ``perfil`` (par legal ou nao)."""
        cenario = cls()
        cenario._reidratar(etapa, ordem_no_perfil(perfil))
        return cenario

    def _reidratar(self, etapa: str, ordem: OrdemDeServico) -> None:
        """Troca a OS e a saga pelas da ``ordem``, com a saga na ``etapa``.

        Nas etapas da compensacao, a saga leva o motivo e o que resta do plano
        (a ultima compensacao, que entra sempre).
        """
        compensacao = etapa in {"compensando", "falha_na_compensacao"}
        self.ordens.ordens = {ordem.id: ordem}
        self.sagas.sagas = {
            ordem.id: Saga(
                id=ordem.id,
                _etapa=EtapaSaga(etapa),
                _iniciada_em=self.relogio.agora,
                _etapa_desde=self.relogio.agora,
                _atualizada_em=self.relogio.agora,
                _motivo="cancelamento" if compensacao else None,
                _plano_compensacao=["DescartarDiagnostico"] if compensacao else [],
            )
        }
        self.ordem_id = ordem.id

    @property
    def saga(self) -> Saga:
        return self.sagas.sagas[self.ordem_id]

    @property
    def ordem(self) -> OrdemDeServico:
        return self.ordens.ordens[self.ordem_id]

    def evento(self, tipo: str, **dados: Any) -> MensagemRecebida:
        """Evento do contrato para esta OS; ``causation_id`` = ultimo comando."""
        causa = UUID(self.publicador.envelopes[-1]["id"])
        return evento(tipo, self.ordem_id, causation_id=causa, **dados)

    def receber(self, tipo: str, **dados: Any) -> Tratamento:
        recebido = self.evento(tipo, **dados)
        tratamento = self.orquestrador.tratar(recebido)
        if recebido.origem == "billing-service":
            self.billing[tipo] = recebido.dados
        return tratamento

    def levar_ate(self, etapa: EtapaSaga | str) -> None:
        """Caminho feliz ate a saga entrar na ``etapa`` (OS no status de entrada)."""
        alvo = EtapaSaga(etapa)
        for tipo in FLUXO_FELIZ:
            if self.saga.etapa is alvo:
                return
            self.receber(tipo)
        assert self.saga.etapa is alvo, f"o caminho feliz nao chega a {alvo}"
