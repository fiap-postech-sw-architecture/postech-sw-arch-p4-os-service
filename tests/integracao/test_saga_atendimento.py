"""Passos do BDD de componente da saga (``tests/bdd/saga_atendimento.feature``).

O OS roda inteiro sobre o PostgreSQL de teste; o RabbitMQ da lugar a um
barramento em memoria (ADR-041): os comandos saem da outbox e vao aos
participantes falsos, e os eventos que eles publicam passam pelo mesmo caminho
do consumidor, com o despachante do processo.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy import func, select, text, update

from src.compartilhado.aplicacao.mensageria import (
    Desfecho,
    FalhaPermanenteError,
    FalhaTransitoriaError,
    MensagemRecebida,
)
from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    TransicaoStatusInvalidaException,
)
from src.compartilhado.infraestrutura.database import BANCO_FORA
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.compartilhado.infraestrutura.outbox_mapping import (
    outbox_table,
    registrar_processada,
)
from src.compartilhado.infraestrutura.unit_of_work import TransacaoDaMensagem
from src.consumidor import montar_despachante
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.vencer_prazos import VencerPrazos
from src.ordem_servico.infraestrutura.transacao_dos_prazos import (
    TransacaoDosPrazosSQLAlchemy,
)
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_cancelar_ordem,
    obter_obter_ordem,
    obter_obter_saga,
    obter_registrar_entrega,
    obter_retomar_compensacao,
)
from tests.eventos import envelope_de_evento
from tests.integracao.seed_helpers import criar_cliente_com_veiculo
from tests.unitarios.fakes import PresencaFake

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from src.ordem_servico.aplicacao.dtos import OrdemDeServicoDTO, SagaDTO

scenarios("saga_atendimento.feature")

_ATENDENTE: Final = "0f8e2d7c-6b5a-4c3d-9e1f-a2b3c4d5e6f7"
_ADMIN: Final = "7c1d9a3e-2b4f-4e8a-b6c5-d0e1f2a3b4c5"
# Prazo de resposta e reenvios do `prazos` nos cenarios: os padroes da RFC-004 4.6.
_PRAZO: Final = timedelta(seconds=120)
_MAX_REENVIOS: Final = 5
# Usuario do broker de cada participante, como o catalogo diz quem publica.
_PARTICIPANTE: Final = {"o Billing": "billing", "a Execução": "execucao"}
# A copia de retry volta cinco vezes; a falha seguinte iria para a DLQ.
_ENTREGAS_ATE_A_DLQ: Final = 6
# O comando a que cada evento responde, ou o que abriu o fluxo dele: o evento
# leva o id desse comando no causation_id (RFC-004 secao 5.2).
_COMANDO_QUE_ABRIU: Final = {
    "DiagnosticoIniciado": "SolicitarDiagnostico",
    "DiagnosticoConcluido": "SolicitarDiagnostico",
    "OrcamentoGerado": "GerarOrcamento",
    "GeracaoDeOrcamentoFalhou": "GerarOrcamento",
    "OrcamentoAprovado": "GerarOrcamento",
    "OrcamentoRecusado": "GerarOrcamento",
    "OrcamentoExpirado": "GerarOrcamento",
    "PecasReservadas": "ReservarPecas",
    "ReservaDePecasFalhou": "ReservarPecas",
    "PagamentoSolicitado": "SolicitarPagamento",
    "PagamentoConfirmado": "SolicitarPagamento",
    "PagamentoRecusado": "SolicitarPagamento",
    "PagamentoExpirado": "SolicitarPagamento",
    "ExecucaoAgendada": "AgendarExecucao",
    "ExecucaoIniciada": "AgendarExecucao",
    "ExecucaoFinalizada": "AgendarExecucao",
    "ExecucaoCancelada": "CancelarExecucao",
    "PagamentoCancelado": "EstornarPagamento",
    "PagamentoEstornado": "EstornarPagamento",
    "EstornoDePagamentoFalhou": "EstornarPagamento",
    "ReservaLiberada": "LiberarReserva",
    "OrcamentoCancelado": "CancelarOrcamento",
    "DiagnosticoDescartado": "DescartarDiagnostico",
}
_CAMINHO_FELIZ: Final = (
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
# Como o participante responde a cada compensacao.
_RESPOSTA: Final = {
    "CancelarExecucao": "ExecucaoCancelada",
    "EstornarPagamento": "PagamentoEstornado",
    "LiberarReserva": "ReservaLiberada",
    "CancelarOrcamento": "OrcamentoCancelado",
    "DescartarDiagnostico": "DiagnosticoDescartado",
}


class Barramento:
    """Outbox -> participantes e participantes -> consumidor do OS, em memoria."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._despachante = montar_despachante(timedelta(seconds=120))
        self.fila_do_os: deque[tuple[dict[str, Any], int]] = deque()
        self.comandos: dict[str, dict[str, Any]] = {}
        self.entregues: list[str] = []
        # Cada envelope que saiu da outbox, na ordem, inclusive os reenvios.
        self.envelopes: list[dict[str, Any]] = []
        # Com o relay parado, os comandos esperam `pendente` na outbox.
        self.relay_parado = False
        self.caixas: dict[str, list[str]] = {"billing": [], "execucao": []}
        self.eventos: dict[str, dict[str, Any]] = {}
        self.desfechos: dict[str, str] = {}
        self.dlq: list[tuple[str, str]] = []

    def levar_a_outbox(self) -> None:
        """O relay: comandos pendentes vao, validados, a fila do participante."""
        if self.relay_parado:
            return
        with self._session_factory() as sessao:
            linhas = sessao.execute(
                select(outbox_table.c.id, outbox_table.c.envelope)
                .where(outbox_table.c.status == "pendente")
                .order_by(outbox_table.c.id)
            ).all()
            for linha in linhas:
                envelope = linha.envelope
                catalogo().validar(envelope)
                destino = catalogo().destino(envelope["tipo"]).routing_key
                self.caixas[destino.split(".")[1]].append(envelope["tipo"])
                self.comandos[envelope["tipo"]] = envelope
                self.envelopes.append(envelope)
                self.entregues.append(envelope["tipo"])
            sessao.execute(
                update(outbox_table)
                .where(outbox_table.c.id.in_([linha.id for linha in linhas]))
                .values(status="entregue", entregue_em=func.clock_timestamp())
            )
            sessao.commit()

    def publicar(self, envelope: dict[str, Any]) -> None:
        catalogo().validar(envelope)
        self.eventos[envelope["tipo"]] = envelope
        self.fila_do_os.append((envelope, 1))
        self.entregar()

    def entregar(self) -> None:
        """O consumidor: passa pela fila ate nenhuma mensagem andar.

        Cada mensagem grava ``mensagens_processadas``, roda o handler e comita
        numa transacao; a transitoria volta para o fim da fila, como a copia
        de retry, e a permanente vai para a DLQ com o motivo.
        """
        andou = True
        while andou and self.fila_do_os:
            andou = False
            for _ in range(len(self.fila_do_os)):
                envelope, entrega = self.fila_do_os.popleft()
                if self._consumir(envelope):
                    andou = True
                elif entrega == _ENTREGAS_ATE_A_DLQ:
                    self.dlq.append((envelope["tipo"], "tentativas_esgotadas"))
                else:
                    self.fila_do_os.append((envelope, entrega + 1))
        self.levar_a_outbox()

    def _consumir(self, envelope: dict[str, Any]) -> bool:
        mensagem = MensagemRecebida.do_envelope(envelope)
        with self._session_factory() as sessao:
            if not registrar_processada(sessao, mensagem.id):
                return True
            try:
                desfecho = self._despachante[mensagem.tipo](
                    mensagem, TransacaoDaMensagem(sessao)
                )
            except (FalhaTransitoriaError, ConflitoDeConcorrenciaException):
                sessao.rollback()
                self.desfechos[mensagem.tipo] = "adiantada"
                return False
            except FalhaPermanenteError as exc:
                sessao.rollback()
                self.desfechos[mensagem.tipo] = "recusada"
                self.dlq.append((mensagem.tipo, exc.motivo))
                return True
            sessao.commit()
        self.desfechos[mensagem.tipo] = desfecho.value
        return True


