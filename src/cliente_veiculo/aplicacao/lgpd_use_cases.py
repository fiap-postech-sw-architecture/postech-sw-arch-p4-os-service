from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.cliente_veiculo.aplicacao.dtos import (
    ConsentimentoDTO,
    DadosPessoaisDTO,
)
from src.cliente_veiculo.aplicacao.use_cases import (
    obter_cliente_ou_falhar,
    tipo_documento,
    travar_cliente_sem_os_ativa,
    veiculo_dto,
)
from src.cliente_veiculo.dominio.consentimento import ConsentimentoCliente
from src.cliente_veiculo.dominio.exceptions import (
    ConsentimentoNaoEncontradoException,
)
from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException

if TYPE_CHECKING:
    from uuid import UUID

    from src.cliente_veiculo.aplicacao.dtos import RegistrarConsentimentoDTO
    from src.cliente_veiculo.aplicacao.ports import OrdemDeServicoPort
    from src.cliente_veiculo.dominio.repository import ClienteRepository
    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork


class ExportarDadosPessoais:
    def __init__(self, repo: ClienteRepository) -> None:
        self._repo = repo

    def executar(self, cliente_id: UUID) -> DadosPessoaisDTO:
        cliente = obter_cliente_ou_falhar(self._repo, cliente_id)
        return DadosPessoaisDTO(
            id=cliente.id,
            nome=cliente.nome,
            documento_formatado=cliente.documento.formatado(),
            tipo_documento=tipo_documento(cliente),
            contato=cliente.contato.valor,
            veiculos=[veiculo_dto(v) for v in cliente.veiculos],
            ativo=cliente.ativo,
        )


class ExcluirDadosPessoais:
    """Anonimiza (erasure LGPD) os dados de um cliente sem OS ativa.

    Alcanca cliente, veiculos, o texto livre das OS dele (descricao do
    problema e motivos de cancelamento), a placa e a descricao que ficaram nos
    comandos da outbox e, por comando ``AnonimizarVeiculo`` (um por veiculo,
    fora da saga), a placa guardada na Execucao (RFC-004 secao 5.3), tudo na
    mesma transacao: se ela desfaz, nenhum comando sai. Repetido, o erasure nao
    reenvia o comando de quem ja foi anonimizado.

    Espelha o guard de `DesativarCliente`: trava o cliente (FOR UPDATE) e
    consulta `OrdemDeServicoPort.existe_os_ativa_para_cliente` na mesma
    transacao do apagamento; se houver OS ativa, levanta
    `ViolacaoRegraDeNegocioException` (409). Com OS ativa o tratamento ainda
    nao terminou: os dados sao necessarios para executar o contrato do qual o
    titular e parte (LGPD, art. 7, V), e apagar agora quebraria a prestacao do
    servico.
    """

    def __init__(
        self,
        repo: ClienteRepository,
        uow: UnitOfWork,
        os_port: OrdemDeServicoPort,
    ) -> None:
        self._repo = repo
        self._uow = uow
        self._os_port = os_port

    def executar(self, cliente_id: UUID) -> None:
        with self._uow:
            travar_cliente_sem_os_ativa(self._repo, self._os_port, cliente_id)
            # Lidos antes: depois de anonimizados, o repositorio nao os acha mais.
            veiculos = self._repo.ids_dos_veiculos_a_anonimizar(cliente_id)
            self._repo.anonimizar_dados(cliente_id)
            self._os_port.anonimizar_textos_livres_do_cliente(cliente_id)
            self._os_port.anonimizar_comandos_do_cliente_na_outbox(cliente_id)
            for veiculo_id in veiculos:
                # Fora da saga: o correlation_id e o veiculo, sem causa e sem
                # resposta; a placa nunca vai para o log.
                self._uow.publicar_comando(
                    Comando.ANONIMIZAR_VEICULO,
                    {"veiculo_id": veiculo_id},
                    correlation_id=veiculo_id,
                )
            self._uow.commit()


class RegistrarConsentimento:
    def __init__(self, repo: ClienteRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(
        self, cliente_id: UUID, dto: RegistrarConsentimentoDTO
    ) -> ConsentimentoDTO:
        obter_cliente_ou_falhar(self._repo, cliente_id)
        existente = self._repo.obter_consentimento(cliente_id, dto.tipo)
        if existente is not None and existente.ativo:
            # Grant-grant do mesmo tipo: ja existe consentimento ativo para o
            # par cliente+tipo. Levanta violacao de regra (409 no mapeamento
            # de erros existente) em vez de criar registro ativo duplicado.
            raise ViolacaoRegraDeNegocioException(
                mensagem="Consentimento ativo ja existe para este tipo"
            )
        agora = datetime.now(tz=UTC)
        consentimento = ConsentimentoCliente.criar(
            cliente_id=cliente_id,
            tipo=dto.tipo,
            concedido_em=agora,
        )
        with self._uow:
            self._repo.salvar_consentimento(consentimento)
            self._uow.commit()
        return ConsentimentoDTO(
            id=consentimento.id,
            cliente_id=consentimento.cliente_id,
            tipo=consentimento.tipo,
            concedido_em=consentimento.concedido_em,
            revogado_em=consentimento.revogado_em,
            ativo=consentimento.ativo,
        )


class RevogarConsentimento:
    def __init__(self, repo: ClienteRepository, uow: UnitOfWork) -> None:
        self._repo = repo
        self._uow = uow

    def executar(self, cliente_id: UUID, tipo: str) -> None:
        consentimento = self._repo.obter_consentimento(cliente_id, tipo)
        if consentimento is None:
            raise ConsentimentoNaoEncontradoException()
        agora = datetime.now(tz=UTC)
        consentimento.revogar(agora)
        with self._uow:
            self._repo.salvar_consentimento(consentimento)
            self._uow.commit()
