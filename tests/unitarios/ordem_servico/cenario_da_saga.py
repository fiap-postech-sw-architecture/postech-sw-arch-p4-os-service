"""Saga em memoria para os testes do orquestrador: abertura real e eventos do contrato.

Tambem guarda o oraculo da classificacao (RFC-004 secoes 4.1 e 4.5), escrito
aqui a partir da tabela da RFC e nao importado do modulo testado.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.aplicacao.saga.orquestrador import OrquestradorDaSaga
from src.ordem_servico.aplicacao.saga.saga import Classificacao, EtapaSaga, Saga
from src.ordem_servico.aplicacao.use_cases import AbrirOrdem
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE, ordem_em
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
INICIO: Final = datetime(2026, 10, 7, 12, tzinfo=UTC)
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


def esperado(etapa: str, tipo: str) -> Classificacao:
    """A regra da RFC-004 secao 4.5 para a OS no status de entrada da etapa."""
    alvo = ESPERADA[tipo]
    if alvo == "compensando":
        return C.PROCESSAR if etapa == "compensando" else C.FORA_DA_COMPENSACAO
    if etapa not in LINEAR[:-1]:
        return C.FORA_DO_FLUXO
    if LINEAR.index(alvo) < LINEAR.index(etapa):
        return C.OBSOLETO
    if LINEAR.index(alvo) > LINEAR.index(etapa):
        return C.ADIANTADO
    # Mesma etapa, OS no status de entrada: so o DiagnosticoConcluido (OS
    # ainda recebida) e os desfechos do pagamento (checkout fechado) esperam.
    if tipo in {
        "DiagnosticoConcluido",
        "PagamentoConfirmado",
        "PagamentoRecusado",
        "PagamentoExpirado",
    }:
        return C.ADIANTADO
    return C.PROCESSAR


class Relogio:
    """Relogio que anda 1 s a cada leitura: instantes distintos e em ordem."""

    def __init__(self) -> None:
        self.agora = INICIO

    def __call__(self) -> datetime:
        self.agora += timedelta(seconds=1)
        return self.agora


class CenarioDaSaga:
    """OS aberta pelo caso de uso real, fakes em memoria e o orquestrador real."""

    def __init__(self) -> None:
        self.ordens = RepoEmMemoria()
        self.sagas = SagasEmMemoria()
        self.publicador = FakeUnitOfWork()
        self.relogio = Relogio()
        self.orquestrador = OrquestradorDaSaga(
            ordens=self.ordens,
            sagas=self.sagas,
            publicador=self.publicador,
            prazo_resposta=PRAZO,
            relogio=self.relogio,
        )
        self.ordem_id: UUID = (
            AbrirOrdem(self.ordens, self.publicador, ClientePortFake(), self.sagas)
            .executar(
                AbrirOrdemDTO(
                    cliente_id=UUID(int=1),
                    veiculo_id=UUID(int=2),
                    descricao_problema="Barulho na suspensao",
                    ator=ATOR_ATENDENTE,
                )
            )
            .id
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
        ordem = ordem_em(STATUS_DE_ENTRADA[etapa])
        cenario.ordens.ordens = {ordem.id: ordem}
        cenario.sagas.sagas = {
            ordem.id: Saga(
                id=ordem.id,
                _etapa=EtapaSaga(etapa),
                _iniciada_em=INICIO,
                _etapa_desde=INICIO,
                _atualizada_em=INICIO,
            )
        }
        cenario.ordem_id = ordem.id
        return cenario

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
        return self.orquestrador.tratar(self.evento(tipo, **dados))

    def levar_ate(self, etapa: EtapaSaga | str) -> None:
        """Caminho feliz ate a saga entrar na ``etapa`` (OS no status de entrada)."""
        alvo = EtapaSaga(etapa)
        for tipo in FLUXO_FELIZ:
            if self.saga.etapa is alvo:
                return
            self.receber(tipo)
        assert self.saga.etapa is alvo, f"o caminho feliz nao chega a {alvo}"