class Atendimento:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.barramento = Barramento(session_factory)
        self.ordem_id = UUID(int=0)
        self.veiculo: tuple[UUID, UUID] | None = None
        # Comandos entregues antes do ultimo evento publicado ou pedido.
        self.comandos_antes = 0
        self.cancelamento: OrdemDeServicoDTO | None = None
        self.recusa: TransicaoStatusInvalidaException | None = None
        self.retomada: SagaDTO | None = None
        # O consumidor do OS esta de pe (o `prazos` so age com ele).
        self.presenca = PresencaFake()

    def publicar(
        self,
        tipo: str,
        participante: str | None = None,
        *,
        causa: str | None = None,
        **dados: Any,
    ) -> None:
        """O participante publica o evento, no trace do comando que o abriu.

        ``causa`` e o comando que o evento responde, quando nao e o que abriu o
        fluxo dele (o estorno automatico leva o id do ``SolicitarPagamento``).
        """
        produtor = catalogo().produtor(tipo)
        if participante is not None and _PARTICIPANTE[participante] != produtor:
            msg = f"o cenario poe {participante} publicando {tipo}, de {produtor}"
            raise ValueError(msg)
        self.comandos_antes = len(self.barramento.entregues)
        causa = causa or _COMANDO_QUE_ABRIU[tipo]
        self.barramento.publicar(
            envelope_de_evento(
                tipo,
                correlation_id=self.ordem_id,
                causation_id=self.barramento.comandos[causa]["id"],
                **dados,
            )
        )

    def cancelar(self, motivo: str) -> None:
        """O atendente pede o cancelamento pelo caso de uso da API."""
        self.comandos_antes = len(self.barramento.entregues)
        self.cancelamento = None
        self.recusa = None
        with self.session_factory() as sessao:
            try:
                self.cancelamento = obter_cancelar_ordem(sessao).executar(
                    self.ordem_id, motivo, ator=_ATENDENTE
                )
            except TransicaoStatusInvalidaException as exc:
                self.recusa = exc
        self.barramento.levar_a_outbox()

    def vencer_prazo(self) -> None:
        """O prazo de resposta passa e o `prazos` roda um ciclo.

        O tempo anda no banco: o prazo gravado pela saga e a entrega de cada
        comando recuam um prazo e um segundo, e o ciclo roda com o relogio real,
        o mesmo do consumidor. Os comandos que ele gera saem da outbox em seguida.
        """
        recuo = _PRAZO.total_seconds() + 1
        with self.session_factory() as sessao:
            sessao.execute(
                text(
                    "UPDATE sagas SET prazo_resposta_em = "
                    "prazo_resposta_em - make_interval(secs => :recuo)"
                ),
                {"recuo": recuo},
            )
            sessao.execute(
                text(
                    "UPDATE outbox SET entregue_em = "
                    "entregue_em - make_interval(secs => :recuo) "
                    "WHERE status = 'entregue'"
                ),
                {"recuo": recuo},
            )
            sessao.commit()
        self.comandos_antes = len(self.barramento.entregues)
        VencerPrazos(
            abrir=lambda: TransacaoDosPrazosSQLAlchemy(self.session_factory),
            presenca=self.presenca,
            contexto=ContextoDeEnvio(_PRAZO),
            max_reenvios=_MAX_REENVIOS,
            banco_fora=BANCO_FORA,
        ).executar()
        self.barramento.levar_a_outbox()

    def retomar(self) -> None:
        """O admin retoma a compensacao parada pelo caso de uso da API."""
        self.comandos_antes = len(self.barramento.entregues)
        self.retomada = None
        self.recusa = None
        with self.session_factory() as sessao:
            try:
                self.retomada = obter_retomar_compensacao(sessao).executar(
                    self.ordem_id, ator=_ADMIN
                )
            except TransicaoStatusInvalidaException as exc:
                self.recusa = exc
        self.barramento.levar_a_outbox()

    def saga(self) -> SagaDTO:
        """A saga como o ``GET /api/v1/sagas/{id}`` a projeta."""
        with self.session_factory() as sessao:
            return obter_obter_saga(sessao).executar(self.ordem_id)

    def ordem(self) -> OrdemDeServicoDTO:
        """A OS como o ``GET /api/v1/ordens-de-servico/{id}`` a projeta."""
        with self.session_factory() as sessao:
            return obter_obter_ordem(sessao).executar(self.ordem_id)

    def compensacao_pendente(self) -> str | None:
        """A primeira do plano, como o ``GET /api/v1/sagas/{id}`` a mostra."""
        saga = self.saga()
        if saga.etapa != "compensando":
            return None
        return saga.plano_compensacao[0]


