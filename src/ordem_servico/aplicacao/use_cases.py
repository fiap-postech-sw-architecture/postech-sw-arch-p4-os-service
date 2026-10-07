"""Casos de uso da aplicacao Ordem de Servico.

Cada classe expoe ``executar(...)``: compoe repositorio, ``UnitOfWork`` e
ports; as regras ficam no agregado. A abertura inicia a saga e grava o
``SolicitarDiagnostico`` na outbox com ``UnitOfWork.publicar_comando``, no
mesmo commit da OS; os passos seguintes sao do ``OrquestradorDaSaga``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

import structlog

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.cnpj import CNPJ
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.documento import normalizar_cnpj
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
from src.ordem_servico.aplicacao.saga.saga import (
    Envio,
    Saga,
    SagaNaoEncontradaException,
)
from src.ordem_servico.dominio.exceptions import (
    ClienteNaoEncontradoException,
    OrdemNaoEncontradaException,
    VeiculoNaoEncontradoException,
)
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.compartilhado.dominio.documento import Documento
    from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
    from src.ordem_servico.aplicacao.ports import (
        ClientePort,
        ConsultaAcompanhamento,
        SagaRepository,
    )
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
            comando_id = self._uow.publicar_comando(
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
                correlation_id=ordem.id,
            )
            saga = Saga.iniciar(
                ordem.id,
                envio=Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=comando_id),
                ator=dto.ator,
                agora=ordem.criado_em,
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
    """Projecao completa de uma ordem: historico, etapa e passos da saga."""

    def __init__(self, repo: OrdemDeServicoRepository, sagas: SagaRepository) -> None:
        self._repo = repo
        self._sagas = sagas

    def executar(self, ordem_id: UUID) -> OrdemDeServicoDTO:
        """Projeta a ordem; ``OrdemNaoEncontradaException`` (404) se nao existe."""
        ordem = _obter_ordem(self._repo, ordem_id)
        return _ordem_dto(ordem, self._sagas.obter(ordem_id))


class ObterSaga:
    """Estado da saga de uma ordem, para a operacao (RFC-004 secao 4.7)."""

    def __init__(self, sagas: SagaRepository) -> None:
        self._sagas = sagas

    def executar(self, ordem_id: UUID) -> SagaDTO:
        """Projeta a saga; ``SagaNaoEncontradaException`` (404) se nao existe."""
        saga = self._sagas.obter(ordem_id)
        if saga is None:
            raise SagaNaoEncontradaException(ordem_id)
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


class CancelarOrdem:
    """Cancelamento pelo atendimento antes do inicio da execucao.

    Enquanto a saga nao existe, a OS vai direto para CANCELADA; com ela, o
    cancelamento dispara as compensacoes antes (RFC-004 secao 4.4).
    """

    def __init__(
        self, repo: OrdemDeServicoRepository, uow: UnitOfWork, sagas: SagaRepository
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._sagas = sagas

    def executar(
        self, ordem_id: UUID, motivo: str, *, ator: str | None
    ) -> OrdemDeServicoDTO:
        """Cancela a ordem com origem ATENDIMENTO; ``ator`` e o sub do JWT.

        Raises:
            OrdemNaoEncontradaException: ordem inexistente (404).
            TransicaoStatusInvalidaException: execucao ja iniciada ou ordem
                encerrada (409).
            ConflitoDeConcorrenciaException: escrita concorrente (409).
            ValorInvalidoException: motivo vazio, longo demais ou com
                caractere de controle (422).
        """
        with self._uow:
            ordem = _obter_ordem(self._repo, ordem_id)
            # Lida na mesma transacao: nada fica aberto depois do commit.
            saga = self._sagas.obter(ordem_id)
            ordem.cancelar(motivo, OrigemMudanca.ATENDIMENTO, ator=ator)
            self._repo.salvar(ordem)
            self._uow.commit()
        return _ordem_dto(ordem, saga)


class RegistrarEntrega:
    """Entrega do veiculo ao cliente (T10, fora da saga): FINALIZADA -> ENTREGUE."""

    def __init__(
        self, repo: OrdemDeServicoRepository, uow: UnitOfWork, sagas: SagaRepository
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._sagas = sagas

    def executar(self, ordem_id: UUID, *, ator: str | None) -> OrdemDeServicoDTO:
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
