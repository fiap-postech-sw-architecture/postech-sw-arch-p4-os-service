from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from src.cliente_veiculo.aplicacao.dtos import (
    RegistrarConsentimentoDTO,
    VeiculoDTO,
)
from src.cliente_veiculo.aplicacao.lgpd_use_cases import (
    ExcluirDadosPessoais,
    ExportarDadosPessoais,
    RegistrarConsentimento,
    RevogarConsentimento,
)
from src.cliente_veiculo.dominio.cliente import Cliente
from src.cliente_veiculo.dominio.consentimento import ConsentimentoCliente
from src.cliente_veiculo.dominio.contato import Contato
from src.cliente_veiculo.dominio.documento_anonimizado import DocumentoAnonimizado
from src.cliente_veiculo.dominio.exceptions import (
    ClienteNaoEncontradoException,
    ConsentimentoNaoEncontradoException,
)
from src.compartilhado.aplicacao.mensageria import ContratoInvalidoError
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException
from src.compartilhado.dominio.placa import Placa
from tests.unitarios.fakes import FakeUnitOfWork

CPF_VALIDO = "21249722519"


class FakeClienteRepoLGPD:
    def __init__(self, registro: list[str] | None = None) -> None:
        self._clientes: dict[UUID, Cliente] = {}
        self._consentimentos: dict[tuple[UUID, str], ConsentimentoCliente] = {}
        self.anonimizados: set[UUID] = set()
        self.registro = registro if registro is not None else []

    def obter_por_id(self, cliente_id: UUID) -> Cliente | None:
        return self._clientes.get(cliente_id)

    def bloquear_cliente(self, cliente_id: UUID) -> bool:
        self.registro.append("bloquear_cliente")
        return cliente_id in self._clientes

    def salvar(self, cliente: Cliente) -> None:
        self._clientes[cliente.id] = cliente

    def listar(self, offset: int = 0, limit: int = 20) -> list[Cliente]:
        todos = list(self._clientes.values())
        return todos[offset : offset + limit]

    def contar(self) -> int:
        return len(self._clientes)

    def obter_por_documento(self, documento: object) -> Cliente | None:
        return None

    def placa_existe(
        self, placa: Placa, excluir_cliente_id: UUID | None = None
    ) -> bool:
        return False

    def obter_dados_pessoais(self, cliente_id: UUID) -> Cliente | None:
        return self._clientes.get(cliente_id)

    def anonimizar_dados(self, cliente_id: UUID) -> list[UUID]:
        self.registro.append("anonimizar_dados")
        self.anonimizados.add(cliente_id)
        cliente = self._clientes.get(cliente_id)
        return [] if cliente is None else [v.id for v in cliente.veiculos]

    def salvar_consentimento(self, consentimento: ConsentimentoCliente) -> None:
        key = (consentimento.cliente_id, consentimento.tipo)
        self._consentimentos[key] = consentimento

    def obter_consentimento(
        self, cliente_id: UUID, tipo: str
    ) -> ConsentimentoCliente | None:
        return self._consentimentos.get((cliente_id, tipo))

    def revogar_consentimento(self, cliente_id: UUID, tipo: str) -> None:
        key = (cliente_id, tipo)
        c = self._consentimentos.get(key)
        if c is not None:
            c.revogar(datetime.now(tz=UTC))


class TestExportarDadosPessoais:
    def test_sucesso(self) -> None:
        repo = FakeClienteRepoLGPD()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        placa = Placa(valor="ABC1234")
        cliente.adicionar_veiculo(placa, "Fiat", "Uno", 2020)
        repo.salvar(cliente)
        uc = ExportarDadosPessoais(repo=repo)
        result = uc.executar(cliente.id)
        assert result.nome == "Joao"
        assert result.tipo_documento == "cpf"
        assert result.contato == "11999"
        assert len(result.veiculos) == 1
        # p3 #168: reutiliza VeiculoDTO (mesmo shape JSON de antes, sem dict manual).
        assert isinstance(result.veiculos[0], VeiculoDTO)
        assert result.veiculos[0].placa == "ABC1234"
        assert result.veiculos[0].marca == "Fiat"
        assert result.ativo is True

    def test_cliente_inexistente(self) -> None:
        repo = FakeClienteRepoLGPD()
        uc = ExportarDadosPessoais(repo=repo)
        with pytest.raises(ClienteNaoEncontradoException):
            uc.executar(uuid4())

    def test_export_de_cliente_anonimizado_retorna_tipo_anonimizado(self) -> None:
        # Apos anonimizacao, ``DocumentoAnonimizado`` substitui CPF/CNPJ no
        # agregado. O DTO precisa refletir o novo tipo — antes do refactor
        # caia no ramo ``else "cnpj"`` mesmo para CPFs originais.
        repo = FakeClienteRepoLGPD()
        cliente_id = uuid4()
        cliente = Cliente(
            id=cliente_id,
            _nome="ANONIMIZADO",
            _documento=DocumentoAnonimizado(cliente_id=cliente_id),
            _contato=Contato(valor="anonimizado@anonimizado.local"),
        )
        repo.salvar(cliente)
        uc = ExportarDadosPessoais(repo=repo)
        result = uc.executar(cliente_id)
        assert result.tipo_documento == "anonimizado"
        assert result.documento_formatado == "ANONIMIZADO"
        assert result.nome == "ANONIMIZADO"