@given("um cliente com um veículo cadastrado", target_fixture="atendimento")
def _cliente_com_veiculo(session_factory: sessionmaker[Session]) -> Atendimento:
    atendimento = Atendimento(session_factory)
    with session_factory() as sessao:
        cliente = criar_cliente_com_veiculo(sessao)
        sessao.commit()
        atendimento.veiculo = (cliente.id, cliente.veiculos[0].id)
    return atendimento


@given("uma ordem de serviço aberta pelo atendente")
def _abrir(atendimento: Atendimento) -> None:
    assert atendimento.veiculo is not None
    cliente_id, veiculo_id = atendimento.veiculo
    with atendimento.session_factory() as sessao:
        atendimento.ordem_id = (
            obter_abrir_ordem(sessao)
            .executar(
                AbrirOrdemDTO(
                    cliente_id=cliente_id,
                    veiculo_id=veiculo_id,
                    descricao_problema="Barulho na suspensao dianteira",
                    ator=_ATENDENTE,
                )
            )
            .id
        )
    atendimento.barramento.levar_a_outbox()


@given(parsers.parse('os eventos do caminho feliz anteriores a "{evento}"'))
def _caminho_feliz_ate(atendimento: Atendimento, evento: str) -> None:
    anteriores = _CAMINHO_FELIZ[: _CAMINHO_FELIZ.index(evento)]
    for tipo in anteriores:
        atendimento.publicar(tipo)


