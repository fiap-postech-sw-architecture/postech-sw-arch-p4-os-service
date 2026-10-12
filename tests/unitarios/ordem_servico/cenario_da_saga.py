"""Saga em memoria para os testes do orquestrador: abertura real e eventos do contrato.

Tambem guarda o oraculo da classificacao (RFC-004 secoes 4.1, 4.4 e 4.5),
escrito aqui a partir das tabelas da RFC e nao importado do modulo testado.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.orquestrador import OrquestradorDaSaga
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.use_cases import AbrirOrdem, CancelarOrdem
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
    from collections.abc import Sequence

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.dtos import OrdemDeServicoDTO
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


# A compensacao a que cada resposta responde (RFC-004 secao 4.3) e as cinco, na
# ordem em que saem (secao 4.4).
RESPONDE: Final = {
    "ExecucaoCancelada": "CancelarExecucao",
    "PagamentoCancelado": "EstornarPagamento",
    "PagamentoEstornado": "EstornarPagamento",
    "EstornoDePagamentoFalhou": "EstornarPagamento",
    "ReservaLiberada": "LiberarReserva",
    "OrcamentoCancelado": "CancelarOrcamento",
    "DiagnosticoDescartado": "DescartarDiagnostico",
}
COMPENSACOES: Final = (
    "CancelarExecucao",
    "EstornarPagamento",
    "LiberarReserva",
    "CancelarOrcamento",
    "DescartarDiagnostico",
)
# O que a saga ja fez em cada etapa antes do pivot: os passos T concluidos e o
# comando com prazo que espera resposta (RFC-004 secoes 4.1 e 4.3). No
# pagamento, antes e depois do PagamentoSolicitado (o checkout aberto conclui
# o T6).
FEITO: Final[dict[str, tuple[tuple[str, ...], str | None]]] = {
    "aguardando_diagnostico": ((), None),
    "aguardando_orcamento": ((), "GerarOrcamento"),
    "aguardando_decisao": (("T3",), None),
    "aguardando_reserva": (("T3",), "ReservarPecas"),
    "aguardando_pagamento": (("T3", "T5"), "SolicitarPagamento"),
    "aguardando_pagamento_com_checkout": (("T3", "T5", "T6"), None),
    "aguardando_agendamento": (("T3", "T5", "T6"), "AgendarExecucao"),
    "aguardando_inicio": (("T3", "T5", "T6", "T7"), None),
}
# O plano de cada linha da tabela da RFC-004 secao 4.4, pelo que a saga ja fez
# (``FEITO``) e pelo motivo; as linhas com dois gatilhos ("cancelamento ou
# prazo") tem uma entrada por gatilho.
PLANO_DA_RFC: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    ("aguardando_diagnostico", "cancelamento"): COMPENSACOES[4:],
    ("aguardando_orcamento", "geracao_falhou"): COMPENSACOES[4:],
    ("aguardando_orcamento", "cancelamento"): COMPENSACOES[3:],
    ("aguardando_orcamento", "prazo_tecnico"): COMPENSACOES[3:],
    ("aguardando_decisao", "orcamento_recusado"): COMPENSACOES[4:],
    ("aguardando_decisao", "orcamento_expirado"): COMPENSACOES[4:],
    ("aguardando_decisao", "cancelamento"): COMPENSACOES[3:],
    ("aguardando_reserva", "reserva_falhou"): COMPENSACOES[3:],
    ("aguardando_reserva", "cancelamento"): COMPENSACOES[2:],
    ("aguardando_reserva", "prazo_tecnico"): COMPENSACOES[2:],
    ("aguardando_pagamento_com_checkout", "pagamento_recusado"): COMPENSACOES[2:],
    ("aguardando_pagamento_com_checkout", "pagamento_expirado"): COMPENSACOES[2:],
    ("aguardando_pagamento", "cancelamento"): COMPENSACOES[1:],
    ("aguardando_pagamento_com_checkout", "cancelamento"): COMPENSACOES[1:],
    ("aguardando_pagamento", "prazo_tecnico"): COMPENSACOES[1:],
    ("aguardando_agendamento", "cancelamento"): COMPENSACOES,
    ("aguardando_agendamento", "prazo_tecnico"): COMPENSACOES,
    ("aguardando_inicio", "cancelamento"): COMPENSACOES,
}
# A compensacao pendente das etapas da compensacao quando o teste nao diz
# outra: a ultima do plano, que entra sempre.
PENDENTE_PADRAO: Final = "DescartarDiagnostico"
EM_COMPENSACAO: Final = {"compensando", "falha_na_compensacao"}

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


def esperado(
    etapa: str,
    tipo: str,
    perfil: str | None = None,
    *,
    pendente: str | None = None,
    casada: bool = False,
) -> Classificacao:
    """A regra da RFC-004 secoes 4.4 e 4.5, com a OS no ``perfil`` (sem ele, o
    de entrada da etapa).

    ``pendente`` e a compensacao que espera resposta (nas etapas da
    compensacao, ``PENDENTE_PADRAO`` sem ela) e ``casada``, se o
    ``causation_id`` do evento e o de um envio dela. OS encerrada (cancelada ou
    entregue) com a saga viva e o estado que o cancelamento recusa: o evento e
    recusado, nunca aplicado. O ``PagamentoEstornado`` que nao responde ao
    ``EstornarPagamento`` e o estorno automatico, em qualquer etapa.
    """
    perfil = perfil or perfil_de_entrada(etapa)
    if etapa in EM_COMPENSACAO:
        pendente = pendente or PENDENTE_PADRAO
    alvo = ESPERADA[tipo]
    responde = (
        etapa == "compensando"
        and RESPONDE.get(tipo) == pendente
        and (casada or pendente != "EstornarPagamento")
    )
    if tipo == "PagamentoEstornado" and not responde:
        return C.ESTORNO_AUTOMATICO
    if etapa in {"concluida", "compensada"}:
        return C.FORA_DA_COMPENSACAO if alvo == "compensando" else C.FORA_DO_FLUXO
    if perfil in {"entregue", "cancelada"}:
        return C.ORDEM_ENCERRADA
    if alvo == "compensando":
        return C.PROCESSAR if responde else C.FORA_DA_COMPENSACAO
    if etapa in EM_COMPENSACAO:
        # So o inicio e o fim da execucao contam. Com o CancelarExecucao
        # pendente, o inicio decide a corrida do pivot e o fim que chegou antes
        # dele espera (adiantado); com outra pendente, contradizem a saga.
        if tipo not in {"ExecucaoIniciada", "ExecucaoFinalizada"}:
            return C.FORA_DO_FLUXO
        if pendente != "CancelarExecucao":
            return C.INCOERENTE
        return C.PROCESSAR if tipo == "ExecucaoIniciada" else C.ADIANTADO
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

    @classmethod
    def em_compensacao(
        cls,
        plano: Sequence[str],
        *,
        status: StatusOrdem = StatusOrdem.AGUARDANDO_EXECUCAO,
        motivo: str = "cancelamento",
        falha: str | None = None,
    ) -> CenarioDaSaga:
        """Saga reidratada compensando pelo ``plano``, ou parada nele (``falha``).

        A OS chega ao ``status`` pelos fatos de dominio. A pendente (a primeira
        do plano) sai pelo publicador, e o comando em voo guarda o id dela: o
        evento do cenario a responde pelo ``causation_id``. Parada na falha, o
        comando em voo fica, sem prazo.
        """
        cenario = cls()
        cenario._reidratar(
            "falha_na_compensacao" if falha else "compensando",
            ordem_em(status),
            plano=plano,
            motivo=motivo,
            falha=falha,
        )
        return cenario

    def _reidratar(
        self,
        etapa: str,
        ordem: OrdemDeServico,
        *,
        plano: Sequence[str] = (PENDENTE_PADRAO,),
        motivo: str = "cancelamento",
        falha: str | None = None,
    ) -> None:
        """Troca a OS e a saga pelas da ``ordem``, com a saga na ``etapa``.

        Nas etapas da compensacao, a saga leva o motivo e o ``plano`` restante
        (sem ele, a ultima compensacao, que entra sempre); em compensando, a
        pendente sai pelo publicador e fica em voo, e na falha a saga guarda a
        ``falha`` (estorno recusado, sem ela).
        """
        self.ordens.ordens = {ordem.id: ordem}
        self.ordem_id = ordem.id
        agora = self.relogio.agora
        campos: dict[str, Any] = {}
        if etapa in EM_COMPENSACAO:
            campos = {"_motivo": motivo, "_plano_compensacao": list(plano)}
        if etapa in EM_COMPENSACAO:
            dados = {"ordem_id": str(ordem.id), "motivo": motivo}
            enviado = self.publicador.publicar_comando(
                plano[0], dados, correlation_id=ordem.id
            )
            campos["_comando_em_voo"] = {
                "tipo": plano[0],
                "dados": dados,
                "mensagem_ids": [str(enviado)],
                "enviado_em": agora.isoformat(),
            }
            # Parada na falha, o comando em voo fica sem prazo (RFC-004 4.7).
            if etapa == "compensando":
                campos["_prazo_resposta_em"] = agora + PRAZO
        if etapa == "falha_na_compensacao":
            campos["_falha"] = falha or "estorno_recusado"
        self.sagas.sagas = {
            ordem.id: Saga(
                id=ordem.id,
                _etapa=EtapaSaga(etapa),
                _iniciada_em=agora,
                _etapa_desde=agora,
                _atualizada_em=agora,
                **campos,
            )
        }

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

    def cancelar(self, motivo: str = "cliente desistiu") -> OrdemDeServicoDTO:
        """O atendente pede o cancelamento pelo caso de uso real, na mesma outbox."""
        return CancelarOrdem(
            self.ordens,
            self.publicador,
            self.sagas,
            prazo_resposta=PRAZO,
            relogio=self.relogio,
        ).executar(self.ordem_id, motivo, ator=ATOR_ATENDENTE)

    def levar_ate(self, etapa: EtapaSaga | str) -> None:
        """Caminho feliz ate a saga entrar na ``etapa`` (OS no status de entrada)."""
        alvo = EtapaSaga(etapa)
        for tipo in FLUXO_FELIZ:
            if self.saga.etapa is alvo:
                return
            self.receber(tipo)
        assert self.saga.etapa is alvo, f"o caminho feliz nao chega a {alvo}"