class StubOrdemDeServicoPort:
    def __init__(
        self, os_ativa_cliente: bool = False, registro: list[str] | None = None
    ) -> None:
        self._os_ativa_cliente = os_ativa_cliente
        self.textos_anonimizados: list[UUID] = []
        self.registro = registro if registro is not None else []

    def existe_os_ativa_para_cliente(self, cliente_id: UUID) -> bool:
        self.registro.append("existe_os_ativa_para_cliente")
        return self._os_ativa_cliente

    def existe_os_para_veiculo(self, veiculo_id: UUID) -> bool:
        return False

    def anonimizar_textos_livres_do_cliente(self, cliente_id: UUID) -> None:
        self.registro.append("anonimizar_textos_livres_do_cliente")
        self.textos_anonimizados.append(cliente_id)

    def anonimizar_comandos_do_cliente_na_outbox(self, cliente_id: UUID) -> None:
        self.registro.append("anonimizar_comandos_do_cliente_na_outbox")


class UowComRegistro(FakeUnitOfWork):
    def __init__(self, registro: list[str]) -> None:
        super().__init__()
        self._registro = registro

    def __enter__(self) -> UowComRegistro:
        self._registro.append("inicio")
        return self

    def commit(self) -> None:
        self._registro.append("commit")
        super().commit()


class TestExcluirDadosPessoais:
    def test_sucesso(self) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        os_port = StubOrdemDeServicoPort(os_ativa_cliente=False)
        uc = ExcluirDadosPessoais(repo=repo, uow=uow, os_port=os_port)
        uc.executar(cliente.id)
        assert cliente.id in repo.anonimizados
        # O texto livre das OS sai na mesma unidade de trabalho.
        assert os_port.textos_anonimizados == [cliente.id]
        assert uow.committed

    def test_cliente_inexistente(self) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        uc = ExcluirDadosPessoais(repo=repo, uow=uow, os_port=StubOrdemDeServicoPort())
        with pytest.raises(ClienteNaoEncontradoException):
            uc.executar(uuid4())
        assert uow.comandos == []

    @pytest.mark.parametrize("quantidade", [0, 1, 3])
    def test_um_comando_anonimizar_veiculo_por_veiculo(self, quantidade: int) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cliente = Cliente(
            _nome="Joao",
            _documento=CPF(numero=CPF_VALIDO),
            _contato=Contato(valor="11999"),
        )
        for numero in range(quantidade):
            cliente.adicionar_veiculo(
                Placa(valor=f"ABC123{numero}"), "Fiat", "Uno", 2020
            )
        repo.salvar(cliente)

        ExcluirDadosPessoais(
            repo=repo, uow=uow, os_port=StubOrdemDeServicoPort()
        ).executar(cliente.id)

        # Fora da saga: o correlation_id e o veiculo, sem causa; a placa nao vai.
        assert [
            (tipo, dados, corr, causa) for tipo, dados, corr, causa in uow.comandos
        ] == [
            ("AnonimizarVeiculo", {"veiculo_id": v.id}, v.id, None)
            for v in cliente.veiculos
        ]
        assert [e["tipo"] for e in uow.envelopes] == ["AnonimizarVeiculo"] * quantidade
        assert "ABC123" not in repr(uow.envelopes)
        assert uow.committed

    def test_comando_fora_do_contrato_desfaz_a_transacao(self) -> None:
        repo = FakeClienteRepoLGPD()
        cliente = Cliente(
            _nome="Joao",
            _documento=CPF(numero=CPF_VALIDO),
            _contato=Contato(valor="11999"),
        )
        cliente.adicionar_veiculo(Placa(valor="ABC1234"), "Fiat", "Uno", 2020)
        repo.salvar(cliente)

        class UowQueRecusa(FakeUnitOfWork):
            def publicar_comando(self, *args: object, **kwargs: object) -> UUID:
                raise ContratoInvalidoError("recusado")

        uow = UowQueRecusa()
        with pytest.raises(ContratoInvalidoError):
            ExcluirDadosPessoais(
                repo=repo, uow=uow, os_port=StubOrdemDeServicoPort()
            ).executar(cliente.id)

        # Sem commit: a transacao (cliente, textos, outbox, comandos) cai inteira.
        assert not uow.committed
        assert uow.rolled_back

    def test_trava_o_cliente_e_checa_os_ativa_dentro_da_transacao(self) -> None:
        # Check-then-act: sem o lock dentro da UoW, uma OS aberta entre a
        # checagem e o UPDATE escaparia do erasure.
        registro: list[str] = []
        repo = FakeClienteRepoLGPD(registro)
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        os_port = StubOrdemDeServicoPort(registro=registro)

        ExcluirDadosPessoais(
            repo=repo, uow=UowComRegistro(registro), os_port=os_port
        ).executar(cliente.id)

        assert registro == [
            "inicio",
            "bloquear_cliente",
            "existe_os_ativa_para_cliente",
            "anonimizar_dados",
            "anonimizar_textos_livres_do_cliente",
            "anonimizar_comandos_do_cliente_na_outbox",
            "commit",
        ]

    def test_com_os_ativa_bloqueado(self) -> None:
        # LGPD Art. 16: dados podem ser retidos para execucao de contrato. Uma
        # OS ativa e o contrato em andamento -> erasure bloqueado com 409
        # (mesmo guard de DesativarCliente, que e menos destrutivo).
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        os_port = StubOrdemDeServicoPort(os_ativa_cliente=True)
        uc = ExcluirDadosPessoais(repo=repo, uow=uow, os_port=os_port)
        with pytest.raises(
            ViolacaoRegraDeNegocioException, match="ordem de servico ativa"
        ):
            uc.executar(cliente.id)
        assert cliente.id not in repo.anonimizados
        assert os_port.textos_anonimizados == []
        assert uow.comandos == []
        assert not uow.committed