@given(parsers.parse('{participante} publica "{tipo}"'))
@when(parsers.parse('{participante} publica "{tipo}"'))
def _publicar(atendimento: Atendimento, participante: str, tipo: str) -> None:
    atendimento.publicar(tipo, participante)


@given("os participantes respondem a cada compensação")
@when("os participantes respondem a cada compensação")
def _responder_compensacoes(atendimento: Atendimento) -> None:
    while (pendente := atendimento.compensacao_pendente()) is not None:
        atendimento.publicar(_RESPOSTA[pendente])


@when("o atendente registra a entrega")
def _entregar(atendimento: Atendimento) -> None:
    with atendimento.session_factory() as sessao:
        obter_registrar_entrega(sessao).executar(atendimento.ordem_id, ator=_ATENDENTE)


@then(parsers.parse('{participante} recebe o comando "{comando}"'))
def _recebe(atendimento: Atendimento, participante: str, comando: str) -> None:
    caixa = atendimento.barramento.caixas[_PARTICIPANTE[participante]]
    assert caixa[-1:] == [comando]
    envelope = atendimento.barramento.comandos[comando]
    assert envelope["correlation_id"] == str(atendimento.ordem_id)


@then(parsers.parse('o comando enviado em seguida é "{comando}"'))
def _comando_seguinte(atendimento: Atendimento, comando: str) -> None:
    novos = atendimento.barramento.entregues[atendimento.comandos_antes :]
    assert novos == ([] if comando == "nenhum" else [comando])


