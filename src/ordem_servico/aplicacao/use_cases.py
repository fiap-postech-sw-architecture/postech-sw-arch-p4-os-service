"""Casos de uso da aplicacao Ordem de Servico.

Cada classe expoe ``executar(...)``: compoe repositorio, ``UnitOfWork`` e
ports; as regras ficam no agregado. A abertura inicia a saga e grava o
``SolicitarDiagnostico`` na outbox com ``UnitOfWork.publicar_comando``, no
mesmo commit da OS, e o cancelamento a leva a compensar, com a primeira
compensacao no mesmo commit; a retomada reenvia a compensacao parada; os passos
seguintes sao do ``OrquestradorDaSaga``, e os prazos, do ``VencerPrazos``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

import structlog

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.cnpj import CNPJ
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.documento import normalizar_cnpj
from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    DomainException,
    TransicaoStatusInvalidaException,
)
from src.compartilhado.dominio.placa import Placa
from src.ordem_servico.aplicacao.dtos import (
    AcompanhamentoDTO,
    ComandoEmVooDTO,
    MudancaDeStatusDTO,
    OrdemDeServicoDTO,
    OrdemResumoDTO,
    ResumoOrcamentoDTO,
    ResumoPagamentoDTO,
    SagaDTO,
)
from src.ordem_servico.aplicacao.saga.contexto_de_envio import enviar
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    GatilhoDeProcesso,
    SagaNaoEncontradaException,
)
from src.ordem_servico.aplicacao.saga.prazos import comando_a_reenviar
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    ANTES_DO_PIVOT,
    ETAPAS_EM_COMPENSACAO,
)
from src.ordem_servico.dominio.exceptions import (
    ClienteNaoEncontradoException,
    OrdemNaoEncontradaException,
    VeiculoNaoEncontradoException,
)
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.texto_livre import motivo_de_cancelamento

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.compartilhado.dominio.documento import Documento
    from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
    from src.ordem_servico.aplicacao.ports import (
        ClientePort,
        ConsultaAcompanhamento,
        ConsultaDaOrdem,
        SagaRepository,
    )
    from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository

_log = structlog.get_logger(__name__)


def _ordem_dto(ordem: OrdemDeServico, saga: Saga | None) -> OrdemDeServicoDTO:
    """Projeta a OS para ``OrdemDeServicoDTO``, com o historico e a etapa da saga."""
    orcamento = ordem.resumo_orcamento
    pagamento = ordem.resumo_pagamento
    return OrdemDeServicoDTO(
        id=ordem.id,
        cliente_id=ordem.cliente_id,
        veiculo_id=ordem.veiculo_id,
        descricao_problema=ordem.descricao_problema,
        status=ordem.status.value,
        orcamento=(
            ResumoOrcamentoDTO(
                orcamento_id=orcamento.orcamento_id,
                total=orcamento.total.valor,
                moeda=orcamento.total.moeda,
                link_decisao=orcamento.link_decisao,
                valido_ate=orcamento.valido_ate,
            )
            if orcamento is not None
            else None
        ),
        pagamento=(
            ResumoPagamentoDTO(
                pagamento_id=pagamento.pagamento_id,
                status=pagamento.status.value,
                valor=pagamento.valor.valor,
                moeda=pagamento.valor.moeda,
                checkout_url=pagamento.checkout_url,
                expira_em=pagamento.expira_em,
            )
            if pagamento is not None
            else None
        ),
        motivo_cancelamento=ordem.motivo_cancelamento,
        versao=ordem.versao,
        criado_em=ordem.criado_em,
        atualizado_em=ordem.atualizado_em,
        historico=tuple(
            MudancaDeStatusDTO(
                sequencia=m.sequencia,
                de=m.de.value if m.de is not None else None,
                para=m.para.value,
                origem=m.origem.value,
                motivo=m.motivo,
                ator=m.ator,
                ocorrido_em=m.ocorrido_em,
            )
            for m in ordem.historico
        ),
        etapa=saga.etapa.value if saga is not None else None,
        passos=saga.passos if saga is not None else (),
    )


def _obter_ordem(repo: OrdemDeServicoRepository, ordem_id: UUID) -> OrdemDeServico:
    ordem = repo.obter_por_id(ordem_id)
    if ordem is None:
        raise OrdemNaoEncontradaException(ordem_id)
    return ordem


class AbrirOrdem:
    """T1 da saga: abre a OS em RECEBIDA para um cliente ativo e um veiculo dele.

    Na mesma transacao, a saga nasce em ``aguardando_diagnostico`` e o
    ``SolicitarDiagnostico`` vai para a outbox (RFC-004 secao 4).
    """

    def __init__(
        self,
        repo: OrdemDeServicoRepository,
        uow: UnitOfWork,
        cliente_port: ClientePort,
        sagas: SagaRepository,
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._cliente_port = cliente_port
        self._sagas = sagas

    def executar(self, dto: AbrirOrdemDTO) -> OrdemDeServicoDTO:
        """Valida cliente e veiculo, persiste OS e saga e grava o comando.

        Raises:
            ClienteNaoEncontradoException: cliente inexistente ou inativo (404).
            VeiculoNaoEncontradoException: veiculo inexistente ou de outro
                cliente (404; os dois casos sao indistinguiveis de proposito).
            ValorInvalidoException: descricao do problema vazia, longa demais
                ou com caractere de controle (422).
        """
        if not self._cliente_port.cliente_existe(dto.cliente_id):
            raise ClienteNaoEncontradoException(dto.cliente_id)
        veiculo = self._cliente_port.retrato_do_veiculo(dto.cliente_id, dto.veiculo_id)
        if veiculo is None:
            raise VeiculoNaoEncontradoException(dto.veiculo_id)
        ordem = OrdemDeServico.abrir(
            cliente_id=dto.cliente_id,
            veiculo_id=dto.veiculo_id,
            descricao_problema=dto.descricao_problema,
            ator=dto.ator,
        )
        with self._uow:
            self._repo.salvar(ordem)
            # Causa e a requisicao HTTP: sem causation_id (RFC-004 secao 5.2).
            envio = enviar(
                self._uow,
                (
                    Comando.SOLICITAR_DIAGNOSTICO,
                    {
                        "ordem_id": ordem.id,
                        "veiculo_id": ordem.veiculo_id,
                        "veiculo": {
                            "placa": veiculo.placa,
                            "marca": veiculo.marca,
                            "modelo": veiculo.modelo,
                            "ano": veiculo.ano,
                        },
                        "descricao_problema": ordem.descricao_problema,
                    },
                ),
                correlation_id=ordem.id,
            )
            saga = Saga.iniciar(
                ordem.id, envio=envio, ator=dto.ator, agora=ordem.criado_em
            )
            self._sagas.salvar(saga)
            self._uow.commit()
        _log.info("saga started", correlation_id=str(ordem.id), etapa=saga.etapa.value)
        return _ordem_dto(ordem, saga)


class ListarOrdens:
    """Listagem paginada por prioridade de status (fila de atendimento)."""

    def __init__(self, repo: OrdemDeServicoRepository) -> None:
        self._repo = repo

    def executar(
        self, offset: int = 0, limit: int = 20, *, incluir_encerradas: bool = False
    ) -> list[OrdemResumoDTO]:
        ordens = self._repo.listar(
            offset=offset, limit=limit, incluir_encerradas=incluir_encerradas
        )
        return [
            OrdemResumoDTO(
                id=o.id,
                cliente_id=o.cliente_id,
                veiculo_id=o.veiculo_id,
                status=o.status.value,
                criado_em=o.criado_em,
                atualizado_em=o.atualizado_em,
            )
            for o in ordens
        ]

    def contar(self, *, incluir_encerradas: bool = False) -> int:
        """Total do universo listado (mesmo filtro de ``executar``)."""
        return self._repo.contar(incluir_encerradas=incluir_encerradas)


class ObterOrdem:
    """Projecao completa de uma ordem: historico, etapa e passos da saga.

    A OS e a saga vem de uma consulta so: o status e a etapa do mesmo instante.
    """

    def __init__(self, consulta: ConsultaDaOrdem) -> None:
        self._consulta = consulta

    def executar(self, ordem_id: UUID) -> OrdemDeServicoDTO:
        """Projeta a ordem; ``OrdemNaoEncontradaException`` (404) se nao existe."""
        lida = self._consulta.com_saga(ordem_id)
        if lida is None:
            raise OrdemNaoEncontradaException(ordem_id)
        return _ordem_dto(*lida)


def _saga_dto(saga: Saga) -> SagaDTO:
    """Projeta a saga para o ``SagaDTO`` da operacao (RFC-004 secoes 4.7 e 6.1)."""
    em_voo = saga.comando_em_voo
    return SagaDTO(
        ordem_id=saga.ordem_id,
        etapa=saga.etapa.value,
        motivo=saga.motivo,
        falha=saga.falha,
        plano_compensacao=saga.plano_compensacao,
        comando_em_voo=(
            ComandoEmVooDTO(
                tipo=em_voo["tipo"],
                enviado_em=datetime.fromisoformat(em_voo["enviado_em"]),
            )
            if em_voo is not None
            else None
        ),
        reenvios=saga.reenvios,
        prazo_resposta_em=saga.prazo_resposta_em,
        passos=saga.passos,
    )


class ObterSaga:
    """Estado da saga de uma ordem, para a operacao (RFC-004 secao 4.7)."""

    def __init__(self, sagas: SagaRepository) -> None:
        self._sagas = sagas

    def executar(self, ordem_id: UUID) -> SagaDTO:
        """Projeta a saga; ``SagaNaoEncontradaException`` (404) se nao existe."""
        saga = self._sagas.obter(ordem_id)
        if saga is None:
            raise SagaNaoEncontradaException(ordem_id)
        return _saga_dto(saga)


# Respostas do cancelamento recusado (RFC-004 secao 6.1).
CANCELAMENTO_RECUSADO: Final = "Cancelamento recusado: execução já iniciada"
ORDEM_JA_CANCELADA: Final = "Ordem já cancelada"
# Leituras do pedido quando o consumidor grava a mesma OS ou saga no meio.
_LEITURAS_DO_CANCELAMENTO: Final = 3
# Desfecho do pedido na trilha de auditoria: a compensacao comecou agora, ou ja
# estava em curso ou parada e o pedido nao mudou nada.
CANCELAMENTO_ACEITO: Final = "aceito"
CANCELAMENTO_REPETIDO: Final = "repetido"


class CancelarOrdem:
    """Pedido de cancelamento do atendimento (RFC-004 secoes 4.4 e 6.1).

    Antes do pivot, a OS guarda o texto do pedido e a saga entra em
    compensando, com o plano e a primeira compensacao na outbox, num commit
    so; a OS so fica CANCELADA quando a ultima compensacao responde. Com a
    compensacao em curso ou parada, o pedido repetido nao muda nada. Depois do
    inicio da execucao, ou com a ordem ja cancelada, 409. Toda OS tem saga
    desde a abertura: a resposta e sempre 202 ou 409.
    """

    def __init__(
        self,
        repo: OrdemDeServicoRepository,
        uow: UnitOfWork,
        sagas: SagaRepository,
        contexto: ContextoDeEnvio,
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._sagas = sagas
        self._contexto = contexto

    def executar(self, ordem_id: UUID, motivo: str, *, ator: str) -> OrdemDeServicoDTO:
        """Pede o cancelamento; ``ator`` e o sub do JWT.

        Conflito de versao (o consumidor gravou a mesma OS ou saga no meio):
        rele e decide de novo, ate tres leituras (*reread value*, RFC-004 secao
        4.5), porque a etapa pode ter mudado, e com ela a resposta. Todo
        desfecho, aceito ou recusado, vai para a trilha de auditoria com o
        ``ator`` e o ``correlation_id`` (a ordem), nunca o texto do motivo
        (ADR-039).

        Raises:
            OrdemNaoEncontradaException: ordem inexistente (404).
            SagaNaoEncontradaException: OS sem saga (404; nao deveria existir).
            TransicaoStatusInvalidaException: execucao ja iniciada, ordem ja
                cancelada ou encerrada (409).
            ConflitoDeConcorrenciaException: o conflito voltou nas tres leituras
                (409).
            ValorInvalidoException: motivo vazio, longo demais ou com
                caractere de controle (422).
        """
        try:
            # O texto vale antes de qualquer leitura ou efeito, tambem no pedido
            # repetido, que depois nao o grava.
            texto = motivo_de_cancelamento(motivo)
            dto, resultado = self._com_releituras(ordem_id, texto, ator)
        except DomainException as exc:
            _log.warning(
                "order cancellation refused",
                correlation_id=str(ordem_id),
                ator=ator,
                resultado=exc.codigo,
            )
            raise
        _log.info(
            "order cancellation requested",
            correlation_id=str(ordem_id),
            ator=ator,
            resultado=resultado,
            etapa=dto.etapa,
        )
        return dto

    def _com_releituras(
        self, ordem_id: UUID, motivo: str, ator: str
    ) -> tuple[OrdemDeServicoDTO, str]:
        leitura = 1
        while True:
            try:
                return self._cancelar(ordem_id, motivo, ator)
            except ConflitoDeConcorrenciaException:
                if leitura == _LEITURAS_DO_CANCELAMENTO:
                    raise
                leitura += 1

    def _cancelar(
        self, ordem_id: UUID, motivo: str, ator: str
    ) -> tuple[OrdemDeServicoDTO, str]:
        """Uma leitura e a decisao sobre ela, numa transacao: o DTO e o desfecho."""
        with self._uow:
            ordem = _obter_ordem(self._repo, ordem_id)
            # Lida na mesma transacao: nada fica aberto depois do commit.
            saga = self._sagas.obter(ordem_id)
            if saga is None:
                raise SagaNaoEncontradaException(ordem_id)
            if saga.etapa in ETAPAS_EM_COMPENSACAO:
                # A compensacao ja comecou: o pedido repetido nao muda nada.
                return _ordem_dto(ordem, saga), CANCELAMENTO_REPETIDO
            # O comando e o contexto que a saga passa a guardar nascem no span
            # do cancelamento, filho do trace dela (ADR-043).
            with self._contexto.span("saga cancelamento", saga.traceparent):
                self._compensar(ordem, saga, motivo, ator)
                # A OS antes da saga, a mesma ordem do consumidor: sem deadlock.
                self._repo.salvar(ordem)
                self._sagas.salvar(saga)
                self._uow.commit()
        return _ordem_dto(ordem, saga), CANCELAMENTO_ACEITO

    def _compensar(
        self, ordem: OrdemDeServico, saga: Saga, motivo: str, ator: str
    ) -> None:
        """O texto na OS, a primeira compensacao na outbox e a saga compensando.

        A primeira compensacao sai sem ``causation_id`` (a causa e a requisicao,
        RFC-004 secao 5.2) e com o prazo tecnico gravado no envio (secao 4.6).
        """
        if saga.etapa is EtapaSaga.COMPENSADA:
            raise TransicaoStatusInvalidaException(mensagem=ORDEM_JA_CANCELADA)
        if saga.etapa not in ANTES_DO_PIVOT:
            raise TransicaoStatusInvalidaException(mensagem=CANCELAMENTO_RECUSADO)
        # O que pode recusar o pedido vem antes de mudar a OS ou gravar o comando.
        marcos = MarcosDaOrdem.da_ordem(ordem)
        agora = self._contexto.instante_do_passo(
            saga, tipo=GatilhoDeProcesso.CANCELAMENTO_SOLICITADO
        )
        primeira = saga.primeira_compensacao_do_cancelamento(marcos, agora=agora)
        ordem.registrar_pedido_de_cancelamento(motivo)
        envio = self._contexto.enviar(
            self._uow, primeira, correlation_id=ordem.id, agora=agora
        )
        saga.solicitar_cancelamento(marcos, agora=agora, ator=ator, envio=envio)


# Resposta da retomada fora de falha_na_compensacao (RFC-004 secao 6.1).
RETOMADA_RECUSADA: Final = "Retomada recusada: a saga não está em falha na compensação"


class RetomarCompensacao:
    """Retomada da compensacao parada, pela operacao (RFC-004 secoes 4.7 e 6.1).

    Em ``falha_na_compensacao`` reenvia a compensacao pendente (o comando em voo,
    com os mesmos ``dados``) com id novo, prazo novo e contador zerado, e a saga
    volta a ``compensando``. Os ids dos envios anteriores ficam: a resposta
    tardia ao original ainda casa. Em outra etapa, 409.
    """

    def __init__(
        self, uow: UnitOfWork, sagas: SagaRepository, contexto: ContextoDeEnvio
    ) -> None:
        self._uow = uow
        self._sagas = sagas
        self._contexto = contexto

    def executar(self, ordem_id: UUID, *, ator: str) -> SagaDTO:
        """Retoma a compensacao; ``ator`` e o sub do JWT do admin.

        Todo desfecho vai para a trilha de auditoria com o ``ator`` e o
        ``correlation_id`` (a ordem), como o cancelamento (ADR-039).

        Raises:
            SagaNaoEncontradaException: OS sem saga (404).
            TransicaoStatusInvalidaException: saga fora de
                ``falha_na_compensacao`` (409).
            ConflitoDeConcorrenciaException: o consumidor gravou a saga no meio
                (409; o operador repete o pedido).
        """
        try:
            dto = self._retomar(ordem_id, ator)
        except DomainException as exc:
            _log.warning(
                "saga resume refused",
                correlation_id=str(ordem_id),
                ator=ator,
                resultado=exc.codigo,
            )
            raise
        _log.info(
            "saga resumed",
            correlation_id=str(ordem_id),
            ator=ator,
            etapa=dto.etapa,
            comando=dto.comando_em_voo.tipo if dto.comando_em_voo else None,
        )
        return dto

    def _retomar(self, ordem_id: UUID, ator: str) -> SagaDTO:
        with self._uow:
            saga = self._sagas.obter(ordem_id)
            if saga is None:
                raise SagaNaoEncontradaException(ordem_id)
            if saga.etapa is not EtapaSaga.FALHA_NA_COMPENSACAO:
                raise TransicaoStatusInvalidaException(mensagem=RETOMADA_RECUSADA)
            # O comando e o contexto que a saga passa a guardar nascem no span da
            # retomada, filho do trace dela, com link para a requisicao.
            with self._contexto.span("saga retomada", saga.traceparent):
                agora = self._contexto.instante_do_passo(
                    saga, tipo=GatilhoDeProcesso.RETOMADA
                )
                envio = self._contexto.enviar(
                    self._uow,
                    comando_a_reenviar(saga),
                    correlation_id=saga.ordem_id,
                    agora=agora,
                )
                saga.retomar(envio, agora=agora, ator=ator)
                self._sagas.salvar(saga)
                self._uow.commit()
        return _saga_dto(saga)


class RegistrarEntrega:
    """Entrega do veiculo ao cliente (T10, fora da saga): FINALIZADA -> ENTREGUE."""

    def __init__(
        self, repo: OrdemDeServicoRepository, uow: UnitOfWork, sagas: SagaRepository
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._sagas = sagas

    def executar(self, ordem_id: UUID, *, ator: str) -> OrdemDeServicoDTO:
        """Registra a entrega com origem ATENDIMENTO; ``ator`` e o sub do JWT.

        Raises:
            OrdemNaoEncontradaException: ordem inexistente (404).
            TransicaoStatusInvalidaException: ordem fora de FINALIZADA (409).
            ConflitoDeConcorrenciaException: escrita concorrente (409).
        """
        with self._uow:
            ordem = _obter_ordem(self._repo, ordem_id)
            saga = self._sagas.obter(ordem_id)
            ordem.registrar_entrega(ator=ator)
            self._repo.salvar(ordem)
            self._uow.commit()
        return _ordem_dto(ordem, saga)


_TAMANHO_CNPJ: Final = 14


def _documento(numero: str) -> Documento:
    """CNPJ se tiver 14 caracteres sem mascara (inclui o alfanumerico), senao CPF.

    O VO valida o digito verificador (modulo 11) e levanta ``ValueError``.
    """
    if len(normalizar_cnpj(numero)) == _TAMANHO_CNPJ:
        return CNPJ(numero=numero)
    return CPF(numero=numero)


class ConsultarAcompanhamento:
    """Consulta publica por placa + documento (CPF/CNPJ)."""

    def __init__(self, consulta: ConsultaAcompanhamento) -> None:
        self._consulta = consulta

    def executar(self, placa: str, documento: str) -> AcompanhamentoDTO | None:
        """Ordem mais recente do par placa+documento, ou ``None``.

        Documento (digito verificador) e placa (formato) sao validados pelos
        VOs antes de qualquer acesso ao banco. Entrada invalida devolve
        ``None``, o mesmo resultado de "nao encontrada": a rota publica
        responde o mesmo 404 nos dois casos (anti-enumeracao).
        """
        try:
            placa_vo = Placa(valor=placa)
            documento_vo = _documento(documento)
        except ValueError:
            return None
        return self._consulta.mais_recente(placa_vo, documento_vo)