class TestRegistrarConsentimento:
    def test_sucesso(self) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        uc = RegistrarConsentimento(repo=repo, uow=uow)
        dto = RegistrarConsentimentoDTO(tipo="tratamento_dados")
        result = uc.executar(cliente.id, dto)
        assert result.tipo == "tratamento_dados"
        assert result.ativo is True
        assert result.revogado_em is None
        assert result.cliente_id == cliente.id
        assert uow.committed

    def test_cliente_inexistente(self) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        uc = RegistrarConsentimento(repo=repo, uow=uow)
        dto = RegistrarConsentimentoDTO(tipo="marketing")
        with pytest.raises(ClienteNaoEncontradoException):
            uc.executar(uuid4(), dto)

    def test_grant_grant_do_mesmo_tipo_rejeitado(self) -> None:
        # p3 #168: grant-grant do mesmo par cliente+tipo com consentimento ainda
        # ativo nao pode criar registro duplicado -> 409 (violacao de regra).
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        uc = RegistrarConsentimento(repo=repo, uow=uow)
        dto = RegistrarConsentimentoDTO(tipo="marketing")
        uc.executar(cliente.id, dto)
        with pytest.raises(
            ViolacaoRegraDeNegocioException, match="Consentimento ativo ja existe"
        ):
            uc.executar(cliente.id, dto)

    def test_grant_apos_revogacao_permitido(self) -> None:
        # A regra bloqueia apenas consentimento ATIVO duplicado: revogar e
        # conceder novamente e um fluxo legitimo (novo registro, nova data).
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        uc = RegistrarConsentimento(repo=repo, uow=uow)
        dto = RegistrarConsentimentoDTO(tipo="marketing")
        uc.executar(cliente.id, dto)
        RevogarConsentimento(repo=repo, uow=uow).executar(cliente.id, "marketing")
        result = uc.executar(cliente.id, dto)
        assert result.ativo is True


class TestRevogarConsentimento:
    def test_sucesso(self) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        agora = datetime.now(tz=UTC)
        consentimento = ConsentimentoCliente(
            _cliente_id=cliente.id,
            _tipo="marketing",
            _concedido_em=agora,
        )
        repo.salvar_consentimento(consentimento)
        uc = RevogarConsentimento(repo=repo, uow=uow)
        uc.executar(cliente.id, "marketing")
        salvo = repo.obter_consentimento(cliente.id, "marketing")
        assert salvo is not None
        assert salvo.ativo is False
        assert uow.committed

    def test_dupla_revogacao_levanta_erro(self) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        cpf = CPF(numero=CPF_VALIDO)
        cliente = Cliente(_nome="Joao", _documento=cpf, _contato=Contato(valor="11999"))
        repo.salvar(cliente)
        agora = datetime.now(tz=UTC)
        consentimento = ConsentimentoCliente(
            _cliente_id=cliente.id,
            _tipo="marketing",
            _concedido_em=agora,
        )
        consentimento.revogar(agora)
        repo.salvar_consentimento(consentimento)
        uc = RevogarConsentimento(repo=repo, uow=uow)
        with pytest.raises(ValueError, match="ja foi revogado"):
            uc.executar(cliente.id, "marketing")

    def test_consentimento_inexistente(self) -> None:
        repo = FakeClienteRepoLGPD()
        uow = FakeUnitOfWork()
        uc = RevogarConsentimento(repo=repo, uow=uow)
        with pytest.raises(ConsentimentoNaoEncontradoException):
            uc.executar(uuid4(), "marketing")