@then(parsers.parse('a saga está na etapa "{etapa}" com a OS "{status}"'))
def _etapa_e_status(atendimento: Atendimento, etapa: str, status: str) -> None:
    ordem = atendimento.ordem()
    assert (ordem.etapa, ordem.status) == (etapa, status)


@then(parsers.parse('a OS fica "{status}" com a saga "{etapa}"'))
def _final(atendimento: Atendimento, status: str, etapa: str) -> None:
    ordem = atendimento.ordem()
    assert (ordem.status, ordem.etapa) == (status, etapa)


@then(parsers.parse('as compensações enviadas, em ordem, são "{compensacoes}"'))
def _compensacoes(atendimento: Atendimento, compensacoes: str) -> None:
    enviadas = [t for t in atendimento.barramento.entregues if t in _RESPOSTA]
    assert enviadas == compensacoes.split(", ")


@then(parsers.parse('a OS fica cancelada pela saga com o motivo "{motivo}"'))
def _cancelada(atendimento: Atendimento, motivo: str) -> None:
    ordem = atendimento.ordem()
    assert (ordem.status, ordem.etapa, ordem.motivo_cancelamento) == (
        "cancelada",
        "compensada",
        motivo,
    )
    ultima = ordem.historico[-1]
    assert (ultima.para, ultima.origem, ultima.motivo) == ("cancelada", "saga", motivo)


@given(parsers.parse('o atendente pede o cancelamento com o motivo "{motivo}"'))
@when(parsers.parse('o atendente pede o cancelamento com o motivo "{motivo}"'))
def _cancelar_a_ordem(atendimento: Atendimento, motivo: str) -> None:
    atendimento.cancelar(motivo)


@given(parsers.parse('{participante} responde "{tipo}" ao "{comando}"'))
@when(parsers.parse('{participante} responde "{tipo}" ao "{comando}"'))
def _responder_ao_comando(
    atendimento: Atendimento, participante: str, tipo: str, comando: str
) -> None:
    atendimento.publicar(tipo, participante, causa=comando)


@when("o Billing publica o estorno automático da aprovação tardia")
def _estorno_automatico(atendimento: Atendimento) -> None:
    # O Billing estorna sozinho a aprovacao que chega depois do cancelamento: o
    # evento leva o id do SolicitarPagamento (RFC-004 secao 5.2).
    atendimento.publicar(
        "PagamentoEstornado",
        "o Billing",
        causa="SolicitarPagamento",
        motivo="pagamento_apos_encerramento",
    )


@then(parsers.parse('o pedido é aceito com a saga na etapa "{etapa}"'))
def _pedido_aceito(atendimento: Atendimento, etapa: str) -> None:
    assert atendimento.recusa is None
    assert atendimento.cancelamento is not None
    assert atendimento.cancelamento.etapa == etapa


@then(parsers.parse('o pedido é recusado com 409 e a mensagem "{mensagem}"'))
def _pedido_recusado(atendimento: Atendimento, mensagem: str) -> None:
    assert atendimento.cancelamento is None
    assert atendimento.recusa is not None
    assert (atendimento.recusa.codigo, atendimento.recusa.mensagem) == (
        "TRANSICAO_STATUS_INVALIDA",
        mensagem,
    )


@then(parsers.parse('a OS segue "{status}" e guarda o motivo "{motivo}"'))
def _guarda_o_pedido(atendimento: Atendimento, status: str, motivo: str) -> None:
    ordem = atendimento.ordem()
    assert (ordem.status, ordem.motivo_cancelamento) == (status, motivo)
    # O texto do atendente fica so na OS: a saga guarda codigos.
    assert motivo not in str(atendimento.saga())


@then("a OS não guarda mais o motivo do pedido")
def _descartou_o_pedido(atendimento: Atendimento) -> None:
    assert atendimento.ordem().motivo_cancelamento is None


@then(parsers.parse('o comando em voo da saga é "{comando}"'))
def _em_voo(atendimento: Atendimento, comando: str) -> None:
    em_voo = atendimento.saga().comando_em_voo
    assert em_voo is not None
    assert em_voo.tipo == comando
    assert atendimento.saga().prazo_resposta_em is not None


