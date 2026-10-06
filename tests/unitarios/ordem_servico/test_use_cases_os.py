"""Casos de uso de OS com repositorio em memoria e UoW fake (sem banco)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.dominio.cnpj import CNPJ
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    TransicaoStatusInvalidaException,
)
from src.compartilhado.dominio.placa import Placa
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO, AcompanhamentoDTO
from src.ordem_servico.aplicacao.use_cases import (
    AbrirOrdem,
    CancelarOrdem,
    ConsultarAcompanhamento,
    ListarOrdens,
    ObterOrdem,
    RegistrarEntrega,
)
from src.ordem_servico.dominio.exceptions import (
    ClienteNaoEncontradoException,
    OrdemNaoEncontradaException,
    VeiculoNaoEncontradoException,
)
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import CHECKOUT_URL, LINK_DECISAO, ordem_em
from tests.unitarios.fakes import (
    ClientePortFake,
    ConsultaAcompanhamentoEspia,
    FakeUnitOfWork,
    RepoEmMemoria,
)


def _dto() -> AbrirOrdemDTO:
    return AbrirOrdemDTO(
        cliente_id=uuid4(), veiculo_id=uuid4(), descricao_problema="Freio rangendo"
    )


class TestAbrirOrdem:
    def test_abre_persiste_e_projeta(self) -> None:
        repo, uow = RepoEmMemoria(), FakeUnitOfWork()
        dto = _dto()

        resultado = AbrirOrdem(repo, uow, ClientePortFake()).executar(dto)

        assert uow.committed
        (salva,) = repo.salvas
        assert resultado.id == salva.id
        assert (resultado.cliente_id, resultado.veiculo_id) == (
            dto.cliente_id,
            dto.veiculo_id,
        )
        assert resultado.status == "recebida"
        assert resultado.descricao_problema == "Freio rangendo"
        assert resultado.orcamento is None
        assert resultado.pagamento is None
        assert resultado.versao == 1
        (abertura,) = resultado.historico
        assert (abertura.de, abertura.para, abertura.origem) == (
            None,
            "recebida",
            "atendimento",
        )

    def test_cliente_inexistente_ou_inativo_levanta_sem_persistir(self) -> None:
        repo, uow = RepoEmMemoria(), FakeUnitOfWork()
        uc = AbrirOrdem(repo, uow, ClientePortFake(cliente_ok=False))

        with pytest.raises(ClienteNaoEncontradoException):
            uc.executar(_dto())

        assert repo.salvas == []
        assert not uow.committed

    def test_veiculo_de_outro_cliente_levanta_sem_persistir(self) -> None:
        repo, uow = RepoEmMemoria(), FakeUnitOfWork()
        uc = AbrirOrdem(repo, uow, ClientePortFake(veiculo_ok=False))

        with pytest.raises(VeiculoNaoEncontradoException):
            uc.executar(_dto())

        assert repo.salvas == []
        assert not uow.committed

    def test_descricao_invalida_levanta_value_error(self) -> None:
        dto = AbrirOrdemDTO(
            cliente_id=uuid4(), veiculo_id=uuid4(), descricao_problema="   "
        )
        with pytest.raises(ValueError, match="descricao do problema"):
            AbrirOrdem(RepoEmMemoria(), FakeUnitOfWork(), ClientePortFake()).executar(
                dto
            )


class TestObterOrdem:
    def test_projeta_resumos_e_historico(self) -> None:
        ordem = ordem_em(StatusOrdem.AGUARDANDO_PAGAMENTO)

        dto = ObterOrdem(RepoEmMemoria(ordem)).executar(ordem.id)

        assert dto.status == "aguardando_pagamento"
        assert dto.orcamento is not None
        assert dto.orcamento.total == Decimal("350.00")
        assert dto.orcamento.moeda == "BRL"
        assert dto.orcamento.link_decisao == LINK_DECISAO
        assert dto.pagamento is not None
        assert dto.pagamento.status == "solicitado"
        assert dto.pagamento.checkout_url == CHECKOUT_URL
        assert [m.para for m in dto.historico] == [
            "recebida",
            "em_diagnostico",
            "aguardando_aprovacao",
            "aguardando_pagamento",
        ]
        assert [m.origem for m in dto.historico] == [
            "atendimento",
            "execucao",
            "billing",
            "billing",
        ]

    def test_inexistente_levanta_404(self) -> None:
        with pytest.raises(OrdemNaoEncontradaException):
            ObterOrdem(RepoEmMemoria()).executar(uuid4())


class TestCancelarOrdem:
    def test_cancela_pelo_atendimento(self) -> None:
        ordem = ordem_em(StatusOrdem.AGUARDANDO_APROVACAO)
        repo, uow = RepoEmMemoria(ordem), FakeUnitOfWork()

        dto = CancelarOrdem(repo, uow).executar(ordem.id, "cliente desistiu")

        assert uow.committed
        assert repo.salvas == [ordem]
        assert dto.status == "cancelada"
        assert dto.motivo_cancelamento == "cliente desistiu"
        assert dto.historico[-1].origem == "atendimento"
        assert dto.historico[-1].motivo == "cliente desistiu"

    def test_depois_da_execucao_levanta_409_sem_persistir(self) -> None:
        ordem = ordem_em(StatusOrdem.EM_EXECUCAO)
        repo, uow = RepoEmMemoria(ordem), FakeUnitOfWork()

        with pytest.raises(TransicaoStatusInvalidaException):
            CancelarOrdem(repo, uow).executar(ordem.id, "tarde")

        assert repo.salvas == []
        assert not uow.committed
        assert uow.rolled_back

    def test_conflito_de_versao_propaga_sem_commit(self) -> None:
        ordem = ordem_em(StatusOrdem.RECEBIDA)
        repo, uow = RepoEmMemoria(ordem, conflito=True), FakeUnitOfWork()

        with pytest.raises(ConflitoDeConcorrenciaException):
            CancelarOrdem(repo, uow).executar(ordem.id, "x")

        assert not uow.committed
        assert uow.rolled_back

    def test_inexistente_levanta_404(self) -> None:
        with pytest.raises(OrdemNaoEncontradaException):
            CancelarOrdem(RepoEmMemoria(), FakeUnitOfWork()).executar(uuid4(), "x")


class TestRegistrarEntrega:
    def test_entrega_finalizada(self) -> None:
        ordem = ordem_em(StatusOrdem.FINALIZADA)
        repo, uow = RepoEmMemoria(ordem), FakeUnitOfWork()

        dto = RegistrarEntrega(repo, uow).executar(ordem.id)

        assert uow.committed
        assert dto.status == "entregue"
        assert dto.historico[-1].origem == "atendimento"

    @pytest.mark.parametrize(
        "estado",
        [s for s in StatusOrdem if s is not StatusOrdem.FINALIZADA],
    )
    def test_fora_de_finalizada_levanta_409(self, estado: StatusOrdem) -> None:
        ordem = ordem_em(estado)
        repo, uow = RepoEmMemoria(ordem), FakeUnitOfWork()

        with pytest.raises(TransicaoStatusInvalidaException):
            RegistrarEntrega(repo, uow).executar(ordem.id)

        assert repo.salvas == []
        assert not uow.committed


class TestListarOrdens:
    def test_repassa_paginacao_e_filtro(self) -> None:
        ordem = ordem_em(StatusOrdem.EM_DIAGNOSTICO)
        repo = RepoEmMemoria(ordem)
        uc = ListarOrdens(repo)

        itens = uc.executar(offset=5, limit=7, incluir_encerradas=True)

        assert repo.args_listar == (5, 7, True)
        (item,) = itens
        assert (item.id, item.status) == (ordem.id, "em_diagnostico")
        assert uc.contar(incluir_encerradas=True) == 42
        assert repo.args_contar is True


def _arabe_indico(digitos: str) -> str:
    return "".join(chr(0x0660 + int(d)) for d in digitos)


_CPF = "52998224725"
_ACOMPANHAMENTO = AcompanhamentoDTO(
    status="em_diagnostico",
    criado_em=datetime(2026, 10, 1, 12, tzinfo=UTC),
    atualizado_em=datetime(2026, 10, 1, 13, tzinfo=UTC),
)


class TestConsultarAcompanhamento:
    def test_par_valido_consulta_com_os_vos_normalizados(self) -> None:
        espia = ConsultaAcompanhamentoEspia(resultado=_ACOMPANHAMENTO)

        dto = ConsultarAcompanhamento(espia).executar("abc-1d23", "529.982.247-25")

        assert dto is _ACOMPANHAMENTO
        assert espia.chamadas == [(Placa(valor="ABC1D23"), CPF(numero=_CPF))]

    @pytest.mark.parametrize(
        ("documento", "esperado"),
        [
            pytest.param(
                "11.222.333/0001-81", CNPJ(numero="11222333000181"), id="cnpj"
            ),
            pytest.param(
                "12.abc.345/01de-35", CNPJ(numero="12ABC34501DE35"), id="cnpj-alfanum"
            ),
        ],
    )
    def test_documento_de_14_caracteres_vira_cnpj(
        self, documento: str, esperado: CNPJ
    ) -> None:
        espia = ConsultaAcompanhamentoEspia()

        assert ConsultarAcompanhamento(espia).executar("ABC1D23", documento) is None
        assert espia.chamadas == [(Placa(valor="ABC1D23"), esperado)]

    @pytest.mark.parametrize(
        ("placa", "documento"),
        [
            pytest.param("ABC1D23", "52998224726", id="cpf-dv-errado"),
            pytest.param("ABC1D23", "123.456.789-00", id="cpf-mascarado-dv-errado"),
            pytest.param("ABC1D23", "111.111.111-11", id="cpf-digitos-iguais"),
            pytest.param("ABC1D23", "11.111.111/0001-11", id="cnpj-dv-errado"),
            pytest.param("ABC1D23", "12ABC34501DE36", id="cnpj-alfanum-dv-errado"),
            pytest.param("ABC1D23", "abcdefghijk", id="documento-sem-digito"),
            pytest.param("ABC1D23", _arabe_indico(_CPF), id="cpf-arabe-indico"),
            pytest.param("!!!!!!!", _CPF, id="placa-com-simbolos"),
            pytest.param("1234ABC", _CPF, id="placa-fora-do-padrao"),
            pytest.param("ABC" + _arabe_indico("1234"), _CPF, id="placa-arabe-indica"),
        ],
    )
    def test_entrada_invalida_devolve_none_sem_consultar_o_banco(
        self, placa: str, documento: str
    ) -> None:
        espia = ConsultaAcompanhamentoEspia(resultado=_ACOMPANHAMENTO)

        assert ConsultarAcompanhamento(espia).executar(placa, documento) is None
        assert espia.chamadas == []

    def test_par_valido_sem_ordem_devolve_none(self) -> None:
        espia = ConsultaAcompanhamentoEspia()

        assert ConsultarAcompanhamento(espia).executar("ABC1D23", _CPF) is None
        assert len(espia.chamadas) == 1
