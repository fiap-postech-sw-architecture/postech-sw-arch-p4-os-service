"""Casos de uso da aplicacao Ordem de Servico.

Cada classe expoe ``executar(...)``: compoe repositorio, ``UnitOfWork`` e
ports; as regras ficam no agregado. Os eventos de integracao registrados no
agregado vao para a outbox no mesmo commit da UoW. Comandos para Billing e
Execucao e compensacoes sao papel da saga, fora destes casos de uso.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.cnpj import CNPJ
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.documento import normalizar_cnpj
from src.compartilhado.dominio.placa import Placa
from src.ordem_servico.aplicacao.dtos import (
    AcompanhamentoDTO,
    MudancaDeStatusDTO,
    OrdemDeServicoDTO,
    OrdemResumoDTO,
    ResumoOrcamentoDTO,
    ResumoPagamentoDTO,
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
    from src.ordem_servico.aplicacao.ports import ClientePort, ConsultaAcompanhamento
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository


def _ordem_dto(ordem: OrdemDeServico) -> OrdemDeServicoDTO:
    """Projeta o agregado para ``OrdemDeServicoDTO`` (com o historico)."""
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
            )
            if orcamento is not None
            else None
        ),
        pagamento=(
            ResumoPagamentoDTO(
                pagamento_id=pagamento.pagamento_id,
                status=pagamento.status.value,
                checkout_url=pagamento.checkout_url,
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
                ocorrido_em=m.ocorrido_em,
            )
            for m in ordem.historico
        ),
    )


def _obter_ordem(repo: OrdemDeServicoRepository, ordem_id: UUID) -> OrdemDeServico:
    ordem = repo.obter_por_id(ordem_id)
    if ordem is None:
        raise OrdemNaoEncontradaException(ordem_id)
    return ordem


class AbrirOrdem:
    """Abre a OS em RECEBIDA para um cliente ativo e um veiculo dele."""

    def __init__(
        self,
        repo: OrdemDeServicoRepository,
        uow: UnitOfWork,
        cliente_port: ClientePort,
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._cliente_port = cliente_port

    def executar(self, dto: AbrirOrdemDTO) -> OrdemDeServicoDTO:
        """Valida cliente e veiculo e persiste a OS.

        Raises:
            ClienteNaoEncontradoException: cliente inexistente ou inativo (404).
            VeiculoNaoEncontradoException: veiculo inexistente ou de outro
                cliente (404; os dois casos sao indistinguiveis de proposito).
            ValueError: descricao do problema vazia ou longa demais (422).
        """
        if not self._cliente_port.cliente_existe(dto.cliente_id):
            raise ClienteNaoEncontradoException(dto.cliente_id)
        if not self._cliente_port.veiculo_pertence_ao_cliente(
            dto.cliente_id, dto.veiculo_id
        ):
            raise VeiculoNaoEncontradoException(dto.veiculo_id)
        ordem = OrdemDeServico.abrir(
            cliente_id=dto.cliente_id,
            veiculo_id=dto.veiculo_id,
            descricao_problema=dto.descricao_problema,
        )
        with self._uow:
            self._repo.salvar(ordem)
            self._uow.commit()
        return _ordem_dto(ordem)


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
    """Projecao completa de uma ordem, historico incluso."""

    def __init__(self, repo: OrdemDeServicoRepository) -> None:
        self._repo = repo

    def executar(self, ordem_id: UUID) -> OrdemDeServicoDTO:
        """Projeta a ordem; ``OrdemNaoEncontradaException`` (404) se nao existe."""
        return _ordem_dto(_obter_ordem(self._repo, ordem_id))


class CancelarOrdem:
    """Cancelamento pelo atendimento antes do inicio da execucao.

    Enquanto a saga nao existe, a OS vai direto para CANCELADA; com ela, o
    cancelamento dispara as compensacoes antes (brief secao 3).
    """

    def __init__(self, repo: OrdemDeServicoRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(self, ordem_id: UUID, motivo: str) -> OrdemDeServicoDTO:
        """Cancela a ordem com origem ATENDIMENTO.

        Raises:
            OrdemNaoEncontradaException: ordem inexistente (404).
            TransicaoStatusInvalidaException: execucao ja iniciada ou ordem
                encerrada (409).
            ConflitoDeConcorrenciaException: escrita concorrente (409).
            ValueError: motivo vazio ou longo demais (422).
        """
        with self._uow:
            ordem = _obter_ordem(self._repo, ordem_id)
            ordem.cancelar(motivo, OrigemMudanca.ATENDIMENTO)
            self._repo.salvar(ordem)
            self._uow.commit()
        return _ordem_dto(ordem)


class RegistrarEntrega:
    """Entrega do veiculo ao cliente: FINALIZADA -> ENTREGUE."""

    def __init__(self, repo: OrdemDeServicoRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(self, ordem_id: UUID) -> OrdemDeServicoDTO:
        """Registra a entrega com origem ATENDIMENTO.

        Raises:
            OrdemNaoEncontradaException: ordem inexistente (404).
            TransicaoStatusInvalidaException: ordem fora de FINALIZADA (409).
            ConflitoDeConcorrenciaException: escrita concorrente (409).
        """
        with self._uow:
            ordem = _obter_ordem(self._repo, ordem_id)
            ordem.registrar_entrega()
            self._repo.salvar(ordem)
            self._uow.commit()
        return _ordem_dto(ordem)


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