@then("nenhum comando saiu em seguida")
def _nenhum_comando(atendimento: Atendimento) -> None:
    assert atendimento.barramento.entregues[atendimento.comandos_antes :] == []


@then(
    parsers.parse('a falha da saga é "{falha}" com a compensação pendente "{comando}"')
)
def _falha(atendimento: Atendimento, falha: str, comando: str) -> None:
    saga = atendimento.saga()
    assert (saga.falha, saga.plano_compensacao[0]) == (falha, comando)
    # O comando em voo fica, sem prazo, para a retomada reenvia-lo.
    assert saga.comando_em_voo is not None
    assert (saga.comando_em_voo.tipo, saga.prazo_resposta_em) == (comando, None)


@then(parsers.parse('a OS mostra o pagamento "{status}"'))
def _pagamento(atendimento: Atendimento, status: str) -> None:
    pagamento = atendimento.ordem().pagamento
    assert pagamento is not None
    assert pagamento.status == status


@then("os registros da saga terminam em:")
def _registros_finais(atendimento: Atendimento, datatable: list[list[str]]) -> None:
    _cabecalho, *linhas = datatable
    esperados = [gatilho for (gatilho,) in linhas]
    gatilhos = [p["gatilho"] for p in atendimento.ordem().passos]
    assert gatilhos[-len(esperados) :] == esperados


@then("os registros da saga são, em ordem:")
def _registros(atendimento: Atendimento, datatable: list[list[str]]) -> None:
    _cabecalho, *linhas = datatable
    assert [p["gatilho"] for p in atendimento.ordem().passos] == [
        gatilho for (gatilho,) in linhas
    ]


@then("a OS mostra o link de decisão e o checkout publicados pelo Billing")
def _resumos(atendimento: Atendimento) -> None:
    eventos = atendimento.barramento.eventos
    orcamento = eventos["OrcamentoGerado"]["dados"]
    pagamento = eventos["PagamentoSolicitado"]["dados"]
    ordem = atendimento.ordem()
    assert ordem.orcamento is not None
    assert ordem.pagamento is not None
    assert (
        str(ordem.orcamento.orcamento_id),
        ordem.orcamento.total,
        ordem.orcamento.moeda,
        ordem.orcamento.link_decisao,
        ordem.orcamento.valido_ate,
    ) == (
        orcamento["orcamento_id"],
        Decimal(orcamento["total"]),
        orcamento["moeda"],
        orcamento["link_decisao"],
        datetime.fromisoformat(orcamento["valido_ate"]),
    )
    assert (
        str(ordem.pagamento.pagamento_id),
        ordem.pagamento.status,
        ordem.pagamento.valor,
        ordem.pagamento.moeda,
        ordem.pagamento.checkout_url,
        ordem.pagamento.expira_em,
    ) == (
        pagamento["pagamento_id"],
        "solicitado",
        Decimal(pagamento["valor"]),
        pagamento["moeda"],
        pagamento["checkout_url"],
        datetime.fromisoformat(pagamento["expira_em"]),
    )


@then(parsers.parse('o evento "{tipo}" volta para a fila'))
def _volta(atendimento: Atendimento, tipo: str) -> None:
    barramento = atendimento.barramento
    assert barramento.desfechos[tipo] == "adiantada"
    assert [e["tipo"] for e, _ in barramento.fila_do_os] == [tipo]


@then(parsers.parse('o evento "{tipo}" é ignorado'))
def _ignorado(atendimento: Atendimento, tipo: str) -> None:
    assert atendimento.barramento.desfechos[tipo] == Desfecho.IGNORADA.value


@then(parsers.parse('o evento "{tipo}" vai para a DLQ com o motivo "{motivo}"'))
def _na_dlq(atendimento: Atendimento, tipo: str, motivo: str) -> None:
    assert atendimento.barramento.dlq == [(tipo, motivo)]


@then("nenhuma mensagem ficou na fila do OS")
def _fila_vazia(atendimento: Atendimento) -> None:
    assert (list(atendimento.barramento.fila_do_os), atendimento.barramento.dlq) == (
        [],
        [],
    )


@given("o prazo de resposta vence")
@when("o prazo de resposta vence")
def _prazo_vence(atendimento: Atendimento) -> None:
    atendimento.vencer_prazo()


@when(parsers.parse("o prazo de resposta vence {vezes:d} vezes"))
def _prazo_vence_varias_vezes(atendimento: Atendimento, vezes: int) -> None:
    for _ in range(vezes):
        atendimento.vencer_prazo()


@given("o relay está parado")
def _relay_parado(atendimento: Atendimento) -> None:
    atendimento.barramento.relay_parado = True


@when("o relay volta")
def _relay_volta(atendimento: Atendimento) -> None:
    atendimento.barramento.relay_parado = False
    atendimento.barramento.levar_a_outbox()


@given("o consumidor do OS está fora")
def _consumidor_fora(atendimento: Atendimento) -> None:
    atendimento.presenca.ha = False


@when("o consumidor do OS volta")
def _consumidor_volta(atendimento: Atendimento) -> None:
    atendimento.presenca.ha = True


@given("o operador retoma a compensação")
@when("o operador retoma a compensação")
def _retomar(atendimento: Atendimento) -> None:
    atendimento.retomar()


@then(parsers.parse('a retomada é aceita com a saga na etapa "{etapa}"'))
def _retomada_aceita(atendimento: Atendimento, etapa: str) -> None:
    assert atendimento.recusa is None
    assert atendimento.retomada is not None
    assert (atendimento.retomada.etapa, atendimento.retomada.falha) == (etapa, None)


@then(parsers.parse('a retomada é recusada com 409 e a mensagem "{mensagem}"'))
def _retomada_recusada(atendimento: Atendimento, mensagem: str) -> None:
    assert atendimento.retomada is None
    assert atendimento.recusa is not None
    assert (atendimento.recusa.codigo, atendimento.recusa.mensagem) == (
        "TRANSICAO_STATUS_INVALIDA",
        mensagem,
    )


def _envios(atendimento: Atendimento, comando: str) -> list[dict[str, Any]]:
    return [e for e in atendimento.barramento.envelopes if e["tipo"] == comando]


@then(
    parsers.parse(
        'o comando "{comando}" foi enviado {vezes:d} vezes, com id novo e os '
        "mesmos dados"
    )
)
def _enviado_varias_vezes(atendimento: Atendimento, comando: str, vezes: int) -> None:
    envios = _envios(atendimento, comando)
    assert len(envios) == vezes
    assert len({e["id"] for e in envios}) == vezes
    assert all(e["dados"] == envios[0]["dados"] for e in envios)
    # O reenvio nao tem causa (a causa e o prazo); o original, a de quem o abriu.
    assert {e["causation_id"] for e in envios[1:]} <= {None}


@then(parsers.parse("a saga tem {reenvios:d} reenvios no comando em voo"))
def _reenvios(atendimento: Atendimento, reenvios: int) -> None:
    assert atendimento.saga().reenvios == reenvios


@then("nenhum comando foi reenviado")
def _nenhum_reenvio(atendimento: Atendimento) -> None:
    assert atendimento.saga().reenvios == 0
    assert atendimento.barramento.entregues[atendimento.comandos_antes :] == []
    assert "reenvio" not in [p["gatilho"] for p in atendimento.ordem().passos]


@given(parsers.parse('o Billing responde "{tipo}" ao envio original do "{comando}"'))
@when(parsers.parse('o Billing responde "{tipo}" ao envio original do "{comando}"'))
def _responder_ao_original(atendimento: Atendimento, tipo: str, comando: str) -> None:
    original = _envios(atendimento, comando)[0]
    atendimento.comandos_antes = len(atendimento.barramento.entregues)
    atendimento.barramento.publicar(
        envelope_de_evento(
            tipo,
            correlation_id=atendimento.ordem_id,
            causation_id=original["id"],
        )
    )
